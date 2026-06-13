"""Coordinator: dynamic-interval refresh of the latest dataset."""

from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ApiError, AuthError, EudaApiClient
from .const import (
    CONF_IDENTIFIER,
    CONF_VIN,
    DATASET_INTERVAL,
    DOMAIN,
    MIN_INTERVAL,
    NO_CONTENT_SUFFIX,
    POST_DATASET_BUFFER,
    RETRY_INTERVAL,
    SERVER_ERROR_BACKOFF_INTERVALS,
)
from .data import Dataset, DataPoint

_LOGGER = logging.getLogger(__name__)
_SERVER_ERROR_CODES = ("HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504")


def _is_server_error(err: Exception) -> bool:
    """True for transient upstream 5xx errors worth backoff/retry."""
    return any(code in str(err) for code in _SERVER_ERROR_CODES)


def _filename_timestamp(name: str) -> datetime | None:
    """Parse a YYYYMMDDhhmmss segment from a dataset filename.

    Handles both layouts seen in the wild ("TIMESTAMP_VIN.zip" and
    "VIN_TIMESTAMP.zip") by scanning the underscore-separated parts
    right-to-left for the first one that parses as a timestamp.
    """
    stem = name.rsplit(".", 1)[0]
    for part in reversed(stem.split("_")):
        try:
            return datetime.strptime(part, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _created_on(entry: dict) -> datetime | None:
    raw = entry.get("createdOn")
    if not raw:
        return _filename_timestamp(entry.get("name", ""))
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return _filename_timestamp(entry.get("name", ""))


class EudaCoordinator(DataUpdateCoordinator[dict[str, DataPoint]]):
    """Fetches the latest dataset and reschedules adaptively."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: EudaApiClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.data[CONF_VIN]}",
            update_interval=RETRY_INTERVAL,
        )
        self.entry = entry
        self.client = client
        self.vin: str = entry.data[CONF_VIN]
        self.identifier: str = entry.data[CONF_IDENTIFIER]
        self.latest_dataset: Dataset | None = None
        self._is_initial_setup: bool = True
        self._consecutive_server_errors: int = 0
        self._listing_failed_server_error: bool = False
        self.status_label: str = "starting"
        self.empty_snapshot_count: int = 0
        self.last_error: str | None = None

    def _server_error_backoff_interval(self):
        """Map consecutive 5xx count to 5 -> 15 -> 30 minute retry spacing."""
        if self._consecutive_server_errors <= 0:
            return RETRY_INTERVAL
        index = min(
            self._consecutive_server_errors - 1,
            len(SERVER_ERROR_BACKOFF_INTERVALS) - 1,
        )
        return SERVER_ERROR_BACKOFF_INTERVALS[index]

    def _note_server_error(self) -> None:
        """Increase 5xx streak and slow down polling progressively."""
        self._consecutive_server_errors += 1
        self.update_interval = self._server_error_backoff_interval()
        _LOGGER.debug(
            "Portal server error streak %d; next retry in %s",
            self._consecutive_server_errors,
            self.update_interval,
        )

    def _reset_server_error_backoff(self) -> None:
        """Return to normal scheduling after a successful portal response."""
        self._consecutive_server_errors = 0

    async def _async_update_data(self) -> dict[str, DataPoint]:
        self.status_label = "updating"
        listing = await self._async_list_with_refresh()

        # content datasets, oldest -> newest by createdOn
        content = sorted(
            (
                e
                for e in listing
                if e.get("name") and not e["name"].endswith(NO_CONTENT_SUFFIX)
            ),
            key=lambda e: _created_on(e) or datetime.min.replace(tzinfo=timezone.utc),
        )
        empty_only = [
            e for e in listing if e.get("name", "").endswith(NO_CONTENT_SUFFIX)
        ]
        _LOGGER.debug("refresh: %d listed, %d with content", len(listing), len(content))

        if not content:
            self.empty_snapshot_count = len(empty_only)
            if not self._listing_failed_server_error:
                self.status_label = (
                    "empty_snapshots" if empty_only else "waiting_for_portal_data"
                )
            if not self._listing_failed_server_error:
                self._reschedule(listing)
            if self.data:
                # Subsequent refresh: keep previous data
                _LOGGER.debug("No new datasets available, keeping previous data")
                return self.data
            # First load with no data: fail so HA retries setup
            _LOGGER.warning(
                "No datasets available on first load, will retry in %s", RETRY_INTERVAL
            )
            raise UpdateFailed("No datasets available on first load")

        # Try to load datasets, starting with newest and falling back to older ones
        last_error = None
        for dataset_entry in reversed(content):
            # Use fewer, faster retries during initial setup for better UX
            # Full retries kick in after first successful load
            max_retries = 3 if self._is_initial_setup else 5
            retry_delay = 3 if self._is_initial_setup else 5

            for attempt in range(max_retries):
                try:
                    payload = await self.client.async_download_dataset(
                        self.vin, self.identifier, dataset_entry["name"]
                    )
                    self.latest_dataset = Dataset.from_json(payload)
                    self._is_initial_setup = False
                    last_error = None
                    break  # Success!
                except ApiError as err:
                    last_error = err
                    self.last_error = str(err)
                    is_server_error = _is_server_error(err)

                    if is_server_error and attempt < max_retries - 1:
                        _LOGGER.debug(
                            "Server error downloading %s (attempt %d/%d): %s, retrying in %ds",
                            dataset_entry["name"],
                            attempt + 1,
                            max_retries,
                            err,
                            retry_delay,
                        )
                        await asyncio.sleep(retry_delay)
                        continue
                    elif is_server_error:
                        _LOGGER.debug(
                            "Server error downloading %s after %d attempts: %s, trying previous dataset",
                            dataset_entry["name"],
                            max_retries,
                            err,
                        )
                        break
                    else:
                        _LOGGER.debug(
                            "Error downloading %s: %s", dataset_entry["name"], err
                        )
                        break

            if last_error is None:
                break

            if last_error and not _is_server_error(last_error):
                break

        # If all downloads failed
        if last_error:
            is_server_error = _is_server_error(last_error)
            self.last_error = str(last_error)
            self.status_label = "download_failed"
            if is_server_error:
                self._note_server_error()
                self.status_label = "server_error"
            else:
                self.update_interval = RETRY_INTERVAL
            if self.data:
                # Subsequent refresh: keep previous data on failure
                _LOGGER.debug(
                    "Could not download any dataset (last error: %s), keeping previous data",
                    last_error,
                )
                if not is_server_error:
                    self._reschedule(listing)
                return self.data
            # First load failure: raise so HA retries setup
            _LOGGER.error(
                "Could not download any dataset on first load: %s. Will retry in %s.",
                last_error,
                RETRY_INTERVAL,
            )
            raise UpdateFailed(
                f"Failed to download dataset on first load: {last_error}"
            ) from last_error

        self._reset_server_error_backoff()
        self.status_label = "ok"
        self.empty_snapshot_count = 0
        self.last_error = None
        self._reschedule(listing)

        # Merge new data with existing to preserve missing fields
        if self.data:
            merged = dict(self.data)
            merged.update(self.latest_dataset.points)
            return merged

        # First successful load
        return self.latest_dataset.points

    async def _async_list_with_refresh(self) -> list[dict]:
        """List datasets, self-healing a stale identifier once if needed.

        If the user deletes and recreates the continuous data subscription on
        the portal, the backend assigns a new identifier and the stored one
        stops working (the list errors or returns no files). Re-fetch the
        identifier from the metadata endpoint and retry once before giving up —
        so it recovers on the next cycle without needing a manual reload.
        """
        self._listing_failed_server_error = False

        # Use fewer, faster retries during initial setup
        max_retries = 3 if self._is_initial_setup else 5
        retry_delay = 3 if self._is_initial_setup else 5

        for identifier_retry in (False, True):
            last_error = None

            for attempt in range(max_retries):
                try:
                    listing = await self.client.async_list_datasets(
                        self.vin, self.identifier
                    )
                    # Empty listing might mean subscription was recreated
                    if (
                        not listing
                        and not identifier_retry
                        and await self._refresh_identifier()
                    ):
                        _LOGGER.info(
                            "Empty listing, retrying with refreshed identifier"
                        )
                        break  # Break inner loop to retry with new identifier
                    self._reset_server_error_backoff()
                    return listing

                except AuthError as err:
                    self.update_interval = RETRY_INTERVAL
                    self.status_label = "auth_failed"
                    self.last_error = str(err)
                    raise UpdateFailed(f"Authentication failed: {err}") from err

                except ApiError as err:
                    last_error = err
                    self.last_error = str(err)
                    is_server_error = _is_server_error(err)

                    # Retry server errors with delay
                    if is_server_error and attempt < max_retries - 1:
                        _LOGGER.debug(
                            "Server error listing datasets (attempt %d/%d): %s, retrying in %ds",
                            attempt + 1,
                            max_retries,
                            err,
                            retry_delay,
                        )
                        await asyncio.sleep(retry_delay)
                        continue

            # After all retries, try refreshing identifier once if not already tried
            if last_error and not identifier_retry and await self._refresh_identifier():
                _LOGGER.info("Retrying list with refreshed identifier after failures")
                continue

            # All attempts failed
            if last_error:
                is_server_error = _is_server_error(last_error)
                if is_server_error:
                    self._note_server_error()
                else:
                    self.update_interval = RETRY_INTERVAL

                # HTTP 400 special case
                if "HTTP 400" in str(last_error):
                    self.status_label = "delivery_not_ready"
                    raise UpdateFailed(
                        "Data delivery not ready yet (HTTP 400). If you just enabled "
                        "the continuous data request on the portal, it can take a few "
                        "hours to start; will keep retrying."
                    ) from last_error

                # Server errors with existing data - return empty to keep old data
                if is_server_error and self.data:
                    self._listing_failed_server_error = True
                    self.status_label = "listing_failed"
                    _LOGGER.error(
                        "Failed to list datasets after %d attempts: %s. Keeping previous data.",
                        max_retries,
                        last_error,
                    )
                    return []

                # Other errors or first load: raise UpdateFailed
                self.status_label = "listing_failed"
                raise UpdateFailed(str(last_error)) from last_error

        return []

    async def _refresh_identifier(self) -> bool:
        """Re-fetch the data-request identifier; persist it if it changed.

        Returns True (and updates the config entry) when the portal has handed
        out a new identifier, e.g. after the subscription was recreated.
        """
        try:
            meta = await self.client.async_get_metadata(self.vin)
        except ApiError as err:
            _LOGGER.debug("Could not refresh data-request identifier: %s", err)
            return False
        new_id = meta.get("Identifier") or meta.get("identifier")
        if not new_id or new_id == self.identifier:
            return False
        _LOGGER.warning(
            "Data-request identifier changed (%s -> %s); the portal subscription "
            "was likely recreated. Updating the config entry.",
            self.identifier,
            new_id,
        )
        self.identifier = new_id
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_IDENTIFIER: new_id}
        )
        return True

    def _reschedule(self, listing: list[dict]) -> None:
        """Schedule the next poll for ~15 min after the newest known dataset.

        If that time has already passed (a new dataset is due but not yet
        present), poll every minute until it appears.
        """
        timestamps = [ts for e in listing if (ts := _created_on(e))]
        newest = max(timestamps) if timestamps else None
        if newest:
            target = newest + DATASET_INTERVAL + POST_DATASET_BUFFER
            delta = target - dt_util.utcnow()
            if delta > MIN_INTERVAL:
                self.update_interval = delta
                _LOGGER.debug("Next refresh in %s (after newest %s)", delta, newest)
                return
        # newest dataset is overdue (or unknown) -> short retry for the next drop
        self.update_interval = RETRY_INTERVAL
        _LOGGER.debug("Next dataset overdue; retrying in %s", RETRY_INTERVAL)

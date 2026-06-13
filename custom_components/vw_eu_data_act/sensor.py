"""Sensor platform: curated sensors + raw diagnostic data points."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EudaConfigEntry
from .const import raw_unique_id
from .coordinator import EudaCoordinator
from .data import (
    CURATED_BINARY_DOTTED,
    CURATED_BINARY_FLAT,
    CURATED_SENSORS_DOTTED,
    CURATED_SENSORS_FLAT,
    UNIT_RESOLVERS,
    CuratedSensor,
    DataPoint,
    detect_dataset_format,
    format_curated_value,
    friendly_name,
    localize_label,
    resolve_distance_unit,
)
from .entity import EudaEntity

_STATUS_LABELS: dict[str, dict[str, str]] = {
    "de": {
        "starting": "Startet",
        "updating": "Aktualisiert",
        "ok": "OK",
        "waiting_for_portal_data": "Warten auf Portaldaten",
        "empty_snapshots": "Leere Snapshots",
        "delivery_not_ready": "Datenlieferung noch nicht bereit",
        "listing_failed": "Datenliste fehlgeschlagen",
        "auth_failed": "Authentifizierung fehlgeschlagen",
        "server_error": "Serverfehler",
        "download_failed": "Download fehlgeschlagen",
    },
    "fr": {
        "starting": "Demarrage",
        "updating": "Mise a jour",
        "ok": "OK",
        "waiting_for_portal_data": "En attente des donnees du portail",
        "empty_snapshots": "Instantanes vides",
        "delivery_not_ready": "Livraison des donnees non prete",
        "listing_failed": "Echec de recuperation des donnees",
        "auth_failed": "Echec d'authentification",
        "server_error": "Erreur serveur",
        "download_failed": "Echec du telechargement",
    },
    "it": {
        "starting": "Avvio",
        "updating": "Aggiornamento",
        "ok": "OK",
        "waiting_for_portal_data": "In attesa dei dati dal portale",
        "empty_snapshots": "Snapshot vuoti",
        "delivery_not_ready": "Consegna dati non pronta",
        "listing_failed": "Recupero elenco dati non riuscito",
        "auth_failed": "Autenticazione non riuscita",
        "server_error": "Errore server",
        "download_failed": "Download non riuscito",
    },
    "nl": {
        "starting": "Starten",
        "updating": "Bijwerken",
        "ok": "OK",
        "waiting_for_portal_data": "Wachten op portaalgegevens",
        "empty_snapshots": "Lege snapshots",
        "delivery_not_ready": "Gegevenslevering nog niet klaar",
        "listing_failed": "Lijst ophalen mislukt",
        "auth_failed": "Authenticatie mislukt",
        "server_error": "Serverfout",
        "download_failed": "Download mislukt",
    },
    "es": {
        "starting": "Iniciando",
        "updating": "Actualizando",
        "ok": "OK",
        "waiting_for_portal_data": "Esperando datos del portal",
        "empty_snapshots": "Instantaneas vacias",
        "delivery_not_ready": "Entrega de datos no preparada",
        "listing_failed": "Error al listar datos",
        "auth_failed": "Autenticacion fallida",
        "server_error": "Error del servidor",
        "download_failed": "Descarga fallida",
    },
}


def _language_key(language: str | None) -> str:
    lang = (language or "").lower()
    if lang.startswith("de"):
        return "de"
    if lang.startswith("fr"):
        return "fr"
    if lang.startswith("it"):
        return "it"
    if lang.startswith("nl"):
        return "nl"
    if lang.startswith("es"):
        return "es"
    return "en"


def _humanize_status(value: str) -> str:
    text = re.sub(r"_+", " ", value.strip().lower())
    text = re.sub(r"\s+", " ", text)
    return text.capitalize() if text else value


def _format_status_label(value: str, language: str | None) -> str:
    labels = _STATUS_LABELS.get(_language_key(language), {})
    return labels.get(value, _humanize_status(value))


def _parse_timestamp_value(raw_value: Any) -> datetime | None:
    """Parse timestamp values from dataset payloads.

    Some fields expose timestamps in the datapoint value itself (epoch millis or
    ISO string) while others only carry timestampUtc metadata.
    """
    if raw_value is None:
        return None

    if isinstance(raw_value, datetime):
        return raw_value

    if isinstance(raw_value, (int, float)):
        value = int(raw_value)
        if value >= 10**12:
            try:
                return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
            except (ValueError, OSError):
                return None
        return None

    if isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return None
        if text.isdigit() and len(text) >= 12:
            try:
                return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
            except (ValueError, OSError):
                return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EudaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = [EudaStatusSensor(coordinator)]
    added_curated_fields: set[str] = set()
    added_raw_keys: set[str] = set()

    def _collect_new_entities() -> list[SensorEntity]:
        points: dict[str, DataPoint] = coordinator.data or {}
        if not points:
            return []

        present_fields = {dp.field_name for dp in points.values()}

        # Detect dataset format and select appropriate curated group
        format_type = detect_dataset_format(points)
        curated_sensors = (
            CURATED_SENSORS_DOTTED if format_type == "dotted" else CURATED_SENSORS_FLAT
        )
        curated_binary = (
            CURATED_BINARY_DOTTED if format_type == "dotted" else CURATED_BINARY_FLAT
        )

        # Build field sets for exclusion from raw sensors
        binary_fields = {b.field_name for b in curated_binary}
        curated_sensor_fields = {s.field_name for s in curated_sensors}

        new_entities: list[SensorEntity] = []

        # curated numeric / text sensors (one per field, if present)
        for curated in curated_sensors:
            if curated.field_name in added_curated_fields:
                continue
            # Special handling for timestamp sensors (e.g., "mileage.timestamp" or "mileage.value.timestamp")
            if ".timestamp" in curated.field_name:
                base_field = curated.field_name.replace(".timestamp", "")
                if base_field in present_fields:
                    new_entities.append(EudaCuratedSensor(coordinator, curated))
                    added_curated_fields.add(curated.field_name)
            elif curated.field_name in present_fields:
                new_entities.append(EudaCuratedSensor(coordinator, curated))
                added_curated_fields.add(curated.field_name)

        # raw diagnostic sensors: every other unique key
        for key, dp in points.items():
            if key in added_raw_keys:
                continue
            if dp.field_name in curated_sensor_fields or dp.field_name in binary_fields:
                continue
            new_entities.append(EudaRawSensor(coordinator, key))
            added_raw_keys.add(key)

        return new_entities

    entities.extend(_collect_new_entities())
    async_add_entities(entities)

    @callback
    def _handle_coordinator_update() -> None:
        new_entities = _collect_new_entities()
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_handle_coordinator_update))


def _find_by_field(points: dict[str, DataPoint], field_name: str) -> DataPoint | None:
    """Pick a single point for a (possibly duplicated) field name.

    The portal's flat array is unordered and a field can appear multiple times
    under different UUIDs with conflicting values, with no way to tell which is
    "live". Select the smallest UUID: arbitrary but stable, so the sensor tracks
    the same data point across refreshes instead of flip-flopping on reshuffle.
    """
    matches = [dp for dp in points.values() if dp.field_name == field_name]
    return min(matches, key=lambda dp: dp.key) if matches else None


class EudaCuratedSensor(EudaEntity, SensorEntity):
    """A curated, well-typed sensor (enabled by default)."""

    def __init__(self, coordinator: EudaCoordinator, curated: CuratedSensor) -> None:
        super().__init__(coordinator)
        self._curated = curated
        self._attr_unique_id = f"{coordinator.vin}_{curated.field_name}"
        self._attr_name = localize_label(curated.name, coordinator.hass.config.language)
        if curated.icon:
            self._attr_icon = curated.icon
        if curated.device_class:
            self._attr_device_class = SensorDeviceClass(curated.device_class)
        if curated.state_class:
            self._attr_state_class = SensorStateClass(curated.state_class)
        if curated.suggested_display_precision is not None:
            self._attr_suggested_display_precision = curated.suggested_display_precision

    def _apply_transform(self, value):
        """Apply configured transform to the raw value."""
        if value is None or not self._curated.transform:
            return value

        transform = self._curated.transform

        if transform == "duration_s":
            # Already handled by parse_duration_seconds in parse_value
            return value

        if transform == "decikelvin_to_celsius":
            from .data import decikelvin_to_celsius

            return decikelvin_to_celsius(str(value))

        return value

    @property
    def native_value(self):
        # Special handling for timestamp fields (both "mileage.timestamp" and "mileage.value.timestamp")
        if ".timestamp" in self._curated.field_name:
            base_field = self._curated.field_name.replace(".timestamp", "")
            dp = _find_by_field(self.coordinator.data or {}, base_field)
            if dp:
                # Prefer the transport timestamp, but fall back to value-encoded
                # timestamps used by some dataset variants.
                parsed = dp.timestamp or _parse_timestamp_value(dp.raw_value)
                if parsed:
                    return self._sticky(parsed)
            return self._sticky(None)

        dp = _find_by_field(self.coordinator.data or {}, self._curated.field_name)

        if not dp:
            return self._sticky(None)

        raw_value = dp.value

        # Apply transforms if specified
        if self._curated.transform:
            if self._curated.transform == "decikelvin_to_celsius":
                from .data import decikelvin_to_celsius

                transformed = decikelvin_to_celsius(dp.raw_value)
                return self._sticky(transformed)

            elif self._curated.transform == "abs":
                from .data import abs_value

                transformed = abs_value(raw_value)
                return self._sticky(transformed)

            elif self._curated.transform == "fuel_consumption":
                from .data import fuel_consumption_l_per_1000km_to_l_per_100km

                transformed = fuel_consumption_l_per_1000km_to_l_per_100km(raw_value)
                return self._sticky(transformed)

        language = self.hass.config.language if self.hass else None
        return self._sticky(
            format_curated_value(
                self._curated.field_name,
                raw_value,
                language=language,
            )
        )

    @property
    def native_unit_of_measurement(self) -> str | None:
        # When a companion unit field is declared (e.g. mileage.unit), resolve
        # the unit at runtime so miles vs km is reported correctly per vehicle;
        # otherwise use the static curated unit.
        cur = self._curated
        if cur.unit_field:
            dp = _find_by_field(self.coordinator.data or {}, cur.unit_field)
            if dp is not None:
                resolver = UNIT_RESOLVERS.get(cur.unit_resolver, resolve_distance_unit)
                resolved = resolver(dp.value)
                if resolved:
                    return resolved
        return cur.unit


class EudaRawSensor(EudaEntity, SensorEntity):
    """A raw data point exposed as a disabled-by-default diagnostic sensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: EudaCoordinator, key: str) -> None:
        super().__init__(coordinator)
        dp = coordinator.data[key]
        self._key = key
        # Namespace by VIN: dataset keys are shared across vehicles, so a bare
        # key collides between config entries (see raw_unique_id / migration).
        self._attr_unique_id = raw_unique_id(coordinator.vin, key)
        self._attr_name = friendly_name(dp.field_name, dp.description)
        # only attach a unit when the value is numeric
        if dp.unit and dp.type_hint in ("int", "float"):
            self._attr_native_unit_of_measurement = dp.unit

    @property
    def native_value(self):
        dp = (self.coordinator.data or {}).get(self._key)
        return self._sticky(dp.value if dp else None)

    @property
    def extra_state_attributes(self) -> dict:
        dp = (self.coordinator.data or {}).get(self._key)
        if not dp:
            return {}
        attrs = {"key": dp.key, "field_name": dp.field_name}
        if dp.description:
            attrs["description"] = dp.description
        if dp.cluster:
            attrs["cluster"] = dp.cluster
        return attrs


class EudaStatusSensor(EudaEntity, SensorEntity):
    """Integration health/status sensor that is available even before first data."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EudaCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_integration_status"
        self._attr_name = localize_label("Status", coordinator.hass.config.language)
        self._attr_icon = "mdi:information-outline"

    @property
    def available(self) -> bool:
        """Status should remain visible regardless of data availability."""
        return True

    @property
    def native_value(self):
        language = self.hass.config.language if self.hass else None
        return _format_status_label(self.coordinator.status_label, language)

    @property
    def extra_state_attributes(self) -> dict:
        language = self.hass.config.language if self.hass else None
        attrs: dict[str, Any] = {
            "status_code": self.coordinator.status_label,
            "language": _language_key(language),
            "update_interval_seconds": int(self.coordinator.update_interval.total_seconds()),
            "empty_snapshot_count": self.coordinator.empty_snapshot_count,
            "consecutive_server_errors": getattr(self.coordinator, "_consecutive_server_errors", 0),
        }
        if self.coordinator.last_error:
            attrs["last_error"] = self.coordinator.last_error
        if self.coordinator.latest_dataset and self.coordinator.latest_dataset.captured_at:
            attrs["captured_at"] = self.coordinator.latest_dataset.captured_at.isoformat()
        return attrs

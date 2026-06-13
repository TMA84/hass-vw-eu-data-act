"""Utility meter helper auto-provisioning for curated EU Data Act sensors."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import SOURCE_USER, ConfigEntry
from homeassistant.const import CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from homeassistant.components.utility_meter.const import (
    CONF_METER_DELTA_VALUES,
    CONF_METER_NET_CONSUMPTION,
    CONF_METER_OFFSET,
    CONF_METER_PERIODICALLY_RESETTING,
    CONF_METER_TYPE,
    CONF_SENSOR_ALWAYS_AVAILABLE,
    CONF_SOURCE_SENSOR,
    CONF_TARIFFS,
    DOMAIN as UTILITY_METER_DOMAIN,
    MONTHLY,
)

from .const import CONF_NICKNAME, CONF_VIN

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class UtilityMeterSpec:
    """How to create one monthly utility meter helper."""

    helper_name_suffix: str
    source_field_candidates: tuple[str, ...]
    delta_values: bool = False


_AUTO_METERS: tuple[UtilityMeterSpec, ...] = (
    UtilityMeterSpec(
        helper_name_suffix="Monthly charged energy",
        source_field_candidates=(
            "battery_state_report.charge_energy",
            "charged_energy",
        ),
    ),
    UtilityMeterSpec(
        helper_name_suffix="Monthly mileage",
        source_field_candidates=(
            "mileage.value",
            "mileage",
        ),
    ),
)


def utility_meter_helper_name(entry: ConfigEntry, helper_name_suffix: str) -> str:
    """Return a stable helper title that remains unique across vehicles."""
    label = entry.data.get(CONF_NICKNAME) or entry.data[CONF_VIN]
    return f"{label} {helper_name_suffix}"


def _source_entity_id(
    entities_by_unique_id: dict[str, str],
    vin: str,
    spec: UtilityMeterSpec,
) -> str | None:
    for field_name in spec.source_field_candidates:
        entity_id = entities_by_unique_id.get(f"{vin}_{field_name}")
        if entity_id:
            return entity_id
    return None


def _already_exists(
    hass: HomeAssistant,
    helper_name: str,
    source_entity_id: str,
    meter_type: str,
) -> bool:
    for meter_entry in hass.config_entries.async_entries(UTILITY_METER_DOMAIN):
        source = meter_entry.options.get(CONF_SOURCE_SENSOR)
        cycle = meter_entry.options.get(CONF_METER_TYPE)
        if source == source_entity_id and cycle == meter_type:
            return True
        if meter_entry.title == helper_name:
            return True
    return False


async def async_ensure_utility_meters(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Create monthly utility_meter helpers for key curated sensors if missing."""
    registry = er.async_get(hass)
    sensor_entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    entities_by_unique_id = {
        reg_entry.unique_id: reg_entry.entity_id
        for reg_entry in sensor_entries
        if reg_entry.domain == Platform.SENSOR
    }
    vin = entry.data[CONF_VIN]

    for spec in _AUTO_METERS:
        source_entity_id = _source_entity_id(entities_by_unique_id, vin, spec)
        if not source_entity_id:
            continue

        helper_name = utility_meter_helper_name(entry, spec.helper_name_suffix)
        if _already_exists(hass, helper_name, source_entity_id, MONTHLY):
            continue

        flow_data = {
            CONF_NAME: helper_name,
            CONF_SOURCE_SENSOR: source_entity_id,
            CONF_METER_TYPE: MONTHLY,
            CONF_METER_OFFSET: 0,
            CONF_TARIFFS: [],
            CONF_METER_NET_CONSUMPTION: False,
            CONF_METER_DELTA_VALUES: spec.delta_values,
            CONF_METER_PERIODICALLY_RESETTING: True,
            CONF_SENSOR_ALWAYS_AVAILABLE: False,
        }
        _LOGGER.info(
            "Creating utility meter helper '%s' from %s",
            helper_name,
            source_entity_id,
        )
        result = await hass.config_entries.flow.async_init(
            UTILITY_METER_DOMAIN,
            context={"source": SOURCE_USER},
            data=flow_data,
        )
        if result.get("type") not in {"create_entry", "abort"}:
            _LOGGER.warning(
                "Unexpected utility_meter flow result for '%s': %s",
                helper_name,
                result.get("type"),
            )

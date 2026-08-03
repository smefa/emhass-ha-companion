"""Thermal loads end to end: the add/edit flow and the registry projection.

A thermal load is created through its own subentry type -- its own "Add
thermal load" button -- and the registry must recognise it, seed its comfort
band, and read the room temperature live when a payload is built.
"""

from __future__ import annotations

from datetime import time

from homeassistant.config_entries import SOURCE_USER, ConfigSubentryData
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.const import (
    CONF_COMFORT_END,
    CONF_COMFORT_START,
    CONF_COMFORT_TEMPERATURE,
    CONF_COOLING_CONSTANT,
    CONF_HEATING_RATE,
    CONF_MAX_TEMPERATURE,
    CONF_NAME,
    CONF_NOMINAL_POWER,
    CONF_SENSE,
    CONF_SETBACK_TEMPERATURE,
    CONF_TEMPERATURE_SENSOR,
    CONF_TIME_STEP,
    DOMAIN,
    SUBENTRY_TYPE_THERMAL,
)
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.thermal import SENSE_COOL, SENSE_HEAT

ROOM_SENSOR = "sensor.living_room_temperature"


def _entry(*, subentries: list[ConfigSubentryData] | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        options={CONF_TIME_STEP: 30},
        subentries_data=subentries or [],
    )


def _thermal_subentry(**data) -> ConfigSubentryData:
    return ConfigSubentryData(
        subentry_type=SUBENTRY_TYPE_THERMAL,
        title="Heat pump",
        unique_id="heat-pump",
        data={CONF_NAME: "Heat pump", CONF_NOMINAL_POWER: 3000, **data},
    )


# --- the add flow ------------------------------------------------------------


async def test_a_thermal_load_can_be_added_through_its_own_flow(hass: HomeAssistant) -> None:
    hass.states.async_set(ROOM_SENSOR, "20.5", {"device_class": "temperature"})
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_THERMAL), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Heat pump",
            CONF_TEMPERATURE_SENSOR: ROOM_SENSOR,
            CONF_SENSE: SENSE_HEAT,
            CONF_NOMINAL_POWER: 3000,
            CONF_COMFORT_TEMPERATURE: 21.0,
            CONF_SETBACK_TEMPERATURE: 18.0,
            CONF_MAX_TEMPERATURE: 24.0,
            CONF_COMFORT_START: "06:30:00",
            CONF_COMFORT_END: "23:00:00",
            CONF_HEATING_RATE: 3.0,
            CONF_COOLING_CONSTANT: 0.1,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Heat pump"
    assert result["data"][CONF_TEMPERATURE_SENSOR] == ROOM_SENSOR
    assert result["data"][CONF_COMFORT_TEMPERATURE] == 21.0


async def test_a_thermal_load_can_be_added_with_no_sensors_at_all(hass: HomeAssistant) -> None:
    """Everything optional blank: EMHASS then falls back to its own start
    temperature, which is degraded but workable."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_THERMAL), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Heat pump",
            CONF_SENSE: SENSE_HEAT,
            CONF_NOMINAL_POWER: 3000,
            CONF_COMFORT_TEMPERATURE: 21.0,
            CONF_SETBACK_TEMPERATURE: 18.0,
            CONF_MAX_TEMPERATURE: 24.0,
            CONF_COMFORT_START: "06:30:00",
            CONF_COMFORT_END: "23:00:00",
            CONF_HEATING_RATE: 3.0,
            CONF_COOLING_CONSTANT: 0.1,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_TEMPERATURE_SENSOR not in result["data"]


async def test_reconfigure_offers_identity_not_the_comfort_band(hass: HomeAssistant) -> None:
    """The comfort band became entities the moment the load was created;
    re-offering it here would show boxes the restored entities override."""
    entry = _entry(subentries=[_thermal_subentry(**{CONF_COMFORT_TEMPERATURE: 22.0})])
    entry.add_to_hass(hass)
    subentry_id = next(iter(entry.subentries))

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_THERMAL),
        context={"source": "reconfigure", "subentry_id": subentry_id},
    )

    offered = {str(key) for key in result["data_schema"].schema}
    assert CONF_TEMPERATURE_SENSOR in offered
    assert CONF_SENSE in offered
    assert CONF_COMFORT_TEMPERATURE not in offered
    assert CONF_NOMINAL_POWER not in offered

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Heat pump", CONF_SENSE: SENSE_COOL, CONF_TEMPERATURE_SENSOR: ROOM_SENSOR},
    )
    assert result["type"] is FlowResultType.ABORT
    subentry = entry.subentries[subentry_id]
    assert subentry.data[CONF_SENSE] == SENSE_COOL
    # The values the step no longer offers survive the update.
    assert subentry.data[CONF_COMFORT_TEMPERATURE] == 22.0


# --- the registry ------------------------------------------------------------


async def test_registry_seeds_a_thermal_load_from_its_subentry(hass: HomeAssistant) -> None:
    entry = _entry(
        subentries=[
            _thermal_subentry(
                **{
                    CONF_TEMPERATURE_SENSOR: ROOM_SENSOR,
                    CONF_SENSE: SENSE_HEAT,
                    CONF_COMFORT_TEMPERATURE: 22.0,
                    CONF_SETBACK_TEMPERATURE: 17.0,
                    CONF_MAX_TEMPERATURE: 25.0,
                    CONF_COMFORT_START: "07:00:00",
                    CONF_COMFORT_END: "21:00:00",
                    CONF_HEATING_RATE: 4.0,
                    CONF_COOLING_CONSTANT: 0.2,
                }
            )
        ]
    )
    entry.add_to_hass(hass)

    registry = DeferrableRegistry(hass, entry)
    registry.sync()

    (load,) = registry.all()
    assert load.is_thermal
    assert load.temperature_sensor == ROOM_SENSOR
    assert load.comfort_temperature == 22.0
    assert load.setback_temperature == 17.0
    assert load.max_temperature == 25.0
    assert load.comfort_start == time(7, 0)
    assert load.comfort_end == time(21, 0)
    assert load.heating_rate == 4.0
    assert load.cooling_constant == 0.2


async def test_the_room_temperature_is_read_live_at_projection_time(
    hass: HomeAssistant,
) -> None:
    entry = _entry(subentries=[_thermal_subentry(**{CONF_TEMPERATURE_SENSOR: ROOM_SENSOR})])
    entry.add_to_hass(hass)
    registry = DeferrableRegistry(hass, entry)
    registry.sync()

    hass.states.async_set(ROOM_SENSOR, "19.4", {"unit_of_measurement": UnitOfTemperature.CELSIUS})
    (load,) = registry.to_loads(dt_util.utcnow(), 30)
    assert load.thermal is not None
    assert load.thermal.current_temperature == 19.4


async def test_a_fahrenheit_room_sensor_is_converted_to_celsius(hass: HomeAssistant) -> None:
    """EMHASS's thermal model assumes Celsius; 68 °F read raw would demand a
    68-degree living room."""
    entry = _entry(subentries=[_thermal_subentry(**{CONF_TEMPERATURE_SENSOR: ROOM_SENSOR})])
    entry.add_to_hass(hass)
    registry = DeferrableRegistry(hass, entry)
    registry.sync()

    hass.states.async_set(
        ROOM_SENSOR, "68.0", {"unit_of_measurement": UnitOfTemperature.FAHRENHEIT}
    )
    (load,) = registry.to_loads(dt_util.utcnow(), 30)
    assert load.thermal.current_temperature == 20.0


async def test_an_unavailable_room_sensor_yields_no_start_temperature(
    hass: HomeAssistant,
) -> None:
    entry = _entry(subentries=[_thermal_subentry(**{CONF_TEMPERATURE_SENSOR: ROOM_SENSOR})])
    entry.add_to_hass(hass)
    registry = DeferrableRegistry(hass, entry)
    registry.sync()

    hass.states.async_set(ROOM_SENSOR, "unavailable")
    (load,) = registry.to_loads(dt_util.utcnow(), 30)
    assert load.thermal is not None
    assert load.thermal.current_temperature is None


async def test_standard_and_thermal_loads_share_one_stable_order(hass: HomeAssistant) -> None:
    """Both types live in the same registry and the same P_deferrable{k}
    numbering -- a thermal load is a deferrable load to EMHASS."""
    entry = _entry(
        subentries=[
            _thermal_subentry(),
            ConfigSubentryData(
                subentry_type="deferrable_load",
                title="Dishwasher",
                unique_id="dishwasher",
                data={CONF_NAME: "Dishwasher", CONF_NOMINAL_POWER: 2000},
            ),
        ]
    )
    entry.add_to_hass(hass)
    registry = DeferrableRegistry(hass, entry)
    registry.sync()

    names = [load.name for load in registry.all()]
    assert names == ["Dishwasher", "Heat pump"]
    assert registry.has_thermal
    projected = registry.to_loads(dt_util.utcnow(), 30)
    assert projected[0].thermal is None
    assert projected[1].thermal is not None

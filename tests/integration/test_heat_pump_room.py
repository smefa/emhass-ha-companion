"""Heat pump rooms: creation, editing and runtime projection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from homeassistant.config_entries import SOURCE_USER, ConfigSubentryData
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import DOMAIN, SUBENTRY_TYPE_THERMAL
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry, live_settings
from custom_components.emhass_companion.heat_pump_room import HeatPumpRoomConfig
from custom_components.emhass_companion.models import Point, Series
from custom_components.emhass_companion.number import LOAD_NUMBERS, LoadNumber

NOW = datetime(2026, 11, 3, 5, tzinfo=UTC)
SENSOR = "sensor.living_room_temperature"


def _stored(**overrides):
    return {
        "name": "House",
        "thermal_model": "heat_pump_room",
        "sense": "heat",
        "temperature_sensor": SENSOR,
        "nominal_power_w": 2000,
        "comfort_temperature": 21,
        "setback_temperature": 19,
        "max_temperature": 23,
        "comfort_start": "06:00:00",
        "comfort_end": "22:00:00",
        "thermal_mass_m3": 10,
        "floor_area_m2": 150,
        "specific_heating_demand": 100,
        "supply_temperature": 35,
        "carnot_efficiency": 0.4,
        "base_temperature": 18,
        "annual_reference_hdd": 3000,
        "thermal_inertia_time_constant": 0,
        **overrides,
    }


def _creation_data(advanced=None, **overrides):
    data = {
        "temperature_sensor": SENSOR,
        "nominal_power_w": 2000,
        "comfort_temperature": 21,
        "setback_temperature": 19,
        "max_temperature": 23,
        "comfort_start": "06:00:00",
        "comfort_end": "22:00:00",
        "thermal_mass_m3": 10,
        "floor_area_m2": 150,
        "specific_heating_demand": 100,
        "supply_temperature": 35,
        **overrides,
    }
    return {**data, "advanced": advanced or {}}


async def _start(hass, *, data=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[]
        if data is None
        else [
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_THERMAL, title="House", unique_id="house", data=data
            )
        ],
    )
    entry.add_to_hass(hass)
    context = (
        {"source": SOURCE_USER}
        if data is None
        else {"source": "reconfigure", "subentry_id": next(iter(entry.subentries))}
    )
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_THERMAL), context=context
    )
    return entry, result


async def test_creation_stores_the_model_flat(hass: HomeAssistant):
    _entry, result = await _start(hass)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "House", "thermal_model": "heat_pump_room"}
    )
    assert result["step_id"] == "heat_pump_room"
    fields = {str(key) for key in result["data_schema"].schema}
    assert {"heating_rate", "cooling_constant", "cop_curve", "sense", "name"}.isdisjoint(fields)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _creation_data({"carnot_efficiency": 0.5})
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["thermal_model"] == "heat_pump_room"
    assert data["sense"] == "heat"
    assert data["name"] == "House"
    assert data["carnot_efficiency"] == 0.5
    assert (data["base_temperature"], data["annual_reference_hdd"]) == (18, 3000)
    assert data["thermal_inertia_time_constant"] == 0
    assert "advanced" not in data


@pytest.mark.parametrize(
    "changes",
    [{"thermal_mass_m3": float("nan")}, {"floor_area_m2": 0}, {"temperature_sensor": ""}],
)
async def test_invalid_values_do_not_create(hass: HomeAssistant, changes):
    entry, result = await _start(hass)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "House", "thermal_model": "heat_pump_room"}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"], _creation_data(**changes)
        )
    assert not entry.subentries


async def test_band_order_is_a_form_error(hass: HomeAssistant):
    entry, result = await _start(hass)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "House", "thermal_model": "heat_pump_room"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _creation_data(setback_temperature=22)
    )
    assert result["errors"] == {"base": "invalid_heat_pump_band"}
    assert not entry.subentries


async def test_reconfigure_edits_the_model_and_keeps_entity_owned_values(hass: HomeAssistant):
    stored = _stored(power_sensor="sensor.heat_pump_power")
    entry, result = await _start(hass, data=stored)
    assert result["step_id"] == "reconfigure_heat_pump_room"
    fields = {str(key) for key in result["data_schema"].schema}
    assert "nominal_power_w" not in fields
    assert "comfort_temperature" not in fields
    assert {"name", "thermal_mass_m3", "supply_temperature", "advanced"} <= fields
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Whole house",
            "temperature_sensor": SENSOR,
            "thermal_mass_m3": 12,
            "floor_area_m2": 150,
            "specific_heating_demand": 80,
            "supply_temperature": 45,
            "advanced": {"thermal_inertia_time_constant": 2},
        },
    )
    assert result["type"] is FlowResultType.ABORT
    updated = next(iter(entry.subentries.values())).data
    assert updated["name"] == "Whole house"
    assert (updated["thermal_mass_m3"], updated["supply_temperature"]) == (12, 45)
    assert updated["thermal_inertia_time_constant"] == 2
    assert updated["nominal_power_w"] == 2000
    assert updated["comfort_temperature"] == 21
    assert "power_sensor" not in updated
    assert updated["thermal_model"] == "heat_pump_room"


async def test_reconfigure_stores_and_clears_the_fixed_cop_settings(hass: HomeAssistant):
    stored = _stored(supply_temperature=35, source_temperature=30, constant_cop=4.2)
    entry, result = await _start(hass, data=stored)
    form = {
        "name": "House",
        "temperature_sensor": SENSOR,
        "thermal_mass_m3": 10,
        "floor_area_m2": 150,
        "specific_heating_demand": 100,
        # Lower than the stored source temperature: valid only once that is cleared.
        "supply_temperature": 25,
        "advanced": {},
    }
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], form)
    assert result["type"] is FlowResultType.ABORT
    updated = next(iter(entry.subentries.values())).data
    assert "constant_cop" not in updated
    assert "source_temperature" not in updated

    _entry, result = await _start(hass, data=_stored())
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**form, "advanced": {"source_temperature": 0, "constant_cop": 4.5}}
    )
    assert result["type"] is FlowResultType.ABORT


async def test_reconfigure_stores_and_clears_the_solar_gain_settings(hass: HomeAssistant):
    entry, result = await _start(hass, data=_stored())
    form = {
        "name": "House",
        "temperature_sensor": SENSOR,
        "thermal_mass_m3": 10,
        "floor_area_m2": 150,
        "specific_heating_demand": 100,
        "supply_temperature": 35,
    }
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**form, "advanced": {"window_area": 18, "shgc": 0.5}}
    )
    assert result["type"] is FlowResultType.ABORT
    stored = next(iter(entry.subentries.values())).data
    assert (stored["window_area"], stored["shgc"]) == (18, 0.5)

    # A blank area clears it; the coefficient keeps its stored value, as the form's default.
    _entry, result = await _start(hass, data=stored)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**form, "advanced": {}}
    )
    assert result["type"] is FlowResultType.ABORT
    cleared = next(iter(_entry.subentries.values())).data
    assert "window_area" not in cleared
    assert cleared["shgc"] == 0.5


def _coordinator(hass: HomeAssistant, **overrides) -> EmhassCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_THERMAL,
                "title": "House",
                "unique_id": "house",
                "data": _stored(**overrides),
            }
        ],
    )
    entry.add_to_hass(hass)
    registry = DeferrableRegistry(hass, entry)
    registry.sync()
    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), registry)
    coordinator.backend_version = "0.18.8"
    hass.states.async_set(SENSOR, "20.5", {"unit_of_measurement": UnitOfTemperature.CELSIUS})
    return coordinator


async def test_projection_builds_a_heat_pump_room(hass: HomeAssistant):
    coordinator = _coordinator(hass)
    (runtime,) = coordinator.loads.all()
    assert runtime.is_heat_pump_room
    assert not runtime.is_measured_room
    assert coordinator.loads.needs_outdoor_temperature
    (projected,) = coordinator.deferrable_loads(NOW)
    assert isinstance(projected.thermal, HeatPumpRoomConfig)
    assert projected.thermal.current_temperature == 20.5
    assert projected.thermal.model.thermal_mass_m3 == 10
    settings = live_settings(runtime)
    assert not {"heating_rate", "cooling_constant", "thermal_inertia"} & settings.keys()
    numbers = {
        description.key
        for description in LOAD_NUMBERS
        if description.applies_fn is None or description.applies_fn(runtime)
    }
    assert not {"heating_rate", "cooling_constant", "thermal_inertia"} & numbers
    assert "comfort_temperature" in numbers


async def test_incomplete_model_fails_the_update(hass: HomeAssistant):
    coordinator = _coordinator(hass, thermal_mass_m3=None)
    with pytest.raises(UpdateFailed, match="heat pump model settings are incomplete"):
        coordinator.deferrable_loads(NOW)


async def test_effective_config_uses_the_fetched_outdoor_forecast(hass: HomeAssistant):
    coordinator = _coordinator(hass)
    (runtime,) = coordinator.loads.all()
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    coordinator._outdoor_forecast = Series(
        Point(start + timedelta(minutes=30 * index), -5.0) for index in range(-2, 100)
    )
    config = coordinator.effective_thermal_config(runtime, 20.5)
    assert isinstance(config, HeatPumpRoomConfig)
    assert config.outdoor
    assert set(config.outdoor) == {-5.0}


async def test_disabled_room_with_incomplete_model_blocks_no_plan(hass: HomeAssistant):
    coordinator = _coordinator(hass, thermal_mass_m3=None)
    (runtime,) = coordinator.loads.all()
    runtime.enabled = False
    (projected,) = coordinator.deferrable_loads(NOW)
    assert projected.thermal is None


async def test_band_numbers_reject_an_out_of_order_band(hass: HomeAssistant):
    coordinator = _coordinator(hass)
    (runtime,) = coordinator.loads.all()
    description = next(item for item in LOAD_NUMBERS if item.key == "comfort_temperature")
    number = LoadNumber(coordinator, runtime, description)
    with pytest.raises(ServiceValidationError):
        await number.async_set_native_value(24)
    assert runtime.comfort_temperature == 21

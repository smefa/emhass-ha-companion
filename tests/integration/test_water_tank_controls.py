"""Tank number restoration and fail-closed control dispatch."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.binary_sensor import LoadShouldRunBinarySensor
from custom_components.emhass_companion.const import (
    DOMAIN,
    LOAD_MODE_AUTO,
    LOAD_MODE_FORCE_ON,
    MODE_SELF_CONSUME,
    SUBENTRY_TYPE_THERMAL,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry, live_settings
from custom_components.emhass_companion.entity import EmhassLoadEntity
from custom_components.emhass_companion.executor import Decision, Executor
from custom_components.emhass_companion.number import LOAD_NUMBERS, LoadNumber

TEMPERATURE = "sensor.tank_temperature"
CONTROL = "switch.tank_heater"


def _tank(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_THERMAL,
                "title": "Tank",
                "unique_id": "tank",
                "data": {
                    "thermal_model": "water_tank",
                    "tank_volume_litres": 200,
                    "nominal_power_w": 2000,
                    "standby_loss_w": 50,
                    "draw_start": "07:00:00",
                    "draw_end": "08:00:00",
                    "draw_energy_kwh": 0,
                    "comfort_temperature": 45,
                    "setback_temperature": 40,
                    "max_temperature": 60,
                    "temperature_sensor": TEMPERATURE,
                    "control_entity": CONTROL,
                },
            }
        ],
    )
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    client = AsyncMock(spec=EmhassClient)
    client.base_url = "http://localhost:5000"
    coordinator = EmhassCoordinator(hass, entry, client, loads)
    coordinator.backend_version = "0.18.8"
    coordinator.async_request_settings_refresh = AsyncMock()
    load = loads.get(next(iter(entry.subentries)))
    hass.states.async_set(TEMPERATURE, "45", {"unit_of_measurement": UnitOfTemperature.CELSIUS})
    hass.states.async_set(CONTROL, "off")
    assert coordinator.water_tank_error(load) is None
    numbers = {
        description.key: LoadNumber(coordinator, load, description)
        for description in LOAD_NUMBERS
        if description.applies_fn is None or description.applies_fn(load)
    }
    return coordinator, load, numbers


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("setback_temperature", 46),
        ("comfort_temperature", 39),
        ("comfort_temperature", 61),
        ("max_temperature", 44),
        ("comfort_temperature", float("nan")),
    ],
)
async def test_band_changes_reject_invalid_order_without_mutating(hass, key, value):
    coordinator, load, numbers = _tank(hass)
    before = numbers[key].native_value
    with pytest.raises(ServiceValidationError):
        await numbers[key].async_set_native_value(value)
    assert numbers[key].native_value == before
    coordinator.async_request_settings_refresh.assert_not_awaited()
    assert (load.setback_temperature, load.comfort_temperature, load.max_temperature) == (
        40,
        45,
        60,
    )


async def test_raise_ceiling_before_comfort_and_setback(hass):
    coordinator, load, numbers = _tank(hass)
    with patch.object(numbers["max_temperature"], "_check_thermal_reachability") as check:
        await numbers["max_temperature"].async_set_native_value(70)
        check.assert_not_called()
    await numbers["comfort_temperature"].async_set_native_value(65)
    await numbers["setback_temperature"].async_set_native_value(65)
    assert (load.setback_temperature, load.comfort_temperature, load.max_temperature) == (
        65,
        65,
        70,
    )
    assert coordinator.async_request_settings_refresh.await_count == 3


async def test_standby_loss_uses_restored_native_value_and_stable_id(hass):
    coordinator, load, numbers = _tank(hass)
    number = numbers["standby_loss"]
    assert number.native_value == 50
    assert number.native_min_value == 0
    assert number.native_max_value == 1000
    assert number.native_step == 1
    assert number.native_unit_of_measurement == "W"
    assert (
        number.unique_id == f"{coordinator.config_entry.entry_id}_{load.subentry_id}_standby_loss"
    )
    with (
        patch.object(EmhassLoadEntity, "async_added_to_hass", new=AsyncMock()),
        patch.object(
            number,
            "async_get_last_number_data",
            new=AsyncMock(return_value=SimpleNamespace(native_value=123)),
        ),
    ):
        await number.async_added_to_hass()
    assert load.standby_loss_w == 123
    assert number.native_value == 123
    await number.async_set_native_value(0)
    assert load.standby_loss_w == 0


async def test_heater_cop_and_comfort_penalty_numbers_seed_restore_and_set(hass):
    _, load, numbers = _tank(hass)
    cop, penalty = numbers["heater_cop"], numbers["comfort_penalty"]
    # Subentry data without them means an immersion element with hard comfort.
    assert (cop.native_value, penalty.native_value) == (1.0, 0.0)
    assert (cop.native_min_value, cop.native_max_value) == (0.1, 10)
    assert (penalty.native_min_value, penalty.native_max_value) == (0, 1000)
    assert cop.unique_id.endswith(f"_{load.subentry_id}_heater_cop")
    with patch.object(EmhassLoadEntity, "async_added_to_hass", new=AsyncMock()):
        for number, value in ((cop, 3.0), (penalty, 12.5)):
            with patch.object(
                number,
                "async_get_last_number_data",
                new=AsyncMock(return_value=SimpleNamespace(native_value=value)),
            ):
                await number.async_added_to_hass()
    assert (load.heater_cop, load.comfort_penalty) == (3.0, 12.5)
    live = live_settings(load)
    assert (live["heater_cop"], live["comfort_penalty"]) == (3.0, 12.5)
    await cop.async_set_native_value(2.5)
    await penalty.async_set_native_value(0)
    assert (load.heater_cop, load.comfort_penalty) == (2.5, 0)


async def test_room_has_no_heater_cop_or_comfort_penalty(hass):
    _, load, _ = _tank(hass)
    load.thermal_model = "room"
    keys = {
        description.key
        for description in LOAD_NUMBERS
        if description.applies_fn is None or description.applies_fn(load)
    }
    assert not {"heater_cop", "comfort_penalty"} & keys


async def test_restore_bands_does_not_validate_partially_restored_values(hass):
    coordinator, load, numbers = _tank(hass)
    targets = {"setback_temperature": 65, "comfort_temperature": 65, "max_temperature": 70}
    with patch.object(EmhassLoadEntity, "async_added_to_hass", new=AsyncMock()):
        for key, value in targets.items():
            with patch.object(
                numbers[key],
                "async_get_last_number_data",
                new=AsyncMock(return_value=SimpleNamespace(native_value=value)),
            ):
                await numbers[key].async_added_to_hass()
    assert (load.setback_temperature, load.comfort_temperature, load.max_temperature) == (
        65,
        65,
        70,
    )
    coordinator.async_request_settings_refresh.assert_not_awaited()


async def test_tank_has_standby_loss_not_room_tuning(hass):
    _, load, numbers = _tank(hass)
    tuning = {"heating_rate", "cooling_constant", "thermal_inertia"}
    assert tuning.isdisjoint(numbers)
    assert "standby_loss" in numbers
    load.thermal_model = "room"
    room_keys = {
        description.key
        for description in LOAD_NUMBERS
        if description.applies_fn is None or description.applies_fn(load)
    }
    assert tuning <= room_keys
    assert "standby_loss" not in room_keys


@pytest.mark.parametrize("reading", [None, "unknown", "unavailable", "nan", "inf", "101"])
@pytest.mark.parametrize("mode", [LOAD_MODE_AUTO, LOAD_MODE_FORCE_ON])
async def test_invalid_temperature_is_unknown_even_with_override(hass, reading, mode):
    coordinator, load, _ = _tank(hass)
    load.mode = mode
    if reading is None:
        hass.states.async_remove(TEMPERATURE)
    else:
        hass.states.async_set(TEMPERATURE, reading, {"unit_of_measurement": "°C"})
    sensor = LoadShouldRunBinarySensor(coordinator, load)
    assert sensor.is_on is None
    assert sensor._reason == coordinator.water_tank_error(load)
    assert sensor._reason


async def test_fahrenheit_telemetry_is_converted_not_rejected(hass):
    coordinator, load, _ = _tank(hass)
    hass.states.async_set(TEMPERATURE, "113", {"unit_of_measurement": UnitOfTemperature.FAHRENHEIT})
    assert coordinator.loads.room_temperature(load) == pytest.approx(45)
    assert coordinator.water_tank_error(load) is None
    load.mode = LOAD_MODE_FORCE_ON
    assert LoadShouldRunBinarySensor(coordinator, load).is_on is True


@pytest.mark.parametrize("should_run", [False, True])
@pytest.mark.parametrize("switch_state", ["on", "off"])
async def test_executor_issues_no_commands_or_run_credit_for_invalid_tank(
    hass, should_run, switch_state
):
    coordinator, load, _ = _tank(hass)
    load.mode = LOAD_MODE_FORCE_ON
    hass.states.async_set(CONTROL, switch_state)
    hass.states.async_remove(TEMPERATURE)
    executor = Executor(hass, coordinator)
    decision = Decision(action=MODE_SELF_CONSUME, loads={load.subentry_id: should_run})
    with (
        patch.object(type(hass.services), "async_call", new=AsyncMock()) as service,
        patch.object(type(load), "observe_command") as observe,
    ):
        await executor._async_apply_loads(decision)
        result = await executor._async_set_load(load, should_run, decision)
    service.assert_not_awaited()
    observe.assert_not_called()
    assert result is (switch_state == "on")
    assert not decision.applied


async def test_executor_rechecks_guard_immediately_before_dispatch(hass):
    coordinator, load, _ = _tank(hass)
    executor = Executor(hass, coordinator)
    decision = Decision(action=MODE_SELF_CONSUME, loads={load.subentry_id: True})
    with (
        patch.object(coordinator, "water_tank_error", side_effect=[None, "telemetry lost"]),
        patch.object(type(hass.services), "async_call", new=AsyncMock()) as service,
    ):
        await executor._async_apply_loads(decision)
    service.assert_not_awaited()


@pytest.mark.parametrize("should_run", [False, True])
async def test_valid_tank_dispatches_normally(hass, should_run):
    coordinator, load, _ = _tank(hass)
    hass.states.async_set(CONTROL, "off" if should_run else "on")
    executor = Executor(hass, coordinator)
    decision = Decision(action=MODE_SELF_CONSUME, loads={load.subentry_id: should_run})
    with patch.object(type(hass.services), "async_call", new=AsyncMock()) as service:
        await executor._async_apply_loads(decision)
    service.assert_awaited_once_with(
        "switch",
        "turn_on" if should_run else "turn_off",
        {},
        blocking=True,
        target={"entity_id": CONTROL},
    )
    assert decision.applied


async def test_tank_temperature_entity_labels_preserve_identity(hass):
    _coordinator, _load, numbers = _tank(hass)
    assert (
        numbers["comfort_temperature"].entity_description.translation_key
        == "tank_required_temperature"
    )
    assert (
        numbers["setback_temperature"].entity_description.translation_key
        == "tank_standby_temperature"
    )
    assert numbers["comfort_temperature"].unique_id.endswith("comfort_temperature")
    assert numbers["setback_temperature"].unique_id.endswith("setback_temperature")


async def test_period_based_tank_has_no_separate_comfort_time_controls(hass):
    from custom_components.emhass_companion.time import LOAD_TIMES

    _, load, _ = _tank(hass)
    comfort_times = [
        description for description in LOAD_TIMES if description.key.startswith("comfort_")
    ]
    assert all(description.applies_fn(load) for description in comfort_times)
    load.hot_water_periods = [
        {"draw_start": "06:00:00", "draw_end": "08:00:00", "draw_energy_kwh": 3}
    ]
    assert all(not description.applies_fn(load) for description in comfort_times)
    assert all(not description.available_fn(load) for description in comfort_times)


async def test_migrating_to_periods_removes_only_obsolete_comfort_time_entries(hass):
    from homeassistant.helpers import entity_registry as er

    from custom_components.emhass_companion.time import async_setup_entry

    coordinator, load, _ = _tank(hass)
    entry = coordinator.config_entry
    entry.runtime_data = SimpleNamespace(coordinator=coordinator, loads=coordinator.loads)
    registry = er.async_get(hass)
    entities = {
        key: registry.async_get_or_create(
            "time",
            DOMAIN,
            f"{entry.entry_id}_{load.subentry_id}_{key}",
            config_entry=entry,
        ).entity_id
        for key in ("comfort_start", "comfort_end", "earliest_start")
    }
    await async_setup_entry(hass, entry, Mock())
    assert all(registry.async_get(entity_id) is not None for entity_id in entities.values())
    load.hot_water_periods = [
        {"draw_start": "06:00:00", "draw_end": "08:00:00", "draw_energy_kwh": 3}
    ]
    await async_setup_entry(hass, entry, Mock())
    assert registry.async_get(entities["comfort_start"]) is None
    assert registry.async_get(entities["comfort_end"]) is None
    assert registry.async_get(entities["earliest_start"]) is not None


async def test_room_load_never_calls_tank_validation(hass):
    coordinator, load, _ = _tank(hass)
    load.thermal_model = "room"
    load.mode = LOAD_MODE_FORCE_ON
    coordinator.water_tank_error = Mock(side_effect=AssertionError("room is not a tank"))
    assert LoadShouldRunBinarySensor(coordinator, load).is_on is True
    decision = Decision(action=MODE_SELF_CONSUME, loads={load.subentry_id: True})
    with patch.object(type(hass.services), "async_call", new=AsyncMock()) as service:
        await Executor(hass, coordinator)._async_apply_loads(decision)
    service.assert_awaited_once()
    coordinator.water_tank_error.assert_not_called()


@pytest.mark.parametrize("version", [None, "unknown", "0.18.7"])
async def test_backend_safety_takes_precedence_over_force_on(hass, version):
    coordinator, load, _ = _tank(hass)
    coordinator.backend_version = version
    load.mode = LOAD_MODE_FORCE_ON
    sensor = LoadShouldRunBinarySensor(coordinator, load)
    assert sensor.is_on is None
    assert sensor._reason == coordinator.water_tank_error(load)
    decision = Decision(action=MODE_SELF_CONSUME, loads={load.subentry_id: True})
    with patch.object(type(hass.services), "async_call", new=AsyncMock()) as service:
        await Executor(hass, coordinator)._async_apply_loads(decision)
    service.assert_not_awaited()


async def test_invalid_tank_without_control_entity_gets_no_command_credit(hass):
    coordinator, load, _ = _tank(hass)
    load.control_entity = None
    hass.states.async_remove(TEMPERATURE)
    decision = Decision(action=MODE_SELF_CONSUME, loads={load.subentry_id: True})
    with patch.object(type(load), "observe_command") as observe:
        await Executor(hass, coordinator)._async_apply_loads(decision)
    observe.assert_not_called()


@pytest.mark.parametrize(
    ("enabled", "mode", "scheduled", "expected"),
    [
        (True, LOAD_MODE_AUTO, 2000, True),
        (True, LOAD_MODE_AUTO, 0, False),
        (False, LOAD_MODE_AUTO, 2000, False),
        (True, LOAD_MODE_FORCE_ON, 0, True),
    ],
)
async def test_valid_tank_keeps_normal_enable_and_override_behavior(
    hass, enabled, mode, scheduled, expected
):
    coordinator, load, _ = _tank(hass)
    load.enabled = enabled
    load.mode = mode
    sensor = LoadShouldRunBinarySensor(coordinator, load)
    with patch.object(sensor, "_scheduled_power", return_value=scheduled):
        assert sensor.is_on is expected


@pytest.mark.parametrize("version", [None, "unknown", "0.18.7"])
async def test_disabled_tank_can_be_turned_off_without_telemetry_or_backend(hass, version):
    coordinator, load, _ = _tank(hass)
    coordinator.backend_version = version
    coordinator._water_tank_plan_blocked = "previous telemetry failure"
    load.enabled = False
    hass.states.async_remove(TEMPERATURE)
    hass.states.async_set(CONTROL, "on")
    assert coordinator.water_tank_error(load) is None
    assert LoadShouldRunBinarySensor(coordinator, load).is_on is False
    decision = Decision(action=MODE_SELF_CONSUME, loads={load.subentry_id: False})
    with patch.object(type(hass.services), "async_call", new=AsyncMock()) as service:
        await Executor(hass, coordinator)._async_apply_loads(decision)
    service.assert_awaited_once_with(
        "switch",
        "turn_off",
        {},
        blocking=True,
        target={"entity_id": CONTROL},
    )

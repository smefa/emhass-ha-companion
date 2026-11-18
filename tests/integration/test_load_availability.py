"""Schedule-helper services, live gates and charger writes against HA."""

from datetime import time
from unittest.mock import AsyncMock

from homeassistant.core import SupportsResponse
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest

from custom_components.emhass_companion.const import ACTION_DAYAHEAD, ACTION_MPC
from custom_components.emhass_companion.coordinator import EmhassData
from custom_components.emhass_companion.deferrable import DeferrableRuntime
from custom_components.emhass_companion.executor import Decision
from custom_components.emhass_companion.load_availability import async_read_schedule, live_ceiling
from custom_components.emhass_companion.models import LastRun
from tests.integration.test_executor import LOAD_SWITCH, _build, _plan


async def test_reads_real_schedule_service_response(hass):
    assert await async_setup_component(
        hass,
        "schedule",
        {
            "schedule": {
                "charging": {
                    "name": "Charging",
                    "monday": [{"from": "10:00", "to": "24:00", "data": {"max_power_w": 3000}}],
                }
            }
        },
    )
    await hass.async_block_till_done()
    weekly = await async_read_schedule(hass, "schedule.charging", 6000)
    assert weekly.blocks == ((0, 36000, 86400, 3000),)
    await hass.data[DATA_INSTANCES]["schedule"].async_remove_entity("schedule.charging")


@pytest.mark.parametrize(
    "state,attrs,mode,expected",
    [
        ("off", {}, "auto", 0),
        ("on", {}, "auto", 6000),
        ("on", {"max_power_w": 3000}, "auto", 3000),
        ("on", {"max_power_w": 0}, "force_on", 0),
        ("on", {"max_power_w": "bad"}, "force_on", 0),
        ("off", {}, "force_on", 6000),
        ("unavailable", {}, "force_on", 0),
    ],
)
async def test_live_ceiling_and_manual_override(hass, state, attrs, mode, expected):
    load = DeferrableRuntime(
        "car",
        "Car",
        nominal_power_w=6000,
        semi_continuous=False,
        availability_schedule="schedule.car",
        power_control_entity="number.charger",
        control_entity="switch.charger",
        mode=mode,
    )
    hass.states.async_set(
        "number.charger", "6000", {"unit_of_measurement": "W", "min": 0, "max": 6000, "step": 1}
    )
    hass.states.async_set("schedule.car", state, attrs)
    assert live_ceiling(hass, load) == expected


async def test_closed_schedule_blocks_stale_plan_immediately(hass):
    executor, coordinator = await _build(hass, with_load=True)
    load = coordinator.loads.all()[0]
    load.availability_schedule = "schedule.allowed"
    coordinator.data = EmhassData(
        plan=_plan(0, deferrable=2000), last_success=dt_util.utcnow(), load_order=[load.subentry_id]
    )
    hass.states.async_set("schedule.allowed", "on")
    assert executor._decide_loads(use_plan=True)[load.subentry_id]
    hass.states.async_set("schedule.allowed", "off")
    assert not executor._decide_loads(use_plan=True)[load.subentry_id]


@pytest.mark.parametrize(
    "unit,max_value,step,expected", [("W", 6000, 100, 2900), ("kW", 6, 0.1, 2.9)]
)
async def test_charger_cap_is_written_before_switch_on(hass, unit, max_value, step, expected):
    executor, coordinator = await _build(hass, with_load=True)
    load = coordinator.loads.all()[0]
    load.nominal_power_w = 6000
    load.semi_continuous = False
    load.mode = "force_on"
    load.availability_schedule = "schedule.allowed"
    load.power_control_entity = "number.charger"
    hass.states.async_set("schedule.allowed", "on", {"max_power_w": 2950})
    hass.states.async_set(
        "number.charger",
        "6" if unit == "kW" else "6000",
        {"unit_of_measurement": unit, "min": 0, "max": max_value, "step": step},
    )
    calls = []

    async def record(call):
        calls.append((call.domain, call.service, dict(call.data)))

    hass.services.async_register("number", "set_value", record)
    hass.services.async_register("switch", "turn_on", record)
    await executor._async_apply_loads(Decision("idle", loads={load.subentry_id: True}))
    assert [(domain, service) for domain, service, _ in calls] == [
        ("number", "set_value"),
        ("switch", "turn_on"),
    ]
    assert calls[0][2]["value"] == pytest.approx(expected)


async def test_power_write_failure_turns_load_off(hass):
    executor, coordinator = await _build(hass, with_load=True)
    load = coordinator.loads.all()[0]
    load.power_control_entity = "number.charger"
    load.mode = "force_on"
    hass.states.async_set(LOAD_SWITCH, "on")
    hass.states.async_set("number.charger", "unavailable")
    switched = []

    async def record(call):
        switched.append(call.service)

    hass.services.async_register("switch", "turn_off", record)
    decision = Decision("idle", loads={load.subentry_id: True})
    await executor._async_apply_loads(decision)
    assert switched == ["turn_off"]
    assert not decision.loads[load.subentry_id]


async def test_day_ahead_trigger_uses_mpc_when_schedule_is_configured(hass):
    _executor, coordinator = await _build(hass, with_load=True)
    load = coordinator.loads.all()[0]
    load.availability_schedule = "schedule.allowed"
    coordinator.backend_version = "0.18.9"
    hass.states.async_set("schedule.allowed", "on")

    async def get_schedule(call):
        return {
            "schedule.allowed": {
                day: [{"from": time(), "to": time.max}]
                for day in (
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                    "friday",
                    "saturday",
                    "sunday",
                )
            }
        }

    hass.services.async_register(
        "schedule", "get_schedule", get_schedule, supports_response=SupportsResponse.ONLY
    )
    coordinator.client.async_optimize = AsyncMock(return_value=(LastRun(status="ok"), None))
    await coordinator.async_run(ACTION_DAYAHEAD)
    args = coordinator.client.async_optimize.call_args.args
    assert args[0] == ACTION_MPC
    assert "def_availability" in args[1]


async def test_should_run_sensor_exposes_live_schedule_cap(hass):
    from custom_components.emhass_companion.binary_sensor import LoadShouldRunBinarySensor

    _executor, coordinator = await _build(hass, with_load=True)
    load = coordinator.loads.all()[0]
    load.availability_schedule = "schedule.allowed"
    coordinator.data = EmhassData(
        plan=_plan(0, deferrable=2000), last_success=dt_util.utcnow(), load_order=[load.subentry_id]
    )
    sensor = LoadShouldRunBinarySensor(coordinator, load)
    sensor.hass = hass
    hass.states.async_set("schedule.allowed", "off")
    assert sensor.is_on is False
    assert sensor.extra_state_attributes["power_limit_w"] == 0
    assert "outside allowed times" in sensor.extra_state_attributes["reason"]
    hass.states.async_set("schedule.allowed", "on")
    assert sensor.is_on
    assert sensor.extra_state_attributes["power_limit_w"] == 2000


async def test_unreadable_schedule_parks_the_load_and_still_plans(hass):
    _executor, coordinator = await _build(hass, with_load=True)
    load = coordinator.loads.all()[0]
    load.availability_schedule = "schedule.missing"
    coordinator.backend_version = "0.18.9"
    coordinator.client.async_optimize = AsyncMock(return_value=(LastRun(status="ok"), None))
    await coordinator.async_run(ACTION_MPC)
    payload = coordinator.client.async_optimize.call_args.args[1]
    assert payload["operating_hours_of_each_deferrable_load"] == [0.0]
    assert "def_availability" not in payload


async def test_unreadable_schedule_does_not_park_a_running_sequence(hass, monkeypatch):
    _executor, coordinator = await _build(hass, with_load=True)
    load = coordinator.loads.all()[0]
    load.availability_schedule = "schedule.missing"
    monkeypatch.setattr(type(load), "sequence_cycle_active", property(lambda self: True))
    coordinator.backend_version = "0.18.9"
    inputs, built = await coordinator._build(ACTION_MPC)
    assert not inputs.load_unavailable
    assert not any("parked" in text for text in built.warnings)


@pytest.mark.parametrize("enabled,hours", [(True, 0.0), (False, 0.0)])
async def test_offline_power_number_does_not_block_the_plan(hass, enabled, hours):
    _executor, coordinator = await _build(hass, with_load=True)
    load = coordinator.loads.all()[0]
    load.enabled = enabled
    load.power_control_entity = "number.charger"
    coordinator.backend_version = "0.18.9"
    hass.states.async_set("number.charger", "unavailable")
    coordinator.client.async_optimize = AsyncMock(return_value=(LastRun(status="ok"), None))
    await coordinator.async_run(ACTION_MPC)
    payload = coordinator.client.async_optimize.call_args.args[1]
    assert payload["number_of_deferrable_loads"] == 1
    assert payload["operating_hours_of_each_deferrable_load"] == [hours]


async def test_power_number_only_routes_day_ahead_to_mpc(hass):
    _executor, coordinator = await _build(hass, with_load=True)
    load = coordinator.loads.all()[0]
    load.power_control_entity = "number.charger"
    coordinator.backend_version = "0.18.9"
    hass.states.async_set(
        "number.charger", "2000", {"unit_of_measurement": "W", "min": 0, "max": 2000, "step": 1}
    )
    coordinator.client.async_optimize = AsyncMock(return_value=(LastRun(status="ok"), None))
    await coordinator.async_run(ACTION_DAYAHEAD)
    assert coordinator.client.async_optimize.call_args.args[0] == ACTION_MPC


async def test_pending_sequence_power_does_not_need_a_plan(hass):
    executor, coordinator = await _build(hass, with_load=True)
    load = coordinator.loads.all()[0]
    load.power_sequence = (2000, 1000)
    load.power_control_entity = "number.charger"
    load.sequence_start_pending = True
    coordinator.data = EmhassData()
    hass.states.async_set(
        "number.charger", "2000", {"unit_of_measurement": "W", "min": 0, "max": 2000, "step": 1}
    )
    assert await executor._async_set_load_power(load, Decision("idle"))


async def test_no_schedule_still_checks_minimum_power(hass):
    load = DeferrableRuntime(
        "car",
        "Car",
        nominal_power_w=6000,
        semi_continuous=False,
        minimum_power_w=1400,
        power_control_entity="number.charger",
        control_entity="switch.car",
    )
    hass.states.async_set(
        "number.charger", "1000", {"unit_of_measurement": "W", "min": 0, "max": 1000, "step": 1}
    )
    assert live_ceiling(hass, load) == 0


@pytest.mark.parametrize("semi_continuous", [True, False])
async def test_stale_minimum_above_power_does_not_keep_a_load_off(hass, semi_continuous):
    """The planner clamps such a minimum to the load's power; so must the gate."""
    load = DeferrableRuntime(
        "pump", "Pump", nominal_power_w=2000, semi_continuous=semi_continuous, minimum_power_w=3000
    )
    assert live_ceiling(hass, load) == 2000


async def test_completion_hold_keeps_the_running_setpoint(hass):
    from datetime import timedelta

    from tests.integration.test_executor import _armed_load

    executor, coordinator = await _build(hass, with_load=True)
    now = dt_util.utcnow()
    load = _armed_load(coordinator)
    load.semi_continuous = False
    load.power_control_entity = "number.charger"
    load.request(now - timedelta(hours=2))
    load.command_runtime = timedelta(hours=2)
    load.observe_power(1500, now - timedelta(hours=2))
    coordinator.data = EmhassData(
        plan=_plan(0, deferrable=0), last_success=now, load_order=[load.subentry_id]
    )
    hass.states.async_set(
        "number.charger", "1500", {"unit_of_measurement": "W", "min": 0, "max": 2000, "step": 1}
    )
    writes = []

    async def record(call):
        writes.append(dict(call.data))

    hass.services.async_register("number", "set_value", record)
    assert load.in_completion_hold(dt_util.utcnow())
    assert await executor._async_set_load_power(load, Decision("idle"))
    assert writes == []


async def test_number_bounds_and_unavailability_trigger_refresh_but_setpoints_do_not(hass):
    from homeassistant.core import callback

    from custom_components.emhass_companion.load_availability import async_track_availability

    load = DeferrableRuntime("car", "Car", power_control_entity="number.charger")
    attrs = {"unit_of_measurement": "W", "min": 0, "max": 6000, "step": 1}
    hass.states.async_set("number.charger", "2000", attrs)
    changes = []

    @callback
    def changed(replan):
        changes.append(replan)

    unsub = async_track_availability(hass, [load], changed)
    try:
        hass.states.async_set("number.charger", "3000", attrs)
        await hass.async_block_till_done()
        assert changes == []
        hass.states.async_set("number.charger", "3000", {**attrs, "max": 4000})
        await hass.async_block_till_done()
        assert len(changes) == 1
        hass.states.async_set("number.charger", "unavailable", attrs)
        await hass.async_block_till_done()
        assert changes == [True, True]
    finally:
        unsub()


async def test_on_time_schedule_closing_reapplies_without_a_new_plan(hass):
    from datetime import timedelta

    from homeassistant.core import callback

    from custom_components.emhass_companion.load_availability import async_track_availability

    load = DeferrableRuntime("car", "Car", availability_schedule="schedule.allowed")
    hass.states.async_set("schedule.allowed", "on", {"next_event": dt_util.utcnow()})
    changes = []

    @callback
    def changed(replan):
        changes.append(replan)

    def due():
        return {"next_event": dt_util.utcnow()}

    later = {"next_event": dt_util.utcnow() + timedelta(hours=2)}
    unsub = async_track_availability(hass, [load], changed)
    try:
        # Closing at the announced boundary: the plan already holds it.
        hass.states.async_set("schedule.allowed", "off", due())
        await hass.async_block_till_done()
        assert changes == [False]
        # Opening on time still replans: its slot was planned as closed.
        hass.states.async_set("schedule.allowed", "on", {**due(), "max_power_w": 3000})
        await hass.async_block_till_done()
        assert changes == [False, True]
        # A lower limit on time is enforced live; a higher one needs a plan.
        hass.states.async_set("schedule.allowed", "on", {**due(), "max_power_w": 2000})
        await hass.async_block_till_done()
        hass.states.async_set("schedule.allowed", "on", later)
        await hass.async_block_till_done()
        assert changes == [False, True, False, True]
        # An edit closes it well before the boundary is due.
        hass.states.async_set("schedule.allowed", "off", later)
        await hass.async_block_till_done()
        # An outage and the recovery both need a new plan.
        hass.states.async_set("schedule.allowed", "unavailable")
        hass.states.async_set("schedule.allowed", "off", later)
        await hass.async_block_till_done()
        assert changes == [False, True, False, True, True, True, True]
    finally:
        unsub()


async def test_should_run_is_off_when_the_plan_is_below_the_charger_minimum(hass):
    from custom_components.emhass_companion.binary_sensor import LoadShouldRunBinarySensor

    executor, coordinator = await _build(hass, with_load=True)
    load = coordinator.loads.all()[0]
    load.nominal_power_w = 6000
    load.semi_continuous = False
    load.power_control_entity = "number.charger"
    hass.states.async_set(
        "number.charger",
        "1400",
        {"unit_of_measurement": "W", "min": 1400, "max": 6000, "step": 100},
    )
    sensor = LoadShouldRunBinarySensor(coordinator, load)
    sensor.hass = hass

    def plan(watts):
        coordinator.data = EmhassData(
            plan=_plan(0, deferrable=watts),
            last_success=dt_util.utcnow(),
            load_order=[load.subentry_id],
        )

    plan(1000)
    assert sensor.is_on is False
    assert "lowest setting" in sensor.extra_state_attributes["reason"]
    assert not await executor._async_set_load_power(load, Decision("idle"))
    plan(2000)
    assert sensor.is_on is True
    assert sensor.extra_state_attributes["reason"] == "following the plan"

"""Executor behaviour against a live Home Assistant.

The dry-run gate is the thing most worth proving: while it is off, *no*
service call may reach the user's hardware, and the decision must still be
recorded so it can be compared against whatever is currently in charge.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    BATTERY_ACTIONS,
    CONF_CONTROL_ENTITY,
    CONF_INVERTER,
    CONF_NOMINAL_POWER,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    DOMAIN,
    MODE_AUTO,
    MODE_FORCE_CHARGE,
    MODE_FORCE_DISCHARGE,
    MODE_IDLE,
    MODE_SELF_CONSUME,
    RECURRENCE_ON_DEMAND,
    SUBENTRY_TYPE_DEFERRABLE,
    SYSTEM_MODES,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator, EmhassData
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.executor import (
    AXIS_BATTERY,
    AXIS_CURTAIL,
    Decision,
    Executor,
    _Command,
)
from custom_components.emhass_companion.models import Plan
from custom_components.emhass_companion.phase_guard import PhaseGuard
from custom_components.emhass_companion.profiles import Profile, async_load_profiles
from custom_components.emhass_companion.profiles.schema import validate_document

MODE_SELECT = "select.inverter_mode"
POWER_NUMBER = "number.inverter_power"
LOAD_SWITCH = "switch.dishwasher"

INVERTER_OPTIONS = {
    "mode_select": MODE_SELECT,
    "power_number": POWER_NUMBER,
    "self_consume_option": "Self Consumption",
    "force_charge_option": "Forced Charge",
    "force_discharge_option": "Forced Discharge",
    "idle_option": "Stop",
}

TEST_INVERTER_KEY = "inverter/test_mode_select_and_power"

# Not a builtin -- the shape that `mode_select_and_power.yaml` used to cover,
# kept here purely as executor test fixture data. Injected straight into
# `coordinator.profiles` rather than written to disk and loaded, since nothing
# here is exercising the loader.
_TEST_INVERTER_DOCUMENT = {
    "name": "Test inverter",
    "kind": "inverter",
    "version": 1,
    "options": {
        "mode_select": {"selector": {"entity": {"domain": "select"}}},
        "power_number": {"selector": {"entity": {"domain": "number"}}},
        "self_consume_option": {"default": "Self Consumption", "selector": {"text": {}}},
        "force_charge_option": {"default": "Forced Charge", "selector": {"text": {}}},
        "force_discharge_option": {"default": "Forced Discharge", "selector": {"text": {}}},
        "idle_option": {"default": "Stop", "selector": {"text": {}}},
    },
    "actions": {
        "self_consume": [
            {
                "service": "select.select_option",
                "target": {"entity_id": "{{ options.mode_select }}"},
                "data": {"option": "{{ options.self_consume_option }}"},
            }
        ],
        "force_charge": [
            {
                "service": "number.set_value",
                "target": {"entity_id": "{{ options.power_number }}"},
                "data": {"value": "{{ power_w }}"},
            },
            {
                "service": "select.select_option",
                "target": {"entity_id": "{{ options.mode_select }}"},
                "data": {"option": "{{ options.force_charge_option }}"},
            },
        ],
        "force_discharge": [
            {
                "service": "number.set_value",
                "target": {"entity_id": "{{ options.power_number }}"},
                "data": {"value": "{{ power_w }}"},
            },
            {
                "service": "select.select_option",
                "target": {"entity_id": "{{ options.mode_select }}"},
                "data": {"option": "{{ options.force_discharge_option }}"},
            },
        ],
        "idle": [
            {
                "service": "select.select_option",
                "target": {"entity_id": "{{ options.mode_select }}"},
                "data": {"option": "{{ options.idle_option }}"},
            }
        ],
    },
}


def _test_inverter_profile() -> Profile:
    document = validate_document(_TEST_INVERTER_DOCUMENT)
    return Profile(
        key=TEST_INVERTER_KEY,
        path="<test fixture>",
        kind="inverter",
        name=document["name"],
        document=document,
        is_builtin=False,
    )


def _plan(
    p_batt: float,
    deferrable: float = 0.0,
    *,
    minutes_ago: int = 0,
    p_grid: float | None = None,
    p_pv_curtailment: float | None = None,
    unit_prod_price: float | None = None,
) -> Plan:
    start = dt_util.utcnow() - timedelta(minutes=minutes_ago)
    record: dict[str, Any] = {
        "timestamp": (start - timedelta(minutes=5)).isoformat(),
        "P_batt": p_batt,
        "P_deferrable0": deferrable,
    }
    if p_grid is not None:
        record["P_grid"] = p_grid
    if p_pv_curtailment is not None:
        record["P_PV_curtailment"] = p_pv_curtailment
    if unit_prod_price is not None:
        record["unit_prod_price"] = unit_prod_price
    return Plan.from_response(
        {
            "status": "ok",
            "generated_at": start.isoformat(),
            "emhass_schema_version": "1.0",
            "plan": [record],
        }
    )


async def _build(
    hass: HomeAssistant,
    *,
    with_load: bool = False,
    inverter: bool = True,
    control_entity: str = LOAD_SWITCH,
    grid: dict[str, Any] | None = None,
    battery_sensor: bool = True,
) -> tuple[Executor, EmhassCoordinator]:
    subentries = []
    if with_load:
        subentries.append(
            {
                "subentry_type": SUBENTRY_TYPE_DEFERRABLE,
                "title": "Dishwasher",
                "unique_id": "dishwasher",
                "data": {CONF_NOMINAL_POWER: 2000, CONF_CONTROL_ENTITY: control_entity},
            }
        )

    options: dict[str, Any] = {
        "battery": {
            "use_battery": True,
            "capacity_wh": 10000,
            "charge_power_max_w": 5000,
            "discharge_power_max_w": 5000,
        }
    }
    if grid is not None:
        options["grid"] = grid
        if battery_sensor:
            options["battery_power_entity"] = "sensor.battery_power"
    if inverter:
        options[CONF_INVERTER] = {
            CONF_PROFILE: TEST_INVERTER_KEY,
            CONF_PROFILE_OPTIONS: INVERTER_OPTIONS,
        }

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        options=options,
        subentries_data=subentries,
    )
    entry.add_to_hass(hass)

    loads = DeferrableRegistry(hass, entry)
    loads.sync()

    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)
    coordinator.profiles = (await async_load_profiles(hass)).profiles
    coordinator.profiles[TEST_INVERTER_KEY] = _test_inverter_profile()
    coordinator.data = EmhassData(plan=_plan(0), last_success=dt_util.utcnow())

    hass.states.async_set(MODE_SELECT, "Self Consumption")
    hass.states.async_set(POWER_NUMBER, "0")
    hass.states.async_set(LOAD_SWITCH, "off")

    executor = Executor(hass, coordinator)
    if coordinator.config.grid.phase_guard_enabled:
        # Wired the way async_setup_entry wires it, minus the meter listener:
        # each test takes its readings explicitly with async_update().
        guard = PhaseGuard(
            hass,
            coordinator.config.grid,
            battery_enabled=coordinator.config.battery.enabled,
            battery_power_entity=coordinator.config.battery_power_entity,
        )
        guard.async_add_listener(executor.async_phase_changed)
        coordinator.phase_guard = guard
    return executor, coordinator


@pytest.fixture
def calls(hass: HomeAssistant) -> list[ServiceCall]:
    """Record every service call the executor makes."""
    recorded: list[ServiceCall] = []

    async def _record(call: ServiceCall) -> None:
        recorded.append(call)

    for domain, service in (
        ("select", "select_option"),
        ("number", "set_value"),
        ("switch", "turn_on"),
        ("switch", "turn_off"),
    ):
        hass.services.async_register(domain, service, _record)
    return recorded


# --- the dry-run gate --------------------------------------------------------


async def test_control_disabled_issues_no_service_calls(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """The single most important guarantee in this integration."""
    executor, coordinator = await _build(hass)
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())
    coordinator.control_enabled = False

    decision = await executor.async_apply()
    await hass.async_block_till_done()

    assert calls == []
    assert decision.applied is False


async def test_control_disabled_still_records_what_it_would_do(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """Dry-run is only useful if the decision is visible for comparison."""
    executor, coordinator = await _build(hass)
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())
    coordinator.control_enabled = False

    decision = await executor.async_apply()

    assert decision.action == MODE_FORCE_CHARGE
    assert decision.power_w == 3000
    assert "control disabled" in decision.reason
    # The exact calls it would have made, so they can be checked in advance.
    assert any(step["service"] == "number.set_value" for step in decision.steps)


async def test_control_enabled_issues_the_calls(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    executor, coordinator = await _build(hass)
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())
    coordinator.control_enabled = True

    decision = await executor.async_apply()
    await hass.async_block_till_done()

    assert decision.applied is True
    assert [call.service for call in calls] == ["set_value", "select_option"]
    assert calls[0].data["value"] == 3000
    assert calls[1].data["option"] == "Forced Charge"


# --- the watchdog ------------------------------------------------------------


async def test_a_stale_plan_falls_back_to_self_consumption(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """A plan that stopped refreshing describes a world that no longer exists."""
    executor, coordinator = await _build(hass)
    coordinator.data = EmhassData(
        plan=_plan(-5000),
        last_success=dt_util.utcnow() - timedelta(hours=3),
    )
    coordinator.control_enabled = True

    decision = await executor.async_apply()
    await hass.async_block_till_done()

    assert decision.action == MODE_SELF_CONSUME
    assert "no current plan" in decision.reason
    assert calls[-1].data["option"] == "Self Consumption"


async def test_no_plan_at_all_falls_back(hass: HomeAssistant) -> None:
    executor, coordinator = await _build(hass)
    coordinator.data = EmhassData(plan=None, last_success=dt_util.utcnow())
    coordinator.control_enabled = True

    decision = await executor.async_apply()
    assert decision.action == MODE_SELF_CONSUME


# --- manual override ---------------------------------------------------------


async def test_manual_mode_suspends_the_plan(hass: HomeAssistant, calls: list[ServiceCall]) -> None:
    executor, coordinator = await _build(hass)
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())
    coordinator.control_enabled = True
    coordinator.system_mode = MODE_IDLE

    decision = await executor.async_apply()
    await hass.async_block_till_done()

    assert decision.action == MODE_IDLE
    assert decision.reason == "manual override"
    assert calls[-1].data["option"] == "Stop"


async def test_manual_modes_carry_no_power(hass: HomeAssistant) -> None:
    """Every selectable mode is a zero-power steady state, so a manual
    override never sends a magnitude -- the failure this replaced was
    force_charge holding at charge_power_max_w indefinitely, which defaults
    to 0 anyway on an install that never set it."""
    executor, coordinator = await _build(hass)
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())
    coordinator.control_enabled = True

    for mode in SYSTEM_MODES:
        if mode == MODE_AUTO:
            continue
        coordinator.system_mode = mode
        decision = await executor.async_apply()
        assert decision.power_w == 0.0, mode


def test_force_modes_are_not_selectable() -> None:
    """Force charge/discharge remain in BATTERY_ACTIONS -- the optimiser still
    decides them and profiles still implement them -- but a person cannot pin
    the system into one."""
    assert MODE_FORCE_CHARGE in BATTERY_ACTIONS
    assert MODE_FORCE_DISCHARGE in BATTERY_ACTIONS
    assert MODE_FORCE_CHARGE not in SYSTEM_MODES
    assert MODE_FORCE_DISCHARGE not in SYSTEM_MODES


# --- the deadband ------------------------------------------------------------


async def test_an_unchanged_command_is_not_reissued(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """Modbus is slow; re-sending the same command every cycle is wasteful."""
    executor, coordinator = await _build(hass)
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())
    coordinator.control_enabled = True

    await executor.async_apply()
    await hass.async_block_till_done()
    first = len(calls)

    coordinator.data = EmhassData(plan=_plan(-3010), last_success=dt_util.utcnow())
    await executor.async_apply()
    await hass.async_block_till_done()

    assert len(calls) == first


async def test_a_changed_action_is_always_issued(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """The deadband must never suppress a charge-to-discharge switch."""
    executor, coordinator = await _build(hass)
    coordinator.control_enabled = True

    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())
    await executor.async_apply()
    await hass.async_block_till_done()
    first = len(calls)

    # Only 60 W apart, but on the other side of zero: a different action.
    coordinator.data = EmhassData(plan=_plan(2940), last_success=dt_util.utcnow())
    await executor.async_apply()
    await hass.async_block_till_done()

    assert len(calls) > first
    assert calls[-1].data["option"] == "Forced Discharge"


async def test_a_large_power_change_is_issued(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    executor, coordinator = await _build(hass)
    coordinator.control_enabled = True

    coordinator.data = EmhassData(plan=_plan(-1000), last_success=dt_util.utcnow())
    await executor.async_apply()
    await hass.async_block_till_done()
    first = len(calls)

    coordinator.data = EmhassData(plan=_plan(-4000), last_success=dt_util.utcnow())
    await executor.async_apply()
    await hass.async_block_till_done()

    assert len(calls) > first


# --- deferrable loads --------------------------------------------------------


async def test_a_scheduled_load_is_switched_on(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    executor, coordinator = await _build(hass, with_load=True)
    coordinator.control_enabled = True
    plan = _plan(0, deferrable=2000)
    coordinator.data = EmhassData(
        plan=plan,
        last_success=dt_util.utcnow(),
        load_order=[next(iter(coordinator.config_entry.subentries))],
    )

    await executor.async_apply()
    await hass.async_block_till_done()

    assert any(call.service == "turn_on" for call in calls)


async def test_a_load_already_in_the_right_state_is_left_alone(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    executor, coordinator = await _build(hass, with_load=True)
    coordinator.control_enabled = True
    hass.states.async_set(LOAD_SWITCH, "on")
    coordinator.data = EmhassData(
        plan=_plan(0, deferrable=2000),
        last_success=dt_util.utcnow(),
        load_order=[next(iter(coordinator.config_entry.subentries))],
    )

    await executor.async_apply()
    await hass.async_block_till_done()

    assert not any(call.service in ("turn_on", "turn_off") for call in calls)


async def test_a_load_without_a_control_entity_is_advisory_only(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """Leaving the control entity unset must keep the load read-only."""
    executor, coordinator = await _build(hass)
    coordinator.control_enabled = True
    coordinator.data = EmhassData(plan=_plan(0), last_success=dt_util.utcnow())

    await executor.async_apply()
    await hass.async_block_till_done()

    assert not any(call.service in ("turn_on", "turn_off") for call in calls)


async def test_a_script_left_over_as_a_control_entity_is_never_fired(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """The form used to accept a script here, and a script cannot be one.

    Its state is "on" only while it executes, so a short one reads "off"
    again immediately and would be re-triggered on every single apply for the
    whole scheduled window -- while `script.turn_off` would only ever cancel
    the script, leaving the appliance running. Doing nothing is the honest
    outcome; the repair (ISSUE_SCRIPT_CONTROL_ENTITY) is what tells the user.
    """
    script = "script.start_dishwasher"

    async def _record(call: ServiceCall) -> None:
        calls.append(call)

    for service in ("turn_on", "turn_off"):
        hass.services.async_register("script", service, _record)
    hass.states.async_set(script, "off")

    executor, coordinator = await _build(hass, with_load=True, control_entity=script)
    coordinator.control_enabled = True
    coordinator.data = EmhassData(
        plan=_plan(0, deferrable=2000),
        last_success=dt_util.utcnow(),
        load_order=[next(iter(coordinator.config_entry.subentries))],
    )

    decision = await executor.async_apply()
    await hass.async_block_till_done()

    assert not any(call.domain == "script" for call in calls)
    # The plan itself is unaffected -- the load is advisory, not excluded.
    assert decision.loads == {next(iter(coordinator.config_entry.subentries)): True}


# --- degradation -------------------------------------------------------------


async def test_no_inverter_profile_means_no_battery_calls(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """A configuration with no inverter profile must still decide, not crash."""
    executor, coordinator = await _build(hass, inverter=False)
    coordinator.control_enabled = True
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())

    decision = await executor.async_apply()
    await hass.async_block_till_done()

    assert decision.action == MODE_FORCE_CHARGE
    assert decision.steps == []
    assert calls == []


async def test_idle_falls_back_to_self_consume_when_the_profile_defines_no_idle(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """A profile is not required to define `idle` -- some hardware genuinely
    has no standby. The plan's decision still says `idle` on the sensor; only
    the *write* is substituted, so a decision the plan legitimately produced
    is honoured instead of just failing."""
    executor, coordinator = await _build(hass)
    coordinator.control_enabled = True
    coordinator.profiles[TEST_INVERTER_KEY].document["actions"].pop("idle")
    coordinator.data = EmhassData(plan=_plan(0), last_success=dt_util.utcnow())

    decision = await executor.async_apply()
    await hass.async_block_till_done()

    assert decision.action == MODE_IDLE
    assert decision.error is None
    assert any("self_consume" in rule for rule in decision.rules)
    assert calls[-1].data["option"] == "Self Consumption"


async def test_idle_resolved_to_self_consume_is_not_reissued_by_a_later_self_consume(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """Write suppression must key on the resolved action, not the decided
    one -- otherwise a profile with no `idle` re-issues identical service
    calls every time the decision alternates between `idle` and
    `self_consume`."""
    executor, coordinator = await _build(hass)
    coordinator.control_enabled = True
    coordinator.profiles[TEST_INVERTER_KEY].document["actions"].pop("idle")

    coordinator.data = EmhassData(plan=_plan(0), last_success=dt_util.utcnow())
    await executor.async_apply()
    await hass.async_block_till_done()
    first = len(calls)

    coordinator.data = EmhassData(plan=_plan(0, p_grid=0), last_success=dt_util.utcnow())
    decision = await executor.async_apply()
    await hass.async_block_till_done()

    assert decision.action == MODE_SELF_CONSUME
    assert len(calls) == first


async def test_a_missing_action_with_no_fallback_is_reported_not_raised(
    hass: HomeAssistant,
) -> None:
    """force_discharge has no substitute -- forcing charge instead of
    discharge is not a fallback, it is the opposite decision, so this must
    still surface as an error rather than silently doing something else."""
    executor, coordinator = await _build(hass)
    coordinator.control_enabled = True
    coordinator.profiles[TEST_INVERTER_KEY].document["actions"].pop("force_discharge")
    coordinator.data = EmhassData(plan=_plan(3000), last_success=dt_util.utcnow())

    decision = await executor.async_apply()

    assert decision.action == MODE_FORCE_DISCHARGE
    assert decision.error is not None
    assert "force_discharge" in decision.error


async def test_a_failing_service_call_does_not_raise(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """One broken inverter call must not take down the whole update."""

    async def _boom(call: ServiceCall) -> None:
        raise RuntimeError("modbus timeout")

    hass.services.async_register("select", "select_option", _boom)

    executor, coordinator = await _build(hass)
    coordinator.control_enabled = True
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())

    decision = await executor.async_apply()
    await hass.async_block_till_done()

    assert decision.error is not None
    assert decision.applied is False


async def test_a_failed_command_is_retried_next_cycle(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """A failure must not be recorded as applied, or the deadband would stick."""
    failing = True

    async def _maybe_boom(call: ServiceCall) -> None:
        calls.append(call)
        if failing:
            raise RuntimeError("modbus timeout")

    hass.services.async_register("select", "select_option", _maybe_boom)

    executor, coordinator = await _build(hass)
    coordinator.control_enabled = True
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())

    await executor.async_apply()
    await hass.async_block_till_done()

    failing = False
    before = len(calls)
    await executor.async_apply()
    await hass.async_block_till_done()

    assert len(calls) > before


# --- plan schema compatibility ------------------------------------------------


def _plan_with_schema(version: str) -> Plan:
    now = dt_util.utcnow()
    return Plan.from_response(
        {
            "status": "ok",
            "generated_at": now.isoformat(),
            "emhass_schema_version": version,
            "plan": [{"timestamp": now.isoformat(), "P_batt": -3000}],
        }
    )


@pytest.mark.parametrize("version", ["1.0", "1.4.2", "1"])
async def test_a_supported_schema_is_accepted(hass: HomeAssistant, version) -> None:
    _, coordinator = await _build(hass)
    assert coordinator._schema_supported(_plan_with_schema(version)) is True


@pytest.mark.parametrize("version", ["2.0", "3.1"])
async def test_an_unknown_schema_major_is_refused(hass: HomeAssistant, version) -> None:
    """A renamed column or flipped sign would be misread, not raise.

    Discarding the plan makes it stale, which the watchdog already handles.
    """
    _, coordinator = await _build(hass)
    assert coordinator._schema_supported(_plan_with_schema(version)) is False


async def test_refusing_a_schema_raises_a_repair(hass: HomeAssistant) -> None:
    from homeassistant.helpers import issue_registry as ir

    from custom_components.emhass_companion.const import ISSUE_PLAN_SCHEMA

    _, coordinator = await _build(hass)
    coordinator._schema_supported(_plan_with_schema("2.0"))

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, ISSUE_PLAN_SCHEMA) is not None

    # And clears once EMHASS returns something understandable again.
    coordinator._schema_supported(_plan_with_schema("1.0"))
    assert registry.async_get_issue(DOMAIN, ISSUE_PLAN_SCHEMA) is None


async def test_a_missing_schema_version_is_tolerated(hass: HomeAssistant) -> None:
    """Older EMHASS builds may not report one; refusing everything is worse."""
    _, coordinator = await _build(hass)
    assert coordinator._schema_supported(_plan_with_schema("")) is True


# --- infeasible-run repair ----------------------------------------------------


async def test_an_infeasible_run_raises_a_repair(hass: HomeAssistant) -> None:
    from homeassistant.helpers import issue_registry as ir

    from custom_components.emhass_companion.const import ISSUE_OPTIMIZATION_INFEASIBLE

    _, coordinator = await _build(hass)
    coordinator._track_infeasible_issue(True, "naive-mpc-optim")

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, ISSUE_OPTIMIZATION_INFEASIBLE) is not None

    # And clears once a run solves cleanly again.
    coordinator._track_infeasible_issue(False, "naive-mpc-optim")
    assert registry.async_get_issue(DOMAIN, ISSUE_OPTIMIZATION_INFEASIBLE) is None


# --- run-failed repair ---------------------------------------------------------


async def test_a_failed_run_raises_a_repair(hass: HomeAssistant) -> None:
    from homeassistant.helpers import issue_registry as ir

    from custom_components.emhass_companion.const import ISSUE_RUN_FAILED

    _, coordinator = await _build(hass)
    coordinator._track_run_failed_issue(True, "naive-mpc-optim", "boom")

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, ISSUE_RUN_FAILED) is not None

    # And clears once a run succeeds again.
    coordinator._track_run_failed_issue(False, "naive-mpc-optim", "")
    assert registry.async_get_issue(DOMAIN, ISSUE_RUN_FAILED) is None


async def test_async_run_raises_a_repair_on_emhass_error(hass: HomeAssistant) -> None:
    from homeassistant.helpers import issue_registry as ir
    from homeassistant.helpers.update_coordinator import UpdateFailed
    import pytest

    from custom_components.emhass_companion.api import EmhassApiError
    from custom_components.emhass_companion.const import ISSUE_RUN_FAILED

    _, coordinator = await _build(hass)

    async def _boom(action: str) -> None:
        raise EmhassApiError("POST /action/naive-mpc-optim returned 500: boom")

    coordinator._run = _boom  # type: ignore[method-assign]

    with pytest.raises(UpdateFailed):
        await coordinator.async_run("naive-mpc-optim", notify=False)

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, ISSUE_RUN_FAILED) is not None


# --- command lifetime --------------------------------------------------------
#
# An inverter whose forced mode carries its own duration reverts on its own.
# Treating an unchanged command as "nothing to do" is how a battery quietly
# stops following the plan halfway through the evening.

EXPIRING_PROFILE = """
name: Expiring command inverter
kind: inverter
version: 2
control:
  lifetime: expires
  duration_min: 30
actions:
  prepare:
    - service: select.select_option
      target: {entity_id: select.inverter_mode}
      data: {option: Remote Control}
  self_consume:
    - service: select.select_option
      target: {entity_id: select.inverter_mode}
      data: {option: Self Consumption}
  restore:
    - service: select.select_option
      target: {entity_id: select.inverter_mode}
      data: {option: Handed Back}
  force_charge:
    - service: number.set_value
      target: {entity_id: number.inverter_power}
      data: {value: "{{ power }}", duration: "{{ duration_min }}"}
  force_discharge:
    - service: number.set_value
      target: {entity_id: number.inverter_power}
      data: {value: "{{ power }}"}
  idle:
    - service: select.select_option
      target: {entity_id: select.inverter_mode}
      data: {option: Stop}
"""


async def _with_expiring_profile(hass: HomeAssistant) -> tuple[Executor, EmhassCoordinator]:
    directory = Path(hass.config.path("emhass_companion/profiles/inverter"))
    await hass.async_add_executor_job(lambda: directory.mkdir(parents=True, exist_ok=True))
    await hass.async_add_executor_job(
        (directory / "expiring.yaml").write_text, EXPIRING_PROFILE, "utf-8"
    )

    executor, coordinator = await _build(hass)
    coordinator.profiles = (await async_load_profiles(hass)).profiles
    coordinator.config.inverter.key = "inverter/expiring"
    coordinator.control_enabled = True
    return executor, coordinator


async def test_an_expiring_command_is_reissued_before_it_lapses(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    executor, coordinator = await _with_expiring_profile(hass)
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())

    await executor.async_apply()
    await hass.async_block_till_done()
    first = len(calls)

    # Same command, but old enough that the inverter is about to revert.
    executor._last_applied[AXIS_BATTERY].at = dt_util.utcnow() - timedelta(minutes=20)
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())
    await executor.async_apply()
    await hass.async_block_till_done()

    assert len(calls) > first, "an expiring command was suppressed as unchanged"


async def test_a_fresh_expiring_command_is_still_deadbanded(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """Re-issuing early is waste; re-issuing late is a lost plan. Only late."""
    executor, coordinator = await _with_expiring_profile(hass)
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())

    await executor.async_apply()
    await hass.async_block_till_done()
    first = len(calls)

    coordinator.data = EmhassData(plan=_plan(-3010), last_success=dt_util.utcnow())
    await executor.async_apply()
    await hass.async_block_till_done()

    assert len(calls) == first


async def test_prepare_runs_once_per_session_not_once_per_write(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    executor, coordinator = await _with_expiring_profile(hass)
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())

    await executor.async_apply()
    await hass.async_block_till_done()
    coordinator.data = EmhassData(plan=_plan(3000), last_success=dt_util.utcnow())
    await executor.async_apply()
    await hass.async_block_till_done()

    prepared = [call for call in calls if call.data.get("option") == "Remote Control"]
    assert len(prepared) == 1


# --- curtailment ---------------------------------------------------------
#
# A second, independent axis from the battery actions above: a plan can
# curtail while idle, and force-charge while not curtailing.

CURTAILING_PROFILE = """
name: Curtailing inverter
kind: inverter
version: 2
actions:
  self_consume:
    - service: select.select_option
      target: {entity_id: select.inverter_mode}
      data: {option: Self Consumption}
  force_charge:
    - service: number.set_value
      target: {entity_id: number.inverter_power}
      data: {value: "{{ power }}"}
  force_discharge:
    - service: number.set_value
      target: {entity_id: number.inverter_power}
      data: {value: "{{ power }}"}
  idle:
    - service: select.select_option
      target: {entity_id: select.inverter_mode}
      data: {option: Stop}
  curtail:
    - service: switch.turn_on
      target: {entity_id: switch.export_limit}
    - service: number.set_value
      target: {entity_id: number.export_limit}
      data: {value: "{{ curtail_w }}"}
  uncurtail:
    - service: switch.turn_off
      target: {entity_id: switch.export_limit}
"""


async def _with_curtailing_profile(hass: HomeAssistant) -> tuple[Executor, EmhassCoordinator]:
    directory = Path(hass.config.path("emhass_companion/profiles/inverter"))
    await hass.async_add_executor_job(lambda: directory.mkdir(parents=True, exist_ok=True))
    await hass.async_add_executor_job(
        (directory / "curtailing.yaml").write_text, CURTAILING_PROFILE, "utf-8"
    )

    executor, coordinator = await _build(hass)
    coordinator.profiles = (await async_load_profiles(hass)).profiles
    coordinator.config.inverter.key = "inverter/curtailing"
    coordinator.control_enabled = True
    return executor, coordinator


async def test_curtailment_is_applied_and_reported(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    executor, coordinator = await _with_curtailing_profile(hass)
    coordinator.data = EmhassData(
        plan=_plan(0, p_grid=-1200, p_pv_curtailment=1900), last_success=dt_util.utcnow()
    )

    decision = await executor.async_apply()
    await hass.async_block_till_done()

    assert decision.curtail is True
    assert decision.curtail_w == 1200
    export_limit_calls = [
        call for call in calls if call.data.get("entity_id") == "number.export_limit"
    ]
    assert export_limit_calls[-1].data["value"] == 1200
    assert any(
        call.service == "turn_on" and call.data.get("entity_id") == "switch.export_limit"
        for call in calls
    )


async def test_no_curtailment_signal_reports_false_not_none(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """None is reserved for "not applicable" (no capability) -- a profile that
    can curtail but has no reason to right now must report False, not None."""
    executor, coordinator = await _with_curtailing_profile(hass)
    coordinator.data = EmhassData(plan=_plan(0), last_success=dt_util.utcnow())

    decision = await executor.async_apply()

    assert decision.curtail is False


async def test_curtailment_write_suppression_is_independent_of_the_battery_deadband(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    executor, coordinator = await _with_curtailing_profile(hass)
    coordinator.data = EmhassData(
        plan=_plan(-3000, p_grid=-1000, p_pv_curtailment=1000), last_success=dt_util.utcnow()
    )
    await executor.async_apply()
    await hass.async_block_till_done()

    def _calls_to(entity_id: str) -> int:
        return sum(1 for call in calls if call.data.get("entity_id") == entity_id)

    battery_calls_before = _calls_to("number.inverter_power")
    curtail_calls_before = _calls_to("number.export_limit")

    # Battery unchanged (still -3000 W); curtailment magnitude jumps well
    # past the deadband. The battery write must be suppressed as unchanged
    # while the curtailment write goes through -- neither axis's deadband may
    # gate the other.
    coordinator.data = EmhassData(
        plan=_plan(-3000, p_grid=-3000, p_pv_curtailment=3000), last_success=dt_util.utcnow()
    )
    await executor.async_apply()
    await hass.async_block_till_done()

    assert _calls_to("number.inverter_power") == battery_calls_before
    assert _calls_to("number.export_limit") > curtail_calls_before


# --- handing control back ----------------------------------------------------


async def test_control_is_handed_back_when_the_gate_is_switched_off(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """A persistent forced mode outlives the switch unless something undoes it."""
    executor, coordinator = await _with_expiring_profile(hass)
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())

    await executor.async_apply()
    await hass.async_block_till_done()

    coordinator.control_enabled = False
    await executor.async_apply()
    await hass.async_block_till_done()

    assert any(call.data.get("option") == "Handed Back" for call in calls)


async def test_restore_uncurtails_and_hands_back_the_battery(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """An export limit left in place is the same failure mode as a battery
    left force-charging -- silent and indefinite, and worse because it costs
    money rather than just looking wrong. Both must be undone, independently,
    on the same handover."""
    executor, coordinator = await _with_curtailing_profile(hass)
    coordinator.data = EmhassData(
        plan=_plan(-3000, p_grid=-1000, p_pv_curtailment=1000), last_success=dt_util.utcnow()
    )

    await executor.async_apply()
    await hass.async_block_till_done()
    calls.clear()

    coordinator.control_enabled = False
    await executor.async_apply()
    await hass.async_block_till_done()

    assert any(
        call.service == "turn_off" and call.data.get("entity_id") == "switch.export_limit"
        for call in calls
    )
    assert any(call.data.get("option") == "Self Consumption" for call in calls)


async def test_restore_is_issued_even_when_the_last_command_looks_current(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    executor, coordinator = await _with_expiring_profile(hass)
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())

    await executor.async_apply()
    await hass.async_block_till_done()
    before = len(calls)

    await executor.async_restore("test")
    await hass.async_block_till_done()

    assert len(calls) > before


async def test_restore_falls_back_to_self_consumption(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """The shipped profiles define no `restore`; self-consumption is the answer."""
    executor, coordinator = await _build(hass)
    coordinator.control_enabled = True
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())

    await executor.async_apply()
    await hass.async_block_till_done()
    calls.clear()

    await executor.async_restore("test")
    await hass.async_block_till_done()

    assert any(call.data.get("option") == "Self Consumption" for call in calls)


async def test_a_dry_run_never_reaches_for_the_hardware_on_shutdown(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """Handing back an inverter we never took is someone else's automation."""
    executor, coordinator = await _build(hass)
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())

    await executor.async_apply()  # control gate off: decides, does not write
    await executor.async_restore("Home Assistant stopping")
    await hass.async_block_till_done()

    assert not calls


async def test_a_write_that_fails_partway_is_still_handed_back(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """The power write lands, the mode write raises: the inverter has already
    been moved, so the very first command of a session failing halfway must
    not leave the handover thinking there is nothing to hand back."""

    async def _mode(call: ServiceCall) -> None:
        calls.append(call)
        if call.data.get("option") == "Forced Charge":
            raise RuntimeError("modbus timeout")

    hass.services.async_register("select", "select_option", _mode)
    executor, coordinator = await _build(hass)
    coordinator.control_enabled = True
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())

    decision = await executor.async_apply()
    await hass.async_block_till_done()
    assert decision.error is not None
    assert any(call.service == "set_value" for call in calls)
    calls.clear()

    await executor.async_restore("Home Assistant stopping")
    await hass.async_block_till_done()

    assert any(call.data.get("option") == "Self Consumption" for call in calls)


async def test_a_failed_handover_is_retried_until_it_goes_through(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """The gate going off is a one-off event, but the handover it asks for is
    not: an inverter unreachable at that moment must be handed back once it
    answers again, not left in its forced mode for as long as the gate stays
    off."""
    executor, coordinator = await _with_expiring_profile(hass)
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())
    await executor.async_apply()
    await hass.async_block_till_done()

    reachable = False

    async def _mode(call: ServiceCall) -> None:
        calls.append(call)
        if not reachable:
            raise RuntimeError("modbus timeout")

    hass.services.async_register("select", "select_option", _mode)
    coordinator.control_enabled = False
    await executor.async_apply()
    await hass.async_block_till_done()

    reachable = True
    calls.clear()
    await executor.async_apply()
    await hass.async_block_till_done()
    assert [call.data.get("option") for call in calls] == ["Handed Back"]

    # Done once it went through: not re-sent on every gated cycle after.
    calls.clear()
    await executor.async_apply()
    await hass.async_block_till_done()
    assert not calls


async def test_curtailment_alone_does_not_hand_back_the_battery(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """Restore undoes what was written, axis by axis. A battery that was never
    commanded is not ours to hand back."""
    executor, _coordinator = await _with_curtailing_profile(hass)
    executor._held.add(AXIS_CURTAIL)
    executor._last_applied[AXIS_CURTAIL] = _Command("curtail", 1000.0, dt_util.utcnow())

    await executor.async_restore("test")
    await hass.async_block_till_done()

    assert any(call.data.get("entity_id") == "switch.export_limit" for call in calls)
    assert not any(call.domain == "select" for call in calls)


# --- serialisation -----------------------------------------------------------


def _gate_the_power_write(hass: HomeAssistant, calls: list[ServiceCall]) -> asyncio.Event:
    """Hold the inverter's power write open until the returned event is set.

    Applies only race when one is actually suspended mid-write, which a
    service handler that returns without awaiting anything never is -- with
    eager task execution the loop would simply run them back to back, and the
    bug would be untestable rather than absent.
    """
    gate = asyncio.Event()

    async def _held(call: ServiceCall) -> None:
        calls.append(call)
        await gate.wait()

    hass.services.async_register("number", "set_value", _held)
    return gate


async def test_concurrent_applies_do_not_double_write(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """Two applies racing must not both decide the same write is needed.

    Applies are fired from two independent sources -- every coordinator update
    and every clock tick -- so overlapping is normal, not exotic. Unserialised,
    both read the same empty ``_last_applied``, both conclude the command is
    new, and the inverter is commanded twice.
    """
    executor, coordinator = await _build(hass)
    coordinator.control_enabled = True
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())
    gate = _gate_the_power_write(hass, calls)

    first = hass.async_create_task(executor.async_apply())
    await asyncio.sleep(0.05)
    second = hass.async_create_task(executor.async_apply())
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(first, second)
    await hass.async_block_till_done()

    assert len([call for call in calls if call.service == "set_value"]) == 1


async def test_restore_does_not_interleave_with_an_apply(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """A handover the apply around it finishes writing over is not a handover.

    Serialised, the restore's self-consumption write is the last thing to
    reach the inverter. Unserialised it lands in the middle of the apply, and
    the apply's own remaining write follows it -- leaving the inverter in the
    forced mode the handover exists to release.
    """
    executor, coordinator = await _build(hass)
    coordinator.control_enabled = True
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())
    await executor.async_apply()
    await hass.async_block_till_done()
    calls.clear()

    # A different action, so the second apply is not suppressed as unchanged.
    coordinator.data = EmhassData(plan=_plan(4000), last_success=dt_util.utcnow())
    gate = _gate_the_power_write(hass, calls)

    applying = hass.async_create_task(executor.async_apply())
    await asyncio.sleep(0.05)
    restoring = hass.async_create_task(executor.async_restore("test"))
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(applying, restoring)
    await hass.async_block_till_done()

    handover = max(
        index for index, call in enumerate(calls) if call.data.get("option") == "Self Consumption"
    )
    last_write = max(index for index, call in enumerate(calls))
    assert handover == last_write


# --- how a run ends, from the executor's side ---------------------------------


def _armed_load(coordinator: EmhassCoordinator, **fields):
    """The one deferrable load, armed as an on-demand run."""
    load = coordinator.loads.get(next(iter(coordinator.config_entry.subentries)))
    load.recurrence = RECURRENCE_ON_DEMAND
    load.operating_hours = 1.0
    load.power_sensor = "sensor.dishwasher_power"
    for name, value in fields.items():
        setattr(load, name, value)
    return load


async def test_the_commanded_clock_ticks_only_when_something_was_commanded(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """The gate being off means nothing was asked of the appliance, so no run
    may be credited with the time it was off for."""
    executor, coordinator = await _build(hass, with_load=True)
    load = _armed_load(coordinator)
    load.request(dt_util.utcnow())
    coordinator.data = EmhassData(
        plan=_plan(0, deferrable=2000),
        last_success=dt_util.utcnow(),
        load_order=[load.subentry_id],
    )

    coordinator.control_enabled = False
    await executor.async_apply()
    await hass.async_block_till_done()
    assert load.is_commanded is False

    coordinator.control_enabled = True
    await executor.async_apply()
    await hass.async_block_till_done()
    assert load.is_commanded is True


async def test_a_load_that_failed_to_start_is_not_credited(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """A turn_on that raised (the plug is offline) commanded nothing. Credited
    anyway, an on-demand run would "complete" without the appliance running."""

    async def _offline(call: ServiceCall) -> None:
        raise RuntimeError("plug unreachable")

    hass.services.async_register("switch", "turn_on", _offline)
    executor, coordinator = await _build(hass, with_load=True)
    coordinator.control_enabled = True
    load = _armed_load(coordinator)
    load.request(dt_util.utcnow())
    coordinator.data = EmhassData(
        plan=_plan(0, deferrable=2000),
        last_success=dt_util.utcnow(),
        load_order=[load.subentry_id],
    )

    decision = await executor.async_apply()
    await hass.async_block_till_done()

    assert decision.error is not None
    assert load.is_commanded is False


async def test_disabling_a_running_load_switches_it_off_and_stops_its_clock(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """Disabled parks a load: its Should run sensor says off, so the executor
    must agree -- not leave the appliance on with its run still being credited."""
    executor, coordinator = await _build(hass, with_load=True)
    coordinator.control_enabled = True
    load = _armed_load(coordinator)
    load.request(dt_util.utcnow())
    coordinator.data = EmhassData(
        plan=_plan(0, deferrable=2000),
        last_success=dt_util.utcnow(),
        load_order=[load.subentry_id],
    )
    await executor.async_apply()
    await hass.async_block_till_done()
    assert load.is_commanded is True
    hass.states.async_set(LOAD_SWITCH, "on")
    calls.clear()

    load.enabled = False
    await executor.async_apply()
    await hass.async_block_till_done()

    assert [call.service for call in calls if call.domain == "switch"] == ["turn_off"]
    assert load.is_commanded is False


async def test_a_stale_plan_does_not_re_run_a_request_that_ended(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """The plan outlives the request that produced it. Left to the plan alone,
    a finished run keeps being switched back on until the next optimisation."""
    executor, coordinator = await _build(hass, with_load=True)
    coordinator.control_enabled = True
    load = _armed_load(coordinator, requested=False)
    hass.states.async_set(LOAD_SWITCH, "on")
    coordinator.data = EmhassData(
        plan=_plan(0, deferrable=2000),
        last_success=dt_util.utcnow(),
        load_order=[load.subentry_id],
    )

    await executor.async_apply()
    await hass.async_block_till_done()

    assert any(call.service == "turn_off" for call in calls)


async def test_an_appliance_still_drawing_at_its_target_is_not_cut_off(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """Its target is met, so the plan has stopped asking for it -- obeying that
    would take power from an appliance mid-program."""
    executor, coordinator = await _build(hass, with_load=True)
    coordinator.control_enabled = True
    now = dt_util.utcnow()
    load = _armed_load(coordinator)
    load.request(now - timedelta(hours=2))
    load.command_runtime = timedelta(hours=2)
    load.observe_power(2000, now - timedelta(hours=2))
    hass.states.async_set(LOAD_SWITCH, "on")
    coordinator.data = EmhassData(
        plan=_plan(0, deferrable=0),  # the plan wants nothing more from it
        last_success=now,
        load_order=[load.subentry_id],
    )

    assert load.in_completion_hold(dt_util.utcnow()) is True
    await executor.async_apply()
    await hass.async_block_till_done()

    assert not any(call.service == "turn_off" for call in calls)


# --- phase guard ---------------------------------------------------------------
#
# 16 A main fuse, 1 A margin: every phase is held at 15 A = 3450 W. The test
# battery charges at up to 5000 W, and its power sensor reads what `battery=`
# says it is actually drawing (positive charging) -- never what was commanded.

PHASE_GRID = {
    "phase_l1_entity": "sensor.l1",
    "phase_l2_entity": "sensor.l2",
    "phase_l3_entity": "sensor.l3",
    "main_fuse_a": 16,
    "phase_margin_a": 1.0,
}


def _phases(
    hass: HomeAssistant,
    coordinator: EmhassCoordinator,
    l1: float,
    l2: float = 300,
    l3: float = 300,
    *,
    battery: float = 0,
) -> None:
    for entity_id, watts in (("sensor.l1", l1), ("sensor.l2", l2), ("sensor.l3", l3)):
        hass.states.async_set(entity_id, str(watts), {"unit_of_measurement": "W"})
    # EMHASS's convention on the sensor: negative while charging.
    hass.states.async_set("sensor.battery_power", str(-battery), {"unit_of_measurement": "W"})
    assert coordinator.phase_guard is not None
    coordinator.phase_guard.async_update()


async def test_a_heater_on_one_phase_caps_the_planned_charge(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """L1 at 2600 W leaves 850 W on it -- 2550 W of symmetric charge, not 5000."""
    executor, coordinator = await _build(hass, grid=PHASE_GRID)
    _phases(hass, coordinator, 2600)
    coordinator.data = EmhassData(plan=_plan(-5000), last_success=dt_util.utcnow())
    coordinator.control_enabled = True

    decision = await executor.async_apply()
    await hass.async_block_till_done()

    assert decision.action == MODE_FORCE_CHARGE
    assert decision.planned_charge_w == 5000
    assert decision.power_w == pytest.approx(2550)
    assert decision.phase_cut_w == pytest.approx(2450)
    assert any("phase guard" in rule for rule in decision.rules)
    # Floored, never rounded up past the cap.
    assert 2549 <= calls[0].data["value"] <= 2550


async def test_a_phase_already_at_its_limit_hands_the_battery_to_self_consumption(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    executor, coordinator = await _build(hass, grid=PHASE_GRID)
    _phases(hass, coordinator, 3500)
    coordinator.data = EmhassData(plan=_plan(-5000), last_success=dt_util.utcnow())
    coordinator.control_enabled = True

    decision = await executor.async_apply()
    await hass.async_block_till_done()

    assert decision.action == MODE_SELF_CONSUME
    assert decision.phase_cut_w == 5000
    assert calls[-1].data["option"] == "Self Consumption"


async def test_discharge_is_never_touched(hass: HomeAssistant) -> None:
    """Discharging takes load off every phase; there is nothing to protect."""
    executor, coordinator = await _build(hass, grid=PHASE_GRID)
    _phases(hass, coordinator, 3500)
    coordinator.data = EmhassData(plan=_plan(3000), last_success=dt_util.utcnow())

    decision = await executor.async_apply()

    assert decision.action == MODE_FORCE_DISCHARGE
    assert decision.power_w == 3000
    assert decision.phase_cut_w == 0


async def test_unreadable_phases_fail_open_and_say_so(hass: HomeAssistant) -> None:
    executor, coordinator = await _build(hass, grid=PHASE_GRID)
    _phases(hass, coordinator, 400)
    hass.states.async_set("sensor.l1", "unavailable")
    coordinator.phase_guard.async_update()
    coordinator.data = EmhassData(plan=_plan(-5000), last_success=dt_util.utcnow())

    decision = await executor.async_apply()

    assert decision.power_w == 5000
    assert any("phase readings unavailable" in rule for rule in decision.rules)


async def test_a_reading_in_an_unknown_unit_counts_as_unreadable(hass: HomeAssistant) -> None:
    """Amps read as watts would make the phase look 230 times emptier."""
    _executor, coordinator = await _build(hass, grid=PHASE_GRID)
    _phases(hass, coordinator, 400)
    hass.states.async_set("sensor.l2", "12")
    coordinator.phase_guard.async_update()

    assert not coordinator.phase_guard.available
    assert coordinator.phase_guard.unreadable == ["sensor.l2"]


async def test_current_readings_are_used_as_amps(hass: HomeAssistant) -> None:
    _executor, coordinator = await _build(hass, grid=PHASE_GRID)
    hass.states.async_set("sensor.battery_power", "0", {"unit_of_measurement": "W"})
    for entity_id, amps in (("sensor.l1", 11.3), ("sensor.l2", 1.3), ("sensor.l3", 1.3)):
        hass.states.async_set(entity_id, str(amps), {"unit_of_measurement": "A"})
    coordinator.phase_guard.async_update()

    assert coordinator.phase_guard.charge_cap_w == pytest.approx(3 * 3.7 * 230)


async def test_the_guard_cuts_between_plans_when_a_phase_climbs(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """No new plan, no clock tick: the meter reading alone re-applies.

    The battery is measured drawing the full 5000 W, so L1 at 2600 + 1667 W
    reads as 2600 W of house.
    """
    executor, coordinator = await _build(hass, grid=PHASE_GRID)
    _phases(hass, coordinator, 400)
    coordinator.data = EmhassData(plan=_plan(-5000), last_success=dt_util.utcnow())
    coordinator.control_enabled = True
    await executor.async_apply()
    await hass.async_block_till_done()
    assert calls[-2].data["value"] == 5000

    _phases(hass, coordinator, 2600 + 1667, 300 + 1667, 300 + 1667, battery=5000)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert executor.last_decision.power_w == pytest.approx(2550, abs=2)
    assert calls[-2].data["value"] == pytest.approx(2550, abs=2)


async def test_a_commanded_charge_the_battery_is_not_drawing_is_not_subtracted(
    hass: HomeAssistant, calls: list[ServiceCall], freezer
) -> None:
    """5000 W commanded, nothing drawn yet (ramping, BMS, solar on the DC side).

    Subtracting the command would put the house at 933 W on L1 and allow the
    full 5000 W; only 2550 W actually fits once the battery starts drawing.
    """
    executor, coordinator = await _build(hass, grid=PHASE_GRID)
    _phases(hass, coordinator, 400)
    coordinator.data = EmhassData(plan=_plan(-5000), last_success=dt_util.utcnow())
    coordinator.control_enabled = True
    await executor.async_apply()
    await hass.async_block_till_done()
    assert calls[-2].data["value"] == 5000

    freezer.tick(timedelta(seconds=61))
    _phases(hass, coordinator, 2600, battery=0)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert coordinator.phase_guard.charge_cap_w == pytest.approx(2550)
    assert executor.last_decision.power_w == pytest.approx(2550)


async def test_a_discharging_battery_is_added_back_to_the_house(hass: HomeAssistant) -> None:
    """House 3400 W on L1, battery discharging 3000 W: the meter shows 2400 W.

    Leaving the discharge out allowed 3150 W of charge, which puts L1 at
    19.35 A once the battery switches over. With it added back, 150 W fits.
    """
    executor, coordinator = await _build(hass, grid=PHASE_GRID)
    _phases(hass, coordinator, 3400 - 1000, 300 - 1000, 300 - 1000, battery=-3000)
    coordinator.data = EmhassData(plan=_plan(-5000), last_success=dt_util.utcnow())

    decision = await executor.async_apply()

    assert coordinator.phase_guard.headroom_w == pytest.approx(150)
    assert decision.action == MODE_SELF_CONSUME


async def test_without_a_battery_sensor_the_clamp_is_off_and_says_so(
    hass: HomeAssistant,
) -> None:
    executor, coordinator = await _build(hass, grid=PHASE_GRID, battery_sensor=False)
    _phases(hass, coordinator, 3500)
    coordinator.data = EmhassData(plan=_plan(-5000), last_success=dt_util.utcnow())

    decision = await executor.async_apply()

    assert executor.phase_clamp_blocker() == "no_battery_sensor"
    assert decision.power_w == 5000
    assert any("clamp off (no_battery_sensor)" in rule for rule in decision.rules)
    # The plan's limit does not need the battery, and still applies.
    assert coordinator._grid_import_limit_w(coordinator.config) is not None


async def test_a_slow_profile_turns_the_clamp_off(hass: HomeAssistant) -> None:
    """A 300 s write interval cannot carry a cut a fuse needs within minutes."""
    executor, coordinator = await _build(hass, grid=PHASE_GRID)
    coordinator.profiles[TEST_INVERTER_KEY].document["control"] = {"min_write_interval_s": 300}
    _phases(hass, coordinator, 3500)
    coordinator.data = EmhassData(plan=_plan(-5000), last_success=dt_util.utcnow())

    decision = await executor.async_apply()

    assert executor.phase_clamp_blocker() == "slow_profile"
    assert decision.power_w == 5000


async def test_an_unreadable_battery_sensor_fails_open(hass: HomeAssistant) -> None:
    executor, coordinator = await _build(hass, grid=PHASE_GRID)
    _phases(hass, coordinator, 3500)
    hass.states.async_set("sensor.battery_power", "unavailable")
    coordinator.phase_guard.async_update()
    coordinator.data = EmhassData(plan=_plan(-5000), last_success=dt_util.utcnow())

    decision = await executor.async_apply()

    assert decision.power_w == 5000
    assert any("battery power unavailable" in rule for rule in decision.rules)


async def test_a_blocked_change_of_action_is_retried_whatever_the_watts(
    hass: HomeAssistant,
) -> None:
    """80 W of charge still has to become self-consumption under a 100 W deadband."""
    executor, coordinator = await _build(hass, grid=PHASE_GRID)
    _phases(hass, coordinator, 3500)
    coordinator.control_enabled = True
    executor.last_decision = Decision(
        action=MODE_SELF_CONSUME, power_w=0.0, planned_charge_w=80.0, phase_cut_w=80.0
    )
    executor._last_applied[AXIS_BATTERY] = _Command(MODE_FORCE_CHARGE, 80.0, dt_util.utcnow())

    assert executor._phase_retarget_needed()


async def test_the_cut_is_released_only_after_the_phases_stay_clear(
    hass: HomeAssistant, calls: list[ServiceCall], freezer
) -> None:
    """A heater cycling on and off must not drag the inverter with it."""
    executor, coordinator = await _build(hass, grid=PHASE_GRID)
    _phases(hass, coordinator, 2600)
    coordinator.data = EmhassData(plan=_plan(-5000), last_success=dt_util.utcnow())
    coordinator.control_enabled = True
    await executor.async_apply()
    await hass.async_block_till_done()
    assert executor.last_decision.power_w == pytest.approx(2550)

    # The heater stops; the battery is still charging at the capped 2550 W.
    _phases(hass, coordinator, 400 + 850, 300 + 850, 300 + 850, battery=2550)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert executor.last_decision.power_w == pytest.approx(2550)

    freezer.tick(timedelta(seconds=61))
    coordinator.phase_guard.async_update()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert executor.last_decision.power_w == 5000
    assert executor.last_decision.phase_cut_w == 0
    assert calls[-2].data["value"] == 5000


async def test_guard_forced_self_consumption_does_not_hold_the_plan_back(
    hass: HomeAssistant, freezer
) -> None:
    """The exit hysteresis is for the plan's own chatter, not the guard's.

    p_grid 500 W is past the 300 W entry threshold but inside the 600 W exit
    one: had the plan itself chosen self-consumption, staying there would be
    right. The guard chose it, so the plan's charge resumes once clear.
    """
    executor, coordinator = await _build(hass, grid=PHASE_GRID)
    _phases(hass, coordinator, 3500)
    coordinator.data = EmhassData(plan=_plan(-500, p_grid=500), last_success=dt_util.utcnow())
    assert (await executor.async_apply()).action == MODE_SELF_CONSUME

    # The heater reading clears at once; the window then has to run out.
    _phases(hass, coordinator, 400)
    freezer.tick(timedelta(seconds=61))
    coordinator.phase_guard.async_update()
    coordinator.data = EmhassData(plan=_plan(-500, p_grid=500), last_success=dt_util.utcnow())
    decision = await executor.async_apply()

    assert decision.action == MODE_FORCE_CHARGE
    assert decision.power_w == 500


async def test_dry_run_still_shows_the_cut(hass: HomeAssistant, calls: list[ServiceCall]) -> None:
    executor, coordinator = await _build(hass, grid=PHASE_GRID)
    _phases(hass, coordinator, 2600)
    coordinator.data = EmhassData(plan=_plan(-5000), last_success=dt_util.utcnow())
    coordinator.control_enabled = False

    decision = await executor.async_apply()

    assert calls == []
    assert decision.phase_cut_w == pytest.approx(2450)


async def test_the_plan_gets_the_lower_of_the_guard_and_a_limit_sensor(
    hass: HomeAssistant,
) -> None:
    _executor, coordinator = await _build(
        hass, grid={**PHASE_GRID, "grid_import_limit_entity": "sensor.limit"}
    )
    # Worst-phase limit: 3200 W today + 3 x (3450 - 2600) W more = 5750 W.
    _phases(hass, coordinator, 2600)
    hass.states.async_set("sensor.limit", "7000")
    assert coordinator._grid_import_limit_w(coordinator.config) == pytest.approx(5750)

    hass.states.async_set("sensor.limit", "4000")
    assert coordinator._grid_import_limit_w(coordinator.config) == 4000


async def test_the_plan_limit_remembers_the_worst_of_the_window(
    hass: HomeAssistant, freezer
) -> None:
    """A dishwasher between heating cycles must not hand the plan the whole fuse."""
    _executor, coordinator = await _build(hass, grid=PHASE_GRID)
    _phases(hass, coordinator, 2600)
    low = coordinator._grid_import_limit_w(coordinator.config)

    freezer.tick(timedelta(minutes=10))
    _phases(hass, coordinator, 400)
    assert coordinator._grid_import_limit_w(coordinator.config) == low

    freezer.tick(timedelta(minutes=31))
    coordinator.phase_guard.async_update()
    assert coordinator._grid_import_limit_w(coordinator.config) > low


async def test_a_phase_hovering_at_its_fuse_is_one_warning_not_one_per_reading(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """16.5 A and 15.7 A alternating is one light overload; 21.7 A escalates it."""
    _executor, coordinator = await _build(hass, grid=PHASE_GRID)
    for watts in (3800, 3600, 3800, 3600, 3800):
        _phases(hass, coordinator, watts)

    def warnings() -> int:
        return sum("A main fuse" in record.message for record in caplog.records)

    assert warnings() == 1
    assert coordinator.phase_guard.overload == "light"

    _phases(hass, coordinator, 5000)
    assert warnings() == 2
    assert coordinator.phase_guard.overload == "heavy"


async def test_a_measured_voltage_converts_watt_readings(hass: HomeAssistant) -> None:
    """A sagging phase is more amps for the same watts -- the unsafe direction."""
    _executor, coordinator = await _build(
        hass, grid={**PHASE_GRID, "phase_voltage_entity": "sensor.voltage_l1"}
    )
    hass.states.async_set("sensor.voltage_l1", "210", {"unit_of_measurement": "V"})
    _phases(hass, coordinator, 2600)

    guard = coordinator.phase_guard
    assert guard.voltage_measured
    assert guard.currents_a[0] == pytest.approx(2600 / 210)
    # 3 x (15 A - 12.38 A) x 210 V, against 2550 W at the fixed 230 V.
    assert guard.headroom_w == pytest.approx(3 * (15 * 210 - 2600))
    assert guard.as_attributes()["voltage_v"] == 210


async def test_an_implausible_voltage_falls_back_to_the_fixed_one(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    _executor, coordinator = await _build(
        hass, grid={**PHASE_GRID, "phase_voltage_entity": "sensor.voltage_l1_l2"}
    )
    hass.states.async_set("sensor.voltage_l1_l2", "400", {"unit_of_measurement": "V"})
    _phases(hass, coordinator, 2600)
    _phases(hass, coordinator, 2600)

    guard = coordinator.phase_guard
    assert not guard.voltage_measured
    assert guard.voltage_v == 230
    assert guard.headroom_w == pytest.approx(2550)
    assert sum("Voltage sensor" in record.message for record in caplog.records) == 1

"""Executor behaviour against a live Home Assistant.

The dry-run gate is the thing most worth proving: while it is off, *no*
service call may reach the user's hardware, and the decision must still be
recorded so it can be compared against whatever is currently in charge.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    CONF_CONTROL_ENTITY,
    CONF_INVERTER,
    CONF_NOMINAL_POWER,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    DOMAIN,
    MODE_FORCE_CHARGE,
    MODE_FORCE_DISCHARGE,
    MODE_IDLE,
    MODE_SELF_CONSUME,
    SUBENTRY_TYPE_DEFERRABLE,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator, EmhassData
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.executor import Executor
from custom_components.emhass_companion.models import Plan
from custom_components.emhass_companion.profiles import async_load_profiles

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


def _plan(p_batt: float, deferrable: float = 0.0, *, minutes_ago: int = 0) -> Plan:
    start = dt_util.utcnow() - timedelta(minutes=minutes_ago)
    return Plan.from_response(
        {
            "status": "ok",
            "generated_at": start.isoformat(),
            "emhass_schema_version": "1.0",
            "plan": [
                {
                    "timestamp": (start - timedelta(minutes=5)).isoformat(),
                    "P_batt": p_batt,
                    "P_deferrable0": deferrable,
                }
            ],
        }
    )


async def _build(
    hass: HomeAssistant,
    *,
    with_load: bool = False,
    inverter: bool = True,
) -> tuple[Executor, EmhassCoordinator]:
    subentries = []
    if with_load:
        subentries.append(
            {
                "subentry_type": SUBENTRY_TYPE_DEFERRABLE,
                "title": "Dishwasher",
                "unique_id": "dishwasher",
                "data": {CONF_NOMINAL_POWER: 2000, CONF_CONTROL_ENTITY: LOAD_SWITCH},
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
    if inverter:
        options[CONF_INVERTER] = {
            CONF_PROFILE: "inverter/mode_select_and_power",
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
    coordinator.data = EmhassData(plan=_plan(0), last_success=dt_util.utcnow())

    hass.states.async_set(MODE_SELECT, "Self Consumption")
    hass.states.async_set(POWER_NUMBER, "0")
    hass.states.async_set(LOAD_SWITCH, "off")

    return Executor(hass, coordinator), coordinator


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
    coordinator.system_mode = MODE_FORCE_DISCHARGE

    decision = await executor.async_apply()
    await hass.async_block_till_done()

    assert decision.action == MODE_FORCE_DISCHARGE
    assert decision.reason == "manual override"
    assert calls[-1].data["option"] == "Forced Discharge"


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


async def test_a_missing_action_is_reported_not_raised(hass: HomeAssistant) -> None:
    executor, coordinator = await _build(hass)
    coordinator.control_enabled = True
    coordinator.profiles["inverter/mode_select_and_power"].document["actions"].pop("idle")
    coordinator.data = EmhassData(plan=_plan(0), last_success=dt_util.utcnow())

    decision = await executor.async_apply()

    assert decision.action == MODE_IDLE
    assert decision.error is not None
    assert "idle" in decision.error


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

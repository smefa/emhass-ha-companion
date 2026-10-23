"""A fallback solve is published, flagged, and kept away from the hardware.

EMHASS answers a time-limited solve with its best incumbent and a failed full
solve with a relaxed plan, and reports both with status "ok". The coordinator
still publishes them so the sensors can show them, but marks them as degraded
and raises a repair. Nothing that drives hardware follows them: the should-run
sensors go unknown, and the next request commits no load to them. The
executor's side is in test_executor.py. A full solve clears all of it.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.binary_sensor import LoadShouldRunBinarySensor
from custom_components.emhass_companion.const import (
    ACTION_MPC,
    CONF_NOMINAL_POWER,
    DOMAIN,
    ISSUE_PLAN_DEGRADED,
    SUBENTRY_TYPE_DEFERRABLE,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator, EmhassData
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.models import LastRun, Plan, PlanRow

STEP = timedelta(minutes=15)
FALLBACK_STATUSES = ["Optimal (Relaxed)", "Optimal (Incumbent)"]


def _coordinator(hass: HomeAssistant) -> EmhassCoordinator:
    """One deferrable load, so the plan's P_deferrable0 maps onto something."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_DEFERRABLE,
                "title": "Car",
                "unique_id": "car",
                "data": {CONF_NOMINAL_POWER: 10000},
            }
        ],
    )
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    return EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)


def _car(coordinator: EmhassCoordinator):
    return next(iter(coordinator.loads.all()))


def _plan(optim_status: str, *, car_power: float = 0.0) -> Plan:
    """A plan whose rows span now, so it reads as the current plan.

    EMHASS stamps one status on every row of a run.
    """
    now = dt_util.utcnow()
    return Plan(
        generated_at=now,
        schema_version="1.0",
        rows=[
            PlanRow(
                timestamp=now - STEP + index * STEP,
                p_pv=0.0,
                p_load=0.0,
                optim_status=optim_status,
                deferrables=(car_power,),
            )
            for index in range(4)
        ],
    )


def _answer(optim_status: str | None, *, infeasible: bool = False):
    """An EMHASS stand-in. An infeasible solve comes back with no plan."""

    async def _optimize(action: str, _payload: dict) -> tuple[LastRun, Plan | None]:
        plan = None if optim_status is None else _plan(optim_status)
        return LastRun(status="ok", action=action, infeasible=infeasible), plan

    return _optimize


def _issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_PLAN_DEGRADED)


@pytest.mark.parametrize("optim_status", FALLBACK_STATUSES)
async def test_a_fallback_solve_is_published_and_flagged(
    hass: HomeAssistant, optim_status: str
) -> None:
    coordinator = _coordinator(hass)
    coordinator.client.async_optimize = _answer(optim_status)

    data = await coordinator.async_run(ACTION_MPC)

    # Published, so the sensors can show what EMHASS produced and the run
    # still counts as healthy. Only the hardware-facing paths refuse it.
    assert data.plan is not None
    assert data.last_success is not None
    assert coordinator.plan_is_degraded is True

    issue = _issue(hass)
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.translation_key == ISSUE_PLAN_DEGRADED
    assert issue.translation_placeholders == {"action": ACTION_MPC, "status": optim_status}


async def test_a_full_solve_clears_the_repair(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.client.async_optimize = _answer("Optimal (Relaxed)")
    await coordinator.async_run(ACTION_MPC)
    assert _issue(hass) is not None

    coordinator.client.async_optimize = _answer("Optimal")
    await coordinator.async_run(ACTION_MPC)

    assert _issue(hass) is None
    assert coordinator.plan_is_degraded is False


async def test_an_infeasible_run_keeps_a_fallback_plan_flagged(hass: HomeAssistant) -> None:
    """An infeasible run keeps the previous plan. If that plan is a fallback,
    it is still a fallback, so every hardware-facing path must keep refusing it."""
    coordinator = _coordinator(hass)
    coordinator.client.async_optimize = _answer("Optimal (Relaxed)")
    await coordinator.async_run(ACTION_MPC)

    coordinator.client.async_optimize = _answer(None, infeasible=True)
    data = await coordinator.async_run(ACTION_MPC)

    assert data.plan is not None
    assert coordinator.plan_is_degraded is True
    assert _issue(hass) is not None


async def test_should_run_follows_a_full_plan(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    car = _car(coordinator)
    coordinator.data = EmhassData(
        plan=_plan("Optimal", car_power=10000.0),
        load_order=[car.subentry_id],
        last_success=dt_util.utcnow(),
    )

    assert LoadShouldRunBinarySensor(coordinator, car).is_on is True


@pytest.mark.parametrize("optim_status", FALLBACK_STATUSES)
async def test_should_run_is_unknown_for_a_fallback_solve(
    hass: HomeAssistant, optim_status: str
) -> None:
    """Automations follow should_run, so a fallback must not switch them on."""
    coordinator = _coordinator(hass)
    car = _car(coordinator)
    coordinator.data = EmhassData(
        plan=_plan(optim_status, car_power=10000.0),
        load_order=[car.subentry_id],
        last_success=dt_util.utcnow(),
    )

    sensor = LoadShouldRunBinarySensor(coordinator, car)
    assert sensor.is_on is None
    assert sensor.extra_state_attributes["reason"] == "plan is a fallback solve"


@pytest.mark.parametrize("optim_status", FALLBACK_STATUSES)
async def test_a_fallback_plan_commits_no_load(hass: HomeAssistant, optim_status: str) -> None:
    """The next request pins a load only to a plan the loads were acted on.
    A fallback plan's rows would otherwise be sent back to EMHASS as fixed."""
    coordinator = _coordinator(hass)
    car = _car(coordinator)
    coordinator.data = EmhassData(
        plan=_plan("Optimal", car_power=10000.0),
        load_order=[car.subentry_id],
        last_success=dt_util.utcnow(),
    )
    await coordinator._build(ACTION_MPC)
    assert car.plan_scheduled_now is True

    coordinator.data = EmhassData(
        plan=_plan(optim_status, car_power=10000.0),
        load_order=[car.subentry_id],
        last_success=dt_util.utcnow(),
    )
    await coordinator._build(ACTION_MPC)

    assert car.plan_scheduled_now is False
    assert car.plan_power_now_w == 0.0

"""The infeasible-run repair names its cause.

EMHASS only says "infeasible", for the whole problem. On an infeasible run the
coordinator runs infeasibility.py over the request it just sent and switches
the repair issue to the headline finding's own translation. The diagnosis must
never cost the run: if it fails, the issue falls back to the generic text.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion import coordinator as coordinator_module
from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    ACTION_MPC,
    CONF_NOMINAL_POWER,
    DOMAIN,
    ISSUE_OPTIMIZATION_INFEASIBLE,
    SUBENTRY_TYPE_DEFERRABLE,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator, EmhassData
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.models import LastRun, Plan, PlanRow


def _coordinator(hass: HomeAssistant) -> EmhassCoordinator:
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


def _answer(*, infeasible: bool, pin_car: bool = False):
    """An EMHASS stand-in. With ``pin_car``, it first turns the request it was
    sent into the 2026-10-01 22:00 shape at timestep 0: the car pinned at
    10 kW against a 7.1 kW grid limit, no PV and no battery. The coordinator
    diagnoses that same dict, so this exercises the real checks end to end."""

    async def _optimize(action: str, payload: dict[str, Any]) -> tuple[LastRun, None]:
        if pin_car:
            payload["def_current_power"] = [10000.0]
            payload["maximum_power_from_grid"] = 7102
            payload["pv_power_forecast"] = [0.0] * 96
            payload["load_power_forecast"] = [900.0] * 96
            payload["set_use_battery"] = False
        return LastRun(status="ok", action=action, infeasible=infeasible), None

    return _optimize


def _issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_OPTIMIZATION_INFEASIBLE)


async def test_an_infeasible_run_names_its_cause(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.client.async_optimize = _answer(infeasible=True, pin_car=True)

    data = await coordinator.async_run(ACTION_MPC)

    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == f"{ISSUE_OPTIMIZATION_INFEASIBLE}_timestep0_deficit"
    placeholders = issue.translation_placeholders
    assert placeholders["action"] == ACTION_MPC
    assert placeholders["pins"] == "Car 10.0 kW"
    assert placeholders["grid"] == "7.1 kW"
    assert placeholders["house"] == "900 W"
    assert int(placeholders["total"]) >= 1
    assert data.last_infeasibility
    assert data.last_infeasibility[0].code == "timestep0_deficit"


async def test_a_crash_in_the_diagnosis_still_raises_the_generic_issue(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> list:
        raise RuntimeError("diagnosis bug")

    monkeypatch.setattr(coordinator_module, "diagnose", _boom)
    coordinator = _coordinator(hass)
    coordinator.client.async_optimize = _answer(infeasible=True, pin_car=True)

    data = await coordinator.async_run(ACTION_MPC)

    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == ISSUE_OPTIMIZATION_INFEASIBLE
    assert issue.translation_placeholders == {"action": ACTION_MPC}
    assert data.last_infeasibility == []


async def test_no_finding_keeps_the_generic_text(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.client.async_optimize = _answer(infeasible=True)

    await coordinator.async_run(ACTION_MPC)

    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == ISSUE_OPTIMIZATION_INFEASIBLE


async def test_a_feasible_run_clears_the_issue_and_the_findings(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.client.async_optimize = _answer(infeasible=True, pin_car=True)
    await coordinator.async_run(ACTION_MPC)
    assert _issue(hass) is not None

    coordinator.client.async_optimize = _answer(infeasible=False)
    data = await coordinator.async_run(ACTION_MPC)

    assert _issue(hass) is None
    assert data.last_infeasibility == []


async def test_an_infeasible_run_does_not_renew_the_kept_plan(hass: HomeAssistant) -> None:
    """EMHASS answers "ok" to an infeasible solve. Stamping the kept plan fresh
    on that would let a string of failures hold it executable indefinitely,
    and reading it through this request's load order would mismatch its
    P_deferrable{k} columns as soon as a load was added or removed."""
    coordinator = _coordinator(hass)
    solved = dt_util.utcnow() - timedelta(hours=2)
    kept = Plan(
        generated_at=solved,
        schema_version="1.0",
        rows=[PlanRow(timestamp=solved, deferrables=(0.0, 2000.0))],
    )
    coordinator.data = EmhassData(
        plan=kept, last_success=solved, load_order=["removed-load", "car"]
    )
    coordinator.client.async_optimize = _answer(infeasible=True)

    data = await coordinator.async_run(ACTION_MPC)

    assert data.plan is kept
    assert data.last_success == solved
    assert data.load_order == ["removed-load", "car"]


async def test_an_infeasible_first_run_leaves_no_plan_fresh(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.client.async_optimize = _answer(infeasible=True)

    data = await coordinator.async_run(ACTION_MPC)

    assert data.plan is None
    assert data.last_success is None

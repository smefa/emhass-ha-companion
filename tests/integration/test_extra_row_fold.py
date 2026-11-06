"""Extra rows for later daily windows are folded into their parent load.

EMHASS gets one internal row per additional window and answers with one
``P_deferrable`` column for each. The coordinator adds them into the parent's
column on a successful solve, so nothing downstream sees the extras.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    ACTION_MPC,
    CONF_NOMINAL_POWER,
    DOMAIN,
    SUBENTRY_TYPE_DEFERRABLE,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.models import LastRun, Plan, PlanRow
from custom_components.emhass_companion.payload import ExtraRow

START = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)


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
    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)

    build = coordinator._build

    async def _build_with_extra(action: str) -> Any:
        inputs, built = await build(action)
        return inputs, replace(built, extra_rows=[ExtraRow(0, "Car (day 2)")])

    coordinator._build = _build_with_extra
    return coordinator


def _plan(columns: list[float]) -> Plan:
    rows = [
        PlanRow(timestamp=START + timedelta(minutes=30 * i), deferrables=(value, *columns[1:]))
        for i, value in enumerate([columns[0], columns[0] + 1])
    ]
    return Plan(generated_at=START, schema_version="1.0.0", rows=rows)


def _answer(plan: Plan, *, infeasible: bool = False):
    async def _optimize(action: str, payload: dict[str, Any]) -> tuple[LastRun, Plan]:
        return LastRun(status="ok", action=action, infeasible=infeasible), plan

    return _optimize


async def test_success_plan_is_folded_into_the_parent(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.client.async_optimize = _answer(_plan([100.0, 50.0]))

    data = await coordinator.async_run(ACTION_MPC)

    assert data.plan is not None
    assert [row.deferrables for row in data.plan.rows] == [(150.0,), (151.0,)]
    assert len(data.load_order) == 1
    assert data.extra_row_names == ["Car (day 2)"]


async def test_plan_with_too_few_columns_is_dropped(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.client.async_optimize = _answer(
        Plan(
            generated_at=START,
            schema_version="1.0.0",
            rows=[PlanRow(timestamp=START, deferrables=(100.0,))],
        )
    )

    data = await coordinator.async_run(ACTION_MPC)

    assert data.plan is None


async def test_infeasible_run_keeps_the_folded_plan_without_refolding(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    coordinator.client.async_optimize = _answer(_plan([100.0, 50.0]))
    first = await coordinator.async_run(ACTION_MPC)
    coordinator.data = first

    coordinator.client.async_optimize = _answer(_plan([1.0, 2.0]), infeasible=True)
    second = await coordinator.async_run(ACTION_MPC)

    assert second.plan is first.plan
    assert [row.deferrables for row in second.plan.rows] == [(150.0,), (151.0,)]


async def test_infeasible_run_reports_the_rejected_request_order(hass: HomeAssistant) -> None:
    # The kept plan has only Car's column, but the rejected request also has Lamp.
    coordinator = _coordinator(hass)
    coordinator.client.async_optimize = _answer(_plan([100.0, 50.0]))
    coordinator.data = first = await coordinator.async_run(ACTION_MPC)
    car = first.load_order[0]

    build = coordinator._build

    async def _build_with_lamp(action: str) -> Any:
        inputs, built = await build(action)
        return inputs, replace(built, load_order=[*built.load_order, "lamp"])

    coordinator._build = _build_with_lamp
    coordinator.client.async_optimize = _answer(_plan([1.0, 2.0]), infeasible=True)
    second = await coordinator.async_run(ACTION_MPC)

    assert second.load_order == [car]
    assert second.payload_order == [car, "lamp"]
    assert second.extra_row_names == ["Car (day 2)"]

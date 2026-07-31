"""The "trust the plan" fallback for a load with no running_source.

EMHASS keeps no memory of its own between calls: without something telling
Companion a load already ran, ``completed_timesteps`` stays 0 forever and every
MPC cycle re-proposes the full ``operating_hours`` target as if nothing had
happened yet. A power sensor or control entity fixes this by observing the real
world. With neither, this fallback is the difference between that and treating
Companion's own previous plan as a (weaker, unverified) substitute.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    ACTION_MPC,
    CONF_CONTROL_ENTITY,
    CONF_NOMINAL_POWER,
    DOMAIN,
    SUBENTRY_TYPE_DEFERRABLE,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator, EmhassData
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.models import Plan, PlanRow

T0 = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
STEP = timedelta(minutes=15)


def _plan(powers: list[float], *, start: datetime = T0) -> Plan:
    return Plan(
        generated_at=start,
        schema_version="1.0",
        rows=[
            PlanRow(timestamp=start + STEP * i, deferrables=(power,))
            for i, power in enumerate(powers)
        ],
    )


# --- DeferrableRuntime.assume_from_plan --------------------------------------


def _load(**overrides):
    from custom_components.emhass_companion.deferrable import DeferrableRuntime

    defaults = {"subentry_id": "abc", "name": "Dishwasher", "nominal_power_w": 2000.0}
    return DeferrableRuntime(**{**defaults, **overrides})


def test_a_scheduled_on_off_run_is_folded_into_the_accumulator():
    load = _load()
    plan = _plan([2000, 2000, 2000, 0, 0])  # on for 3 rows, then off

    load.assume_from_plan(plan.rows, index=0, now=T0 + timedelta(hours=2))

    assert load.elapsed_today(T0 + timedelta(hours=2)) == timedelta(minutes=45)


def test_rows_after_now_are_not_assumed():
    load = _load()
    plan = _plan([2000, 2000, 2000, 2000])

    # Only the first two rows (T0, T0+15) are at or before "now".
    load.assume_from_plan(plan.rows, index=0, now=T0 + timedelta(minutes=20))

    assert load.elapsed_today(T0 + timedelta(minutes=20)) == timedelta(minutes=20)


def test_replaying_the_same_rows_again_does_not_double_count():
    """The same Plan object recurs whenever EMHASS keeps reporting infeasible
    (coordinator.py carries the previous plan forward) -- this must be safe to
    call again with an unchanged or extended row set."""
    load = _load()
    plan = _plan([2000, 2000, 0, 0])

    now = T0 + timedelta(hours=1)
    load.assume_from_plan(plan.rows, index=0, now=now)
    first = load.elapsed_today(now)

    load.assume_from_plan(plan.rows, index=0, now=now)
    assert load.elapsed_today(now) == first == timedelta(minutes=30)


def test_a_later_call_only_replays_the_newly_elapsed_slice():
    load = _load()
    plan = _plan([2000, 2000, 2000, 2000, 0])

    load.assume_from_plan(plan.rows, index=0, now=T0 + timedelta(minutes=15))
    load.assume_from_plan(plan.rows, index=0, now=T0 + timedelta(hours=1))

    assert load.elapsed_today(T0 + timedelta(hours=1)) == timedelta(minutes=60)


def test_uses_this_loads_own_column_not_anothers():
    load = _load()
    plan = Plan(
        generated_at=T0,
        schema_version="1.0",
        rows=[PlanRow(timestamp=T0, deferrables=(0.0, 2000.0))],
    )

    load.assume_from_plan(plan.rows, index=0, now=T0 + timedelta(minutes=15))

    assert load.elapsed_today(T0 + timedelta(minutes=15)) == timedelta()


# --- DeferrableRegistry.assume_from_plan -------------------------------------


def _entry(*loads: dict) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_DEFERRABLE,
                "title": load["title"],
                "unique_id": load["title"],
                "data": load["data"],
            }
            for load in loads
        ],
    )


async def test_registry_skips_a_load_with_a_running_source(hass: HomeAssistant) -> None:
    """A real signal always wins -- the fallback must never overwrite it."""
    entry = _entry(
        {
            "title": "Car",
            "data": {CONF_NOMINAL_POWER: 2000, CONF_CONTROL_ENTITY: "switch.car"},
        }
    )
    entry.add_to_hass(hass)
    registry = DeferrableRegistry(hass, entry)
    registry.sync()
    load = registry.get(next(iter(entry.subentries)))

    plan = _plan([2000, 2000])
    registry.assume_from_plan(plan, [load.subentry_id], T0 + timedelta(minutes=30))

    assert load.elapsed_today(T0 + timedelta(minutes=30)) == timedelta()
    assert load.plan_assumed_until is None


async def test_registry_assumes_for_a_sourceless_load(hass: HomeAssistant) -> None:
    entry = _entry({"title": "Dishwasher", "data": {CONF_NOMINAL_POWER: 2000}})
    entry.add_to_hass(hass)
    registry = DeferrableRegistry(hass, entry)
    registry.sync()
    load = registry.get(next(iter(entry.subentries)))

    plan = _plan([2000, 2000, 0])
    registry.assume_from_plan(plan, [load.subentry_id], T0 + timedelta(minutes=30))

    assert load.elapsed_today(T0 + timedelta(minutes=30)) == timedelta(minutes=30)


async def test_registry_is_a_no_op_with_no_plan(hass: HomeAssistant) -> None:
    entry = _entry({"title": "Dishwasher", "data": {CONF_NOMINAL_POWER: 2000}})
    entry.add_to_hass(hass)
    registry = DeferrableRegistry(hass, entry)
    registry.sync()
    load = registry.get(next(iter(entry.subentries)))

    registry.assume_from_plan(None, [load.subentry_id], T0)

    assert load.plan_assumed_until is None


async def test_registry_skips_a_load_missing_from_load_order(hass: HomeAssistant) -> None:
    """A load added after the plan it is being matched against was built."""
    entry = _entry({"title": "Dishwasher", "data": {CONF_NOMINAL_POWER: 2000}})
    entry.add_to_hass(hass)
    registry = DeferrableRegistry(hass, entry)
    registry.sync()
    load = registry.get(next(iter(entry.subentries)))

    plan = _plan([2000])
    registry.assume_from_plan(plan, ["some-other-load"], T0 + timedelta(minutes=15))

    assert load.elapsed_today(T0 + timedelta(minutes=15)) == timedelta()


# --- wired into the coordinator ----------------------------------------------


async def test_coordinator_feeds_the_previous_plan_in_before_building(
    hass: HomeAssistant,
) -> None:
    """The whole point: a sourceless load's completed_timesteps must reach the
    *next* outgoing payload, using the plan and load_order that were actually
    in effect while its now-elapsed rows played out."""
    entry = _entry({"title": "Dishwasher", "data": {CONF_NOMINAL_POWER: 2000}})
    entry.add_to_hass(hass)

    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    subentry_id = next(iter(entry.subentries))

    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)
    # _build() reads dt_util.utcnow() internally for "now", so the plan has to
    # be anchored to real wall-clock time rather than the fixed T0 used above.
    start = dt_util.utcnow() - timedelta(hours=1)
    coordinator.data = EmhassData(
        plan=_plan([2000, 2000, 2000, 0], start=start),
        last_success=start,
        load_order=[subentry_id],
    )

    inputs, _built = await coordinator._build(ACTION_MPC)

    projected = next(load for load in inputs.loads if load.subentry_id == subentry_id)
    assert projected.completed_timesteps > 0
    assert projected.current_state is False  # the plan's own last row was "off"

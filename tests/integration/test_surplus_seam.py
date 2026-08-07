"""The surplus sensors keep reading across the gap between two plans.

EMHASS starts its horizon at the next timestep boundary after launch, so the
instant a plan is published "now" sits before its first row and every sensor
reading ``value_at(now)`` gets nothing back. That is a gap at every single
run, and it is why the coordinator carries one row of the outgoing plan
across the seam -- see ``surplus.seam_carry`` for why a fabricated zero would
be worse than the gap it filled. The unit tests cover the carry itself; what
matters here is that a real run actually captures it, at the one moment the
outgoing plan is still the coordinator's own data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import ACTION_MPC, DOMAIN
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.models import LastRun, Plan, PlanRow

STEP = timedelta(minutes=15)
START = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)


def _coordinator(hass: HomeAssistant) -> EmhassCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"})
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    return EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)


def _plan(first_step: int, *surplus_values: float) -> Plan:
    """A plan whose rows start ``first_step`` steps after ``START``.

    House load is held at zero, so each row's spare PV is the value given.
    """
    return Plan(
        generated_at=START,
        schema_version="1.0",
        rows=[
            PlanRow(
                timestamp=START + (first_step + index) * STEP,
                p_pv=value,
                p_load=0.0,
                deferrables=(),
            )
            for index, value in enumerate(surplus_values)
        ],
    )


async def _run_with(coordinator: EmhassCoordinator, plan: Plan) -> None:
    async def _optimize(_action: str, _payload: dict) -> tuple[LastRun, Plan]:
        return LastRun(status="ok"), plan

    coordinator.client.async_optimize = _optimize
    await coordinator.async_run(ACTION_MPC)


async def test_a_new_plan_does_not_blank_the_surplus_reading(
    hass: HomeAssistant,
) -> None:
    """The bug this closes: mid-morning, sun blazing, and the sensor reads
    unknown for the minutes between the run finishing and its horizon
    starting."""
    coordinator = _coordinator(hass)
    seam = START + STEP + timedelta(minutes=7)

    await _run_with(coordinator, _plan(0, 600.0, 900.0, 1200.0))
    assert coordinator.surplus_series().value_at(seam) == 900.0

    # The next run's horizon has moved past the seam, as it does every time.
    await _run_with(coordinator, _plan(2, 1500.0, 1600.0))

    assert coordinator.surplus_series().value_at(seam) == 900.0
    assert coordinator.surplus_series().value_at(START + 2 * STEP) == 1500.0


async def test_the_carried_row_never_outlives_the_seam_it_bridges(
    hass: HomeAssistant,
) -> None:
    """Run after run, the carry stays one row deep and always belongs to the
    plan immediately before -- it must not pile up a tail of stale forecast
    behind every horizon, nor keep answering once the horizon covers now."""
    coordinator = _coordinator(hass)

    await _run_with(coordinator, _plan(0, 600.0))
    for offset in range(1, 6):
        await _run_with(coordinator, _plan(offset, 100.0 * offset))
        assert len(coordinator._surplus_carry) == 1

    # Five runs in, the series is the newest plan plus exactly one bridge row,
    # not six runs' worth of accumulated history.
    assert coordinator.surplus_series().values == (400.0, 500.0)


async def test_no_plan_still_reads_unknown_rather_than_a_resurrected_row(
    hass: HomeAssistant,
) -> None:
    """ "We do not know" and "there is no spare sun" drive different
    automations, so a run that comes back without a usable plan must not be
    papered over with the last one's value."""
    coordinator = _coordinator(hass)

    await _run_with(coordinator, _plan(0, 600.0, 900.0))
    assert coordinator.surplus_series().value_at(START) == 600.0

    async def _no_plan(_action: str, _payload: dict) -> tuple[LastRun, None]:
        return LastRun(status="ok"), None

    coordinator.client.async_optimize = _no_plan
    await coordinator.async_run(ACTION_MPC)

    assert not coordinator.surplus_series()
    assert coordinator.surplus_series().value_at(START) is None

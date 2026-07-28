"""Tests for per-load sensors, driven directly against a coordinator.

Nothing in the suite previously instantiated these entity classes at all --
`LoadNextStartSensor` appeared only as a translation-key string in
test_packaging.py. That gap is exactly how its "off now, on at the very next
row" bug went unnoticed: the case a next-start sensor exists to report was
also the one case never exercised.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    CONF_NOMINAL_POWER,
    DOMAIN,
    SUBENTRY_TYPE_DEFERRABLE,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator, EmhassData
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.models import Plan
from custom_components.emhass_companion.sensor import LoadNextStartSensor

STEP = timedelta(minutes=30)


def _plan(*, now: object, powers: list[float]) -> Plan:
    """A plan with one row per entry in ``powers``, starting 15 min before now."""
    start = now - timedelta(minutes=15)
    rows = [
        {
            "timestamp": (start + STEP * index).isoformat(),
            "P_deferrable0": power,
        }
        for index, power in enumerate(powers)
    ]
    return Plan.from_response(
        {
            "status": "ok",
            "generated_at": start.isoformat(),
            "emhass_schema_version": "1.0",
            "plan": rows,
        }
    )


async def _sensor(hass: HomeAssistant, powers: list[float]) -> LoadNextStartSensor:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_DEFERRABLE,
                "title": "Dishwasher",
                "unique_id": "dishwasher",
                "data": {CONF_NOMINAL_POWER: 2000},
            }
        ],
    )
    entry.add_to_hass(hass)

    loads = DeferrableRegistry(hass, entry)
    loads.sync()

    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)
    subentry_id = next(iter(entry.subentries))
    coordinator.data = EmhassData(
        plan=_plan(now=dt_util.utcnow(), powers=powers),
        last_success=dt_util.utcnow(),
        load_order=[subentry_id],
    )

    return LoadNextStartSensor(coordinator, loads.get(subentry_id))


# --- the bug: off now, on at the very next row --------------------------------


async def test_a_start_at_the_very_next_row_is_reported(hass: HomeAssistant) -> None:
    """The exact case the sensor exists for, and the one the bug swallowed."""
    sensor = await _sensor(hass, powers=[0, 2000, 2000])

    value = sensor.native_value

    assert value is not None
    plan = sensor.coordinator.data.plan
    assert value == plan.rows[1].timestamp


async def test_a_start_two_rows_out_is_still_reported(hass: HomeAssistant) -> None:
    sensor = await _sensor(hass, powers=[0, 0, 2000])

    value = sensor.native_value

    plan = sensor.coordinator.data.plan
    assert value == plan.rows[2].timestamp


# --- a run already in progress is not a "start" -------------------------------


async def test_a_load_already_running_reports_no_start_for_that_run(
    hass: HomeAssistant,
) -> None:
    """Continuation must not be reported as an upcoming start."""
    sensor = await _sensor(hass, powers=[2000, 2000, 2000])

    assert sensor.native_value is None


async def test_a_later_start_after_a_running_load_turns_off_is_reported(
    hass: HomeAssistant,
) -> None:
    """Running now, then off, then on again -- the second on is the real start."""
    sensor = await _sensor(hass, powers=[2000, 0, 2000])

    value = sensor.native_value

    plan = sensor.coordinator.data.plan
    assert value == plan.rows[2].timestamp


# --- no start at all -----------------------------------------------------------


async def test_a_load_that_never_runs_reports_no_start(hass: HomeAssistant) -> None:
    sensor = await _sensor(hass, powers=[0, 0, 0])
    assert sensor.native_value is None


async def test_no_plan_reports_no_start(hass: HomeAssistant) -> None:
    sensor = await _sensor(hass, powers=[0, 2000])
    sensor.coordinator.data = EmhassData(plan=None)
    assert sensor.native_value is None

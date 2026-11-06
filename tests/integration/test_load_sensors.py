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
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    CONF_NOMINAL_POWER,
    DOMAIN,
    LOAD_TYPE_THERMAL,
    SUBENTRY_TYPE_DEFERRABLE,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator, EmhassData
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.models import Plan
from custom_components.emhass_companion.sensor import (
    LoadMeasuredTemperatureSensor,
    LoadNextStartSensor,
    LoadPlannedTemperatureSensor,
)

STEP = timedelta(minutes=30)


def _plan(*, now: object, powers: list[float], offset: timedelta = timedelta(minutes=-15)) -> Plan:
    """A plan with one row per entry in ``powers``, starting ``offset`` from now."""
    start = now + offset
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


async def _sensor(
    hass: HomeAssistant,
    powers: list[float],
    offset: timedelta = timedelta(minutes=-15),
) -> LoadNextStartSensor:
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
        plan=_plan(now=dt_util.utcnow(), powers=powers, offset=offset),
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


async def test_a_start_at_row_zero_ahead_of_now_is_reported(hass: HomeAssistant) -> None:
    """Plan stamped slightly in the future: row_at clamps now to row zero.

    Seeding from that clamped row made a start at row zero look like a
    continuation, so the sensor said "not_scheduled".
    """
    sensor = await _sensor(hass, powers=[2000, 2000, 0], offset=timedelta(minutes=8))

    plan = sensor.coordinator.data.plan
    assert sensor.native_value == plan.rows[0].timestamp
    assert sensor.extra_state_attributes is None


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


async def test_a_running_load_is_not_reported_as_starting_at_a_future_row_zero(
    hass: HomeAssistant,
) -> None:
    """Already running and the re-solve's plan opens next step: a continuation."""
    sensor = await _sensor(hass, powers=[2000, 2000, 2000], offset=timedelta(minutes=8))
    sensor.load.running_since = dt_util.utcnow() - timedelta(minutes=2)

    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {"reason": "already_running"}


@pytest.mark.parametrize("reading", ["unknown", "unavailable", "nan", "inf", "bad"])
async def test_measured_tank_temperature_unavailable(hass, reading):
    base = await _sensor(hass, powers=[0])
    base.load.temperature_sensor = "sensor.tank_probe"
    sensor = LoadMeasuredTemperatureSensor(base.coordinator, base.load)
    sensor.hass = hass
    hass.states.async_set("sensor.tank_probe", reading)
    assert sensor.native_value is None
    assert not sensor.available
    assert sensor.extra_state_attributes == {"source_entity": "sensor.tank_probe"}


async def test_measured_tank_temperature_tracks_source_without_plan(hass):
    base = await _sensor(hass, powers=[0])
    base.load.temperature_sensor = "sensor.tank_probe"
    sensor = LoadMeasuredTemperatureSensor(base.coordinator, base.load)
    sensor.hass = hass
    base.coordinator.data = None
    assert not sensor.available
    hass.states.async_set("sensor.tank_probe", "140", {"unit_of_measurement": "°F"})
    assert sensor.available
    assert sensor.native_value == 140
    assert sensor.native_unit_of_measurement == "°F"
    hass.states.async_set("sensor.tank_probe", "61", {"unit_of_measurement": "°C"})
    assert sensor.native_value == 61
    assert sensor.native_unit_of_measurement == "°C"


async def test_planned_temperature_carries_the_comfort_band_it_was_solved_against(hass):
    base = await _sensor(hass, powers=[0, 0, 0])
    base.load.load_type = LOAD_TYPE_THERMAL
    base.load.setback_temperature = 18.0
    base.load.comfort_temperature = 21.0
    base.load.max_temperature = 24.0
    sensor = LoadPlannedTemperatureSensor(base.coordinator, base.load)

    attributes = sensor.extra_state_attributes

    rows = base.coordinator.data.plan.rows
    assert len(attributes["min_forecast"]) == len(rows)
    assert len(attributes["max_forecast"]) == len(rows)
    assert {point["value"] for point in attributes["max_forecast"]} == {24.0}
    assert all(18.0 <= point["value"] <= 21.0 for point in attributes["min_forecast"])


async def test_planned_temperature_has_no_band_without_a_plan(hass):
    base = await _sensor(hass, powers=[0])
    base.load.load_type = LOAD_TYPE_THERMAL
    base.coordinator.data = EmhassData(plan=None)
    sensor = LoadPlannedTemperatureSensor(base.coordinator, base.load)

    assert sensor.extra_state_attributes == {"forecast": []}

"""Energy lands in the billing period it flowed in.

A meter reading says how much flowed *since the previous one*, so a reading
taken just after a boundary carries energy from both sides of it. These pin
the three places that has to be split or attributed right -- a demand bucket,
a price change, midnight -- and the clock tick that keeps a steady load from
going unrecorded when nothing changes state at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.emhass_companion.const import DOMAIN
from custom_components.emhass_companion.metering import Meter, MeterSpec, SavingsTracker
from custom_components.emhass_companion.peaks import AGGREGATE_MAX, PeakTracker
from custom_components.emhass_companion.savings import Prices

POWER = "sensor.grid_power"
IMPORT = "sensor.grid_import_energy"
EXPORT = "sensor.grid_export_energy"


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"})
    entry.add_to_hass(hass)
    return entry


def _set(hass: HomeAssistant, entity_id: str, value: float, unit: str) -> None:
    hass.states.async_set(entity_id, str(value), {"unit_of_measurement": unit}, force_update=True)


def _peaks(hass: HomeAssistant) -> PeakTracker:
    return PeakTracker(
        hass,
        _entry(hass),
        Meter(POWER, kind="power"),
        interval=timedelta(hours=1),
        aggregate=AGGREGATE_MAX,
        top_n=1,
        distinct_days=False,
        in_window=lambda _when: True,
    )


# -- demand buckets -------------------------------------------------------------


async def test_a_reading_across_the_hour_is_split_between_the_buckets(
    hass: HomeAssistant, freezer
) -> None:
    """A steady 6 kW read once a minute, half a minute off the hour. The
    reading at 11:00:30 carries 30 s of the 10:00 hour; booked wholly to the
    new bucket, the hour read 5.95 kW."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 8, 3, 10, 0, tzinfo=UTC))
    _set(hass, POWER, 6000, "W")
    tracker = _peaks(hass)
    tracker.async_start()

    freezer.tick(timedelta(seconds=30))
    for _ in range(61):  # 10:00:30, 10:01:30, ..., 11:00:30
        _set(hass, POWER, 6000, "W")
        await hass.async_block_till_done()
        freezer.tick(timedelta(minutes=1))

    assert tracker.current_aggregate_kw == pytest.approx(6.0)
    assert tracker.open_interval_kwh == pytest.approx(0.05)  # 11:00:00-11:00:30
    tracker.async_stop()


async def test_a_steady_load_that_never_changes_state_is_still_recorded(
    hass: HomeAssistant, freezer
) -> None:
    """Home Assistant fires no state change for an unchanged value. Without
    the clock tick a steady 6 kW was never settled, and once silent for longer
    than the restore gap, integrated as nothing at all."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 8, 3, 10, 0, tzinfo=UTC))
    _set(hass, POWER, 6000, "W")
    tracker = _peaks(hass)
    tracker.async_start()

    for _ in range(12):  # ticks at 10:05 ... 11:00
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    assert tracker.current_aggregate_kw == pytest.approx(6.0)
    tracker.async_stop()


# -- the money ledger -------------------------------------------------------------

BOUNDARY = datetime(2026, 8, 3, 11, 0, tzinfo=UTC)


def _two_rate(when: datetime) -> Prices:
    return Prices(buy=1.0 if when < BOUNDARY else 3.0, sell=0.0)


def _savings(hass: HomeAssistant) -> SavingsTracker:
    return SavingsTracker(
        hass,
        _entry(hass),
        MeterSpec(
            grid_import=Meter(IMPORT, kind="energy"), grid_export=Meter(EXPORT, kind="energy")
        ),
        soc_entity=None,
        capacity_kwh=None,
        prices=_two_rate,
        plan_forecast=lambda _now, _window: None,
        add_price_listener=lambda _listener: lambda: None,
    )


async def _started_savings(hass: HomeAssistant, freezer, when: datetime) -> SavingsTracker:
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(when)
    _set(hass, IMPORT, 0.0, "kWh")
    _set(hass, EXPORT, 0.0, "kWh")
    tracker = _savings(hass)
    tracker.async_start()
    return tracker


async def _read(hass: HomeAssistant, freezer, when: datetime, kwh: float) -> None:
    freezer.move_to(when)
    _set(hass, IMPORT, kwh, "kWh")
    await hass.async_block_till_done()


async def test_a_span_is_priced_at_the_rate_in_force_when_it_began(
    hass: HomeAssistant, freezer
) -> None:
    """Priced at the reading's own instant, the span a reading closes cost the
    *next* period's rate whenever a price change fell inside it -- the dear
    hour charged for energy drawn in the cheap one."""
    tracker = await _started_savings(hass, freezer, BOUNDARY - timedelta(minutes=1))

    await _read(hass, freezer, BOUNDARY + timedelta(seconds=20), 0.1)  # began at 1.0
    await _read(hass, freezer, BOUNDARY + timedelta(minutes=1), 0.2)  # began at 3.0

    assert tracker.ledger.actual_cost == pytest.approx(0.1 * 1.0 + 0.1 * 3.0)
    tracker.async_stop()


async def test_the_tick_on_the_boundary_starts_the_new_rate(hass: HomeAssistant, freezer) -> None:
    """With a settle on every boundary, no span straddles one: what the next
    reading carries flowed entirely after it."""
    tracker = await _started_savings(hass, freezer, BOUNDARY - timedelta(minutes=1))

    freezer.move_to(BOUNDARY)
    async_fire_time_changed(hass, BOUNDARY)
    await hass.async_block_till_done()
    await _read(hass, freezer, BOUNDARY + timedelta(seconds=20), 0.1)

    assert tracker.ledger.actual_cost == pytest.approx(0.1 * 3.0)
    tracker.async_stop()


async def test_energy_from_before_midnight_stays_on_the_day_it_flowed(
    hass: HomeAssistant, freezer
) -> None:
    tracker = await _started_savings(hass, freezer, datetime(2026, 8, 3, 23, 59, 30, tzinfo=UTC))

    # 30 of its 32 seconds were yesterday's; rolled over first, the ledger
    # booked all of it to today.
    await _read(hass, freezer, datetime(2026, 8, 4, 0, 0, 2, tzinfo=UTC), 0.5)

    assert tracker.ledger.day == "2026-08-04"
    assert tracker.ledger.imported_kwh == pytest.approx(0.0)
    tracker.async_stop()

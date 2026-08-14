"""``PeakTracker`` against a live Home Assistant.

The pure aggregation math is worked by hand in ``tests/test_peaks.py``; this
file drives the actual subscriptions, the ``Store``, and wall-clock time
through ``freezer``, following the pattern ``tests/integration/
test_source_health.py`` uses for its own watch. Two things get more attention
than the rest, because both are named explicitly in
``docs/network_tariffs_plan.md`` as the sharp edges to get right: the
incurred-peak floor's "today already holds a top-n entry" case, and a restart
that lands mid-interval.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.emhass_companion.const import DOMAIN
from custom_components.emhass_companion.metering import _RESTORE_MAX_GAP, Meter
from custom_components.emhass_companion.peaks import (
    AGGREGATE_MAX,
    AGGREGATE_MEAN_TOP_N,
    PeakTracker,
)

ENTITY_ID = "sensor.grid_import_energy"


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"})
    entry.add_to_hass(hass)
    return entry


def _set_energy(hass: HomeAssistant, value: float) -> None:
    # force_update=True: several tests re-set the same reading purely to get
    # a state_changed event at a later wall-clock instant (nudging the tracker
    # past a bucket boundary with no new energy in between). Home Assistant
    # suppresses the event by default when the state and attributes are
    # unchanged, which would silently swallow that nudge.
    hass.states.async_set(
        ENTITY_ID,
        str(value),
        {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
        force_update=True,
    )


def _tracker(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    interval: timedelta,
    aggregate: str,
    top_n: int = 1,
    distinct_days: bool = False,
    in_window=lambda _when: True,
) -> PeakTracker:
    meter = Meter(ENTITY_ID, kind="energy")
    return PeakTracker(
        hass,
        entry,
        meter,
        interval=interval,
        aggregate=aggregate,
        top_n=top_n,
        distinct_days=distinct_days,
        in_window=in_window,
    )


async def _spike(hass: HomeAssistant, freezer, total: float, kwh: float) -> float:
    """Add ``kwh`` inside the open bucket, then nudge time past its boundary
    to close it. Must be called with the frozen clock sitting exactly on a
    bucket boundary, and leaves it exactly on the next one -- so calls can be
    chained to build up several distinct qualifying intervals in a row.
    Returns the new running total, since the source is a cumulative counter.
    """
    total += kwh
    freezer.tick(timedelta(minutes=29))
    _set_energy(hass, total)
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=31))
    _set_energy(hass, total)
    await hass.async_block_till_done()
    return total


# -- basic accumulation: max aggregate -------------------------------------------


async def test_max_aggregate_tracks_the_highest_hourly_average(
    hass: HomeAssistant, freezer
) -> None:
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 8, 3, 10, 0, tzinfo=UTC))
    entry = _entry(hass)
    tracker = _tracker(hass, entry, interval=timedelta(hours=1), aggregate=AGGREGATE_MAX)
    tracker.async_start()
    _set_energy(hass, 0.0)
    await hass.async_block_till_done()

    total = await _spike(hass, freezer, 0.0, 1.0)  # 10:00-11:00 -> 1.0 kW
    total = await _spike(hass, freezer, total, 3.0)  # 11:00-12:00 -> 3.0 kW, the new peak
    await _spike(hass, freezer, total, 0.5)  # 12:00-13:00 -> 0.5 kW, well below it

    assert tracker.current_aggregate_kw == pytest.approx(3.0)
    assert tracker.floor_kw == pytest.approx(3.0)  # max: floor is just the aggregate
    tracker.async_stop()


# -- mean_top_n, distinct days: the aggregate and the incurred-peak floor -------


async def test_mean_top_n_distinct_days_and_the_today_floor_case(
    hass: HomeAssistant, freezer
) -> None:
    """Göteborg's shape: mean of the top 3, one per day -- including the
    subtlety the plan calls out explicitly, that a bigger peak on a day
    already in the top 3 replaces its own entry rather than adding a fourth.
    """
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 8, 1, 10, 0, tzinfo=UTC))
    entry = _entry(hass)
    tracker = _tracker(
        hass,
        entry,
        interval=timedelta(hours=1),
        aggregate=AGGREGATE_MEAN_TOP_N,
        top_n=3,
        distinct_days=True,
    )
    tracker.async_start()
    _set_energy(hass, 0.0)
    await hass.async_block_till_done()

    total = await _spike(hass, freezer, 0.0, 7.0)  # day 1: 7 kW
    assert tracker.floor_kw == 0.0  # only 1 of 3 days qualifies yet: nothing to beat

    freezer.move_to(datetime(2026, 8, 2, 10, 0, tzinfo=UTC))
    total = await _spike(hass, freezer, total, 8.0)  # day 2: 8 kW
    assert tracker.floor_kw == 0.0  # still only 2 of 3

    freezer.move_to(datetime(2026, 8, 3, 10, 0, tzinfo=UTC))
    total = await _spike(hass, freezer, total, 9.0)  # day 3: 9 kW, and "today"
    assert tracker.current_aggregate_kw == pytest.approx((7.0 + 8.0 + 9.0) / 3)
    # "Today" (day 3) already holds the *highest* of the top three. The naive
    # answer -- the smallest of the top three, day 1's 7.0 -- would have the
    # plan defend a level already exceeded today, for nothing. The correct
    # floor is today's own 9.0: only a peak bigger than that changes the bill.
    assert tracker.floor_kw == pytest.approx(9.0)
    assert tracker.floor_kw != 7.0

    # A fourth day, too low to enter the top three: the floor falls back to
    # the plain displacement value, because today no longer holds an entry.
    freezer.move_to(datetime(2026, 8, 4, 10, 0, tzinfo=UTC))
    await _spike(hass, freezer, total, 2.0)
    assert tracker.current_aggregate_kw == pytest.approx((7.0 + 8.0 + 9.0) / 3)  # unchanged
    assert tracker.floor_kw == pytest.approx(7.0)

    tracker.async_stop()


async def test_headroom_is_measured_against_the_floor_not_the_raw_aggregate(
    hass: HomeAssistant, freezer
) -> None:
    """Locks in the deliberate deviation from the plan's own shorthand --
    see :meth:`PeakTracker.headroom_kw`'s docstring for why."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 8, 1, 10, 0, tzinfo=UTC))
    entry = _entry(hass)
    tracker = _tracker(
        hass,
        entry,
        interval=timedelta(hours=1),
        aggregate=AGGREGATE_MEAN_TOP_N,
        top_n=3,
        distinct_days=True,
    )
    tracker.async_start()
    _set_energy(hass, 0.0)
    await hass.async_block_till_done()

    total = await _spike(hass, freezer, 0.0, 7.0)
    freezer.move_to(datetime(2026, 8, 2, 10, 0, tzinfo=UTC))
    total = await _spike(hass, freezer, total, 8.0)
    freezer.move_to(datetime(2026, 8, 3, 10, 0, tzinfo=UTC))
    await _spike(hass, freezer, total, 9.0)

    assert tracker.current_aggregate_kw == pytest.approx(8.0)
    assert tracker.floor_kw == pytest.approx(9.0)
    assert tracker.headroom_kw(6.0) == pytest.approx(tracker.floor_kw - 6.0)
    assert tracker.headroom_kw(6.0) != tracker.current_aggregate_kw - 6.0
    tracker.async_stop()


# -- monthly rollover, local ------------------------------------------------------


async def test_monthly_rollover_starts_a_fresh_record(hass: HomeAssistant, freezer) -> None:
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 8, 31, 22, 0, tzinfo=UTC))
    entry = _entry(hass)
    tracker = _tracker(hass, entry, interval=timedelta(hours=1), aggregate=AGGREGATE_MAX)
    tracker.async_start()
    _set_energy(hass, 0.0)
    await hass.async_block_till_done()

    total = await _spike(hass, freezer, 0.0, 5.0)  # August's peak: 5 kW
    assert tracker.period_key == "2026-08"
    assert tracker.current_aggregate_kw == pytest.approx(5.0)

    freezer.move_to(datetime(2026, 9, 1, 10, 0, tzinfo=UTC))
    await _spike(hass, freezer, total, 2.0)  # a much smaller September peak

    assert tracker.period_key == "2026-09"
    # August's 5 kW must not leak into September's own record.
    assert tracker.current_aggregate_kw == pytest.approx(2.0)
    tracker.async_stop()


async def test_the_midnight_timer_rolls_over_with_no_meter_activity(
    hass: HomeAssistant, freezer
) -> None:
    """A quiet house must still roll over -- the settle-on-every-tick check
    alone cannot fire if nothing ticks."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 8, 31, 23, 0, tzinfo=UTC))
    entry = _entry(hass)
    tracker = _tracker(hass, entry, interval=timedelta(hours=1), aggregate=AGGREGATE_MAX)
    tracker.async_start()
    _set_energy(hass, 0.0)
    await hass.async_block_till_done()
    assert tracker.period_key == "2026-08"

    freezer.tick(timedelta(hours=1, seconds=5))  # 2026-09-01 00:00:05
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert tracker.period_key == "2026-09"
    tracker.async_stop()


# -- restart mid-interval: the sharp edge the plan calls out --------------------


async def test_restart_mid_interval_neither_loses_nor_double_counts(
    hass: HomeAssistant, freezer
) -> None:
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 8, 10, 8, 0, tzinfo=UTC))
    entry = _entry(hass)

    tracker_a = _tracker(hass, entry, interval=timedelta(hours=1), aggregate=AGGREGATE_MAX)
    await tracker_a.async_load()
    tracker_a.async_start()
    _set_energy(hass, 0.0)
    await hass.async_block_till_done()

    # Five minutes into the 08:00-09:00 bucket: 0.3 kWh accumulated so far.
    freezer.tick(timedelta(minutes=5))
    _set_energy(hass, 0.3)
    await hass.async_block_till_done()

    # "Home Assistant" goes away here, exactly as async_shutdown would run it.
    await tracker_a.async_shutdown()

    # A restart 10 minutes later -- comfortably inside Meter's own
    # _RESTORE_MAX_GAP, and still inside the same hourly bucket. Nothing
    # changed the source entity while it was "down".
    assert timedelta(minutes=10) < _RESTORE_MAX_GAP
    freezer.tick(timedelta(minutes=10))

    tracker_b = _tracker(hass, entry, interval=timedelta(hours=1), aggregate=AGGREGATE_MAX)
    await tracker_b.async_load()
    tracker_b.async_start()

    # More energy arrives after the restart, still inside the same bucket.
    freezer.tick(timedelta(minutes=5))
    _set_energy(hass, 0.5)  # +0.2 kWh since the last real reading
    await hass.async_block_till_done()

    # Close the bucket by crossing into 09:00.
    freezer.tick(timedelta(minutes=45))
    _set_energy(hass, 0.5)
    await hass.async_block_till_done()

    # 0.3 kWh from before the restart plus 0.2 kWh after it, over the hour.
    # Losing the partial would read 0.2; double-counting it would read 0.8.
    assert tracker_b.current_aggregate_kw == pytest.approx(0.5)
    tracker_b.async_stop()


async def test_restart_after_too_long_a_gap_disowns_the_partial(
    hass: HomeAssistant, freezer
) -> None:
    """Mirrors Meter.restore's own rule, one level up: a gap too long to
    price is a gap too long to trust as a demand reading either, so it is
    dropped rather than invented into a qualifying interval."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 8, 10, 8, 0, tzinfo=UTC))
    entry = _entry(hass)

    tracker_a = _tracker(hass, entry, interval=timedelta(hours=1), aggregate=AGGREGATE_MAX)
    await tracker_a.async_load()
    tracker_a.async_start()
    _set_energy(hass, 0.0)
    await hass.async_block_till_done()

    freezer.tick(timedelta(minutes=5))
    _set_energy(hass, 0.3)
    await hass.async_block_till_done()
    await tracker_a.async_shutdown()

    # Down past _RESTORE_MAX_GAP, and the counter kept moving regardless --
    # 0.9 kWh flowed while nothing was watching it.
    gap = _RESTORE_MAX_GAP + timedelta(minutes=5)
    freezer.tick(gap)
    _set_energy(hass, 1.2)

    tracker_b = _tracker(hass, entry, interval=timedelta(hours=1), aggregate=AGGREGATE_MAX)
    await tracker_b.async_load()
    tracker_b.async_start()

    # Only movement from here on should ever count.
    freezer.tick(timedelta(minutes=5))
    _set_energy(hass, 1.4)  # +0.2 kWh since the restart's fresh baseline
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=50))
    _set_energy(hass, 1.4)
    await hass.async_block_till_done()

    # Neither the 0.3 kWh from before the outage nor the 0.9 kWh that flowed
    # during it were counted -- only the 0.2 kWh measured after the restart.
    assert tracker_b.current_aggregate_kw == pytest.approx(0.2)
    tracker_b.async_stop()


# -- local time, not UTC ----------------------------------------------------------


async def test_interval_buckets_use_local_time_not_utc(hass: HomeAssistant, freezer) -> None:
    """A UTC-vs-local mixup would put the bucket boundary two hours off in
    Stockholm's summer offset, and this spike would land in the wrong hour."""
    await hass.config.async_set_time_zone("Europe/Stockholm")
    # 09:30 UTC = 11:30 local in July (UTC+2).
    freezer.move_to(datetime(2026, 7, 15, 9, 30, tzinfo=UTC))
    entry = _entry(hass)
    tracker = _tracker(hass, entry, interval=timedelta(hours=1), aggregate=AGGREGATE_MAX)
    tracker.async_start()
    _set_energy(hass, 0.0)
    await hass.async_block_till_done()

    freezer.tick(timedelta(minutes=10))  # 09:40 UTC = 11:40 local
    _set_energy(hass, 1.0)
    await hass.async_block_till_done()

    # Cross local noon (10:00 UTC = 12:00 local). A UTC-bucketed tracker would
    # still be inside its "09:00 UTC" hour here and would not close anything.
    freezer.tick(timedelta(minutes=20))
    _set_energy(hass, 1.0)
    await hass.async_block_till_done()

    assert tracker.current_aggregate_kw == pytest.approx(1.0)
    (only_interval,) = tracker.contributing_intervals
    assert dt_util.as_local(only_interval.start).hour == 11
    tracker.async_stop()


# -- the demand window --------------------------------------------------------------


async def test_intervals_outside_the_window_are_not_recorded(hass: HomeAssistant, freezer) -> None:
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 8, 5, 3, 0, tzinfo=UTC))  # 03:00, outside 07:00-20:00

    def _business_hours(when: datetime) -> bool:
        return 7 <= dt_util.as_local(when).hour < 20

    entry = _entry(hass)
    tracker = _tracker(
        hass, entry, interval=timedelta(hours=1), aggregate=AGGREGATE_MAX, in_window=_business_hours
    )
    tracker.async_start()
    _set_energy(hass, 0.0)
    await hass.async_block_till_done()

    # A large overnight spike, entirely outside the window.
    await _spike(hass, freezer, 0.0, 9.0)

    assert tracker.current_aggregate_kw == 0.0
    assert tracker.contributing_intervals == ()
    tracker.async_stop()

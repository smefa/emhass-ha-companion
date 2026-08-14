"""Tests for the demand-charge peak aggregation arithmetic.

The Home Assistant plumbing (subscriptions, the Store, restart handling) is
covered in ``tests/integration/test_peak_tracker.py``, which needs a running
``hass``. Everything provable by hand -- the top-n/distinct-days aggregate and
the incurred-peak floor -- is worked here against intervals built by hand, the
same discipline ``tests/test_savings.py`` applies to the cost ledger: an
assertion that merely agreed with whatever the code currently produces would
notice nothing if the floor's "today already holds an entry" case broke.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.emhass_companion.peaks import (
    AGGREGATE_MAX,
    AGGREGATE_MEAN_TOP_N,
    Interval,
    _bucket_start,
    _local_day,
    _local_period,
    _per_day_bests,
    _top_n_intervals,
    aggregate_kw,
    incurred_floor_kw,
)

DAY1 = "2026-08-01"
DAY2 = "2026-08-02"
DAY3 = "2026-08-03"
DAY4 = "2026-08-04"


def _interval(day: str, kw: float, hour: int = 8) -> Interval:
    year, month, dom = (int(part) for part in day.split("-"))
    return Interval(start=datetime(year, month, dom, hour, tzinfo=UTC), local_day=day, kw=kw)


# -- max aggregate --------------------------------------------------------------


def test_max_aggregate_is_the_highest_qualifying_interval():
    intervals = [_interval(DAY1, 3.0), _interval(DAY2, 7.5), _interval(DAY3, 5.0)]
    assert aggregate_kw(intervals, mode=AGGREGATE_MAX) == 7.5


def test_max_aggregate_of_nothing_is_zero():
    assert aggregate_kw([], mode=AGGREGATE_MAX) == 0.0


def test_max_floor_equals_the_aggregate():
    intervals = [_interval(DAY1, 3.0), _interval(DAY2, 7.5)]
    assert incurred_floor_kw(intervals, mode=AGGREGATE_MAX, today=DAY2) == 7.5


# -- mean_top_n, distinct days (Göteborg's shape) --------------------------------


def test_mean_top_n_takes_the_best_of_each_day_first():
    """Two peaks on the same day must not both count -- only the day's best
    survives the dedup, exactly what "on three distinct days" requires."""
    intervals = [
        _interval(DAY1, 9.0, hour=8),
        _interval(DAY1, 4.0, hour=14),  # same day, lower -- must not also count
        _interval(DAY2, 8.0),
        _interval(DAY3, 7.0),
    ]
    # Top 3 distinct days: 9 (day1), 8 (day2), 7 (day3) -> mean 8.0
    assert aggregate_kw(intervals, mode=AGGREGATE_MEAN_TOP_N, n=3, distinct_days=True) == 8.0


def test_mean_top_n_ignores_days_outside_the_top_n():
    intervals = [
        _interval(DAY1, 9.0),
        _interval(DAY2, 8.0),
        _interval(DAY3, 7.0),
        _interval(DAY4, 1.0),
    ]
    assert aggregate_kw(intervals, mode=AGGREGATE_MEAN_TOP_N, n=3, distinct_days=True) == 8.0


def test_fewer_than_n_days_still_averages_over_what_exists():
    intervals = [_interval(DAY1, 9.0), _interval(DAY2, 6.0)]
    assert aggregate_kw(intervals, mode=AGGREGATE_MEAN_TOP_N, n=3, distinct_days=True) == 7.5


def test_empty_record_aggregates_to_zero():
    assert aggregate_kw([], mode=AGGREGATE_MEAN_TOP_N, n=3, distinct_days=True) == 0.0


# -- the incurred-peak floor: the single most important case --------------------


def test_floor_is_zero_before_n_distinct_days_qualify():
    """Only two days have qualified for a top-3 record: any peak at all would
    enter the top three, so nothing yet needs beating."""
    intervals = [_interval(DAY1, 9.0), _interval(DAY2, 6.0)]
    assert (
        incurred_floor_kw(intervals, mode=AGGREGATE_MEAN_TOP_N, n=3, distinct_days=True, today=DAY2)
        == 0.0
    )


def test_floor_is_the_smallest_of_the_top_n_when_today_is_not_in_it():
    """Today (day4) has no qualifying interval yet, or one too low to be in
    the top 3. A new peak today has to beat the *current* smallest of the top
    three -- day3's 7.0 -- to move the bill at all."""
    intervals = [_interval(DAY1, 9.0), _interval(DAY2, 8.0), _interval(DAY3, 7.0)]
    floor = incurred_floor_kw(
        intervals, mode=AGGREGATE_MEAN_TOP_N, n=3, distinct_days=True, today=DAY4
    )
    assert floor == 7.0


def test_floor_when_today_already_holds_the_smallest_top_n_entry():
    """Today (day3) is already the smallest of the top three. Beating its own
    7.0 kW is what it takes to move the bill -- which is exactly the
    "smallest of the top n" figure here, so this case alone would look
    identical whether or not the today-adjustment exists. The next test is
    the one that actually distinguishes the two."""
    intervals = [_interval(DAY1, 9.0), _interval(DAY2, 8.0), _interval(DAY3, 7.0)]
    floor = incurred_floor_kw(
        intervals, mode=AGGREGATE_MEAN_TOP_N, n=3, distinct_days=True, today=DAY3
    )
    assert floor == 7.0


def test_floor_when_today_already_dominates_the_top_n():
    """The subtlety the plan calls out explicitly: today (day1) already holds
    the *highest* entry in the top three, at 9.0 kW. A second, bigger peak
    today does not add a fourth entry to a full top-3 list -- it can only
    replace today's own 9.0. So the floor is 9.0, not the displacement value
    (day3's 7.0) that a *different* day would have to clear.

    Sending only 7.0 here would have the plan fight to hold today under a
    level it has already exceeded, for nothing -- the exact failure the plan
    names as the one to get right.
    """
    intervals = [_interval(DAY1, 9.0), _interval(DAY2, 8.0), _interval(DAY3, 7.0)]
    floor = incurred_floor_kw(
        intervals, mode=AGGREGATE_MEAN_TOP_N, n=3, distinct_days=True, today=DAY1
    )
    assert floor == 9.0
    # And the naive answer would have been wrong in the direction that costs
    # money: it would keep defending a level already beaten.
    assert floor != 7.0


def test_floor_without_distinct_days_never_needs_the_today_adjustment():
    """Without day-dedup, a new interval today is only ever an *additional*
    candidate competing on equal footing -- it cannot displace one of today's
    own earlier entries the way the day-deduped case can, because nothing
    reduces a day to a single representative. So the floor is always simply
    the smallest of the current top n, even when today already contributes
    to it."""
    intervals = [
        _interval(DAY1, 9.0, hour=8),
        _interval(DAY1, 6.0, hour=14),
        _interval(DAY2, 5.0),
    ]
    floor = incurred_floor_kw(
        intervals, mode=AGGREGATE_MEAN_TOP_N, n=3, distinct_days=False, today=DAY1
    )
    assert floor == 5.0


# -- helpers used by the tracker --------------------------------------------------


def test_per_day_bests_keeps_only_the_highest_per_day():
    intervals = [_interval(DAY1, 3.0, hour=8), _interval(DAY1, 9.0, hour=14), _interval(DAY2, 4.0)]
    best = _per_day_bests(intervals)
    assert best[DAY1].kw == 9.0
    assert best[DAY2].kw == 4.0


def test_top_n_intervals_sorted_highest_first_and_truncated():
    intervals = [
        _interval(DAY1, 3.0),
        _interval(DAY2, 9.0),
        _interval(DAY3, 6.0),
        _interval(DAY4, 1.0),
    ]
    top = _top_n_intervals(intervals, n=2, distinct_days=False)
    assert [i.kw for i in top] == [9.0, 6.0]


def test_interval_round_trips_through_dict():
    original = _interval(DAY1, 4.25, hour=9)
    restored = Interval.from_dict(original.as_dict())
    assert restored == original


def test_interval_from_dict_tolerates_garbage():
    assert Interval.from_dict(None) is None
    assert Interval.from_dict({}) is None
    assert Interval.from_dict({"start": "not a datetime", "local_day": DAY1, "kw": 1.0}) is None
    assert (
        Interval.from_dict({"start": _interval(DAY1, 1.0).start.isoformat(), "local_day": DAY1})
        is None
    )


def test_bucket_start_floors_onto_the_hour_in_local_time():
    when = datetime(2026, 8, 4, 13, 47, tzinfo=UTC)
    bucket = _bucket_start(when, timedelta(minutes=60))
    assert bucket == datetime(2026, 8, 4, 13, 0, tzinfo=UTC)


def test_bucket_start_floors_onto_a_half_hour():
    when = datetime(2026, 8, 4, 13, 47, tzinfo=UTC)
    bucket = _bucket_start(when, timedelta(minutes=30))
    assert bucket == datetime(2026, 8, 4, 13, 30, tzinfo=UTC)


def test_bucket_start_exactly_on_the_boundary_is_unchanged():
    when = datetime(2026, 8, 4, 14, 0, tzinfo=UTC)
    bucket = _bucket_start(when, timedelta(minutes=60))
    assert bucket == when


def test_local_day_and_period_are_utc_here_with_no_configured_timezone():
    """Sanity check on the module's own helpers, not on Home Assistant's
    timezone handling -- that dependency, and non-UTC offsets shifting a
    demand window, is covered against a real ``hass`` in the integration
    suite alongside the tracker itself."""
    when = datetime(2026, 8, 4, 13, 0, tzinfo=UTC)
    assert _local_day(when) == "2026-08-04"
    assert _local_period(when) == "2026-08"

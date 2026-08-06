"""Tests for TimeWeightedAverage, pure logic with no hass fixture needed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.emhass_companion.smoothing import TimeWeightedAverage

T0 = datetime(2026, 1, 1, tzinfo=UTC)
WINDOW = timedelta(minutes=15)


def test_a_single_sample_with_no_elapsed_time_is_still_its_own_average() -> None:
    """total_weight is 0 right at the sample's own timestamp; the fallback
    to the latest value is what keeps this from reading as None."""
    average = TimeWeightedAverage()
    average.record(T0, 1000.0, WINDOW)
    assert average.average(T0, WINDOW) == 1000.0


def test_no_samples_yet_averages_to_none() -> None:
    average = TimeWeightedAverage()
    assert average.average(T0, WINDOW) is None


def test_weights_each_value_by_how_long_it_held() -> None:
    """1000 W for 10 minutes then 2000 W for 5 minutes, over a 15-minute window."""
    average = TimeWeightedAverage()
    average.record(T0, 1000.0, WINDOW)
    average.record(T0 + timedelta(minutes=10), 2000.0, WINDOW)
    now = T0 + timedelta(minutes=15)
    assert average.average(now, WINDOW) == (1000.0 * 10 + 2000.0 * 5) / 15


def test_right_after_a_change_the_average_is_still_dominated_by_the_old_value() -> None:
    """The point of averaging: a step change doesn't show up as a step in the
    reported value, it phases in over the window."""
    average = TimeWeightedAverage()
    average.record(T0, 1000.0, WINDOW)
    average.record(T0 + timedelta(minutes=14, seconds=59), 2000.0, WINDOW)
    now = T0 + WINDOW  # one second after the change was recorded
    assert average.average(now, WINDOW) == 1000.0 + (2000.0 - 1000.0) * (1 / 900)


def test_a_value_held_since_before_the_window_counts_for_its_whole_span() -> None:
    average = TimeWeightedAverage()
    average.record(T0, 1000.0, WINDOW)
    now = T0 + timedelta(minutes=20)
    assert average.average(now, WINDOW) == 1000.0


def test_a_missing_reading_clears_history_rather_than_freezing_the_last_value() -> None:
    """A source going offline is a gap, not "unchanged" -- letting the last
    reading's segment silently extend through it would bias the average once
    data resumes, by exactly however long the outage lasted.
    """
    average = TimeWeightedAverage()
    average.record(T0, 1000.0, WINDOW)
    average.record(T0 + timedelta(minutes=1), None, WINDOW)
    assert average.average(T0 + timedelta(minutes=1), WINDOW) is None

    # Once a real reading resumes, averaging starts fresh from there.
    average.record(T0 + timedelta(minutes=2), 3000.0, WINDOW)
    assert average.average(T0 + timedelta(minutes=2), WINDOW) == 3000.0


def test_history_far_outside_the_window_is_pruned_down_to_one_carry_sample() -> None:
    """Old entries are dropped once a later one has already superseded them,
    keeping memory bounded over a long uptime -- except the single most
    recent entry still before window_start, kept as the average's starting
    edge for whatever span of the window it still covers.
    """
    average = TimeWeightedAverage()
    average.record(T0, 1000.0, WINDOW)
    average.record(T0 + timedelta(minutes=1), 1100.0, WINDOW)
    average.record(T0 + timedelta(minutes=2), 1200.0, WINDOW)
    far_future = T0 + timedelta(hours=2)
    average.record(far_future, 3000.0, WINDOW)

    assert [value for _, value in average._history] == [1200.0, 3000.0]

    # Pruning is purely a memory optimisation -- the math it left behind
    # matches what an unpruned history would have computed here too.
    now = far_future + timedelta(minutes=10)
    assert average.average(now, WINDOW) == (1200.0 * 5 + 3000.0 * 10) / 15


def test_a_signal_that_has_not_moved_averages_to_exactly_its_value() -> None:
    """No float drift from the weighting itself.

    Segments are wall-clock gaps, so the weight can be any float at all. The
    naive weighted_sum / total_weight form evaluates (value * weight) /
    weight, which for a lone segment is not exactly `value` at every weight --
    3000 W over ~12 microseconds came back as 3000.0000000000005. Whether a
    given run hit a lossy weight was pure timing, so this showed up as an
    intermittent failure in the sensor tests that read the average back.
    """
    for micros in (1, 12, 37, 500, 4_999, 123_456):
        average = TimeWeightedAverage()
        average.record(T0, 3000.0, WINDOW)
        now = T0 + timedelta(microseconds=micros)
        assert average.average(now, WINDOW) == 3000.0, f"{micros} us"

    # And with several segments all sitting at the same value.
    average = TimeWeightedAverage()
    for step, micros in enumerate((0, 12, 37, 500, 4_999)):
        average.record(T0 + timedelta(microseconds=micros), 3000.0, WINDOW)
        assert average.average(T0 + timedelta(microseconds=micros + 7), WINDOW) == 3000.0, (
            f"sample {step}"
        )

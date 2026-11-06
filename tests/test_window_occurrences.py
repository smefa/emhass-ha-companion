"""Recurrences of a daily load window inside the forecast horizon."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from homeassistant.util import dt as dt_util
import pytest

from custom_components.emhass_companion.payload import window_occurrences, window_to_timesteps

HALF_HOUR = timedelta(minutes=30)

pytestmark = pytest.mark.usefixtures("stockholm_timezone")


def _local(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=dt_util.DEFAULT_TIME_ZONE)


def test_second_day_window_fits_a_48_hour_horizon():
    now = _local(2026, 7, 28, 12)
    assert window_occurrences(time(14), time(18), now, HALF_HOUR, 96) == [(4, 12), (52, 60)]


def test_first_occurrence_matches_the_single_window_result():
    now = _local(2026, 7, 28, 12)
    first = window_occurrences(time(14), time(18), now, HALF_HOUR, 96)[0]
    assert first == window_to_timesteps(time(14), time(18), now, HALF_HOUR, 96)


def test_24_hour_horizon_has_one_occurrence():
    now = _local(2026, 7, 28, 12)
    assert window_occurrences(time(14), time(18), now, HALF_HOUR, 48) == [(4, 12)]


def test_partly_covered_window_is_left_out():
    now = _local(2026, 7, 28, 12)
    # The second window ends at step 60, so 58 steps only reach part of it.
    assert window_occurrences(time(14), time(18), now, HALF_HOUR, 58) == [(4, 12)]
    assert window_occurrences(time(14), time(18), now, HALF_HOUR, 60) == [(4, 12), (52, 60)]


def test_window_ending_on_the_horizon_edge_is_kept():
    now = _local(2026, 7, 28, 18)
    assert window_occurrences(time(14), time(18), now, HALF_HOUR, 96) == [(40, 48), (88, 96)]


def test_already_open_overnight_window_gives_a_full_second_night():
    now = _local(2026, 7, 28, 23)
    # Night 1 is clipped to the 7 h left; night 2 is the full 8 h.
    assert window_occurrences(time(22), time(6), now, HALF_HOUR, 96) == [(0, 14), (46, 62)]


@pytest.mark.parametrize(
    ("earliest", "latest"),
    [(time(14), time(14)), (time(14), None), (None, time(18)), (None, None)],
)
def test_empty_or_open_ended_window_is_a_single_occurrence(earliest, latest):
    now = _local(2026, 7, 28, 12)
    assert window_occurrences(earliest, latest, now, HALF_HOUR, 96) == [
        window_to_timesteps(earliest, latest, now, HALF_HOUR, 96)
    ]


def test_window_opening_beyond_the_horizon_adds_nothing():
    now = _local(2026, 7, 28, 12)
    assert window_occurrences(time(18), time(20), now, HALF_HOUR, 4) == [(0, 0)]


def test_minimum_off_time_longer_than_the_gap_keeps_one_row():
    now = _local(2026, 7, 28, 12)
    args = (time(0), time(23, 45), now, HALF_HOUR, 96)
    assert len(window_occurrences(*args)) == 2
    assert len(window_occurrences(*args, minimum_off=timedelta(minutes=10))) == 2
    assert len(window_occurrences(*args, minimum_off=timedelta(minutes=60))) == 1


def test_minimum_off_time_is_also_checked_on_the_step_grid():
    # 00:05-00:00 leaves five wall-clock minutes, but on half-hour steps the
    # second window opens at the very index the first one closed.
    now = _local(2026, 7, 28, 12)
    args = (time(0, 5), time(0), now, HALF_HOUR, 96)
    assert len(window_occurrences(*args, minimum_off=timedelta(minutes=5))) == 1


def test_spring_forward_day_fits_a_second_window_in_24_hours():
    # 2026-03-29 is 23 h long in Stockholm, so 03:00-04:00 recurs inside 24 h.
    now = _local(2026, 3, 28, 3, 30)
    assert window_occurrences(time(3), time(4), now, HALF_HOUR, 48) == [(0, 1), (45, 47)]


def test_a_normal_day_does_not_fit_a_second_window_in_24_hours():
    now = _local(2026, 7, 28, 3, 30)
    assert window_occurrences(time(3), time(4), now, HALF_HOUR, 48) == [(0, 1)]


def test_window_inside_the_spring_forward_gap_moves_to_the_first_valid_time():
    # 02:30 does not exist on 2026-03-29; the window opens at 03:00 instead.
    now = _local(2026, 3, 27, 12)
    assert window_occurrences(time(2, 30), time(3, 30), now, HALF_HOUR, 96) == [
        (29, 31),
        (76, 77),
    ]


def test_fall_back_night_counts_the_extra_hour():
    # The clocks go back at 03:00 on 2026-10-25, so the first night is 9 real
    # hours (18 steps) and the next, fully after the change, is 8 (16 steps).
    now = _local(2026, 10, 24, 12)
    assert window_occurrences(time(22), time(6), now, HALF_HOUR, 96) == [(20, 38), (70, 86)]

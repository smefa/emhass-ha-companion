"""Today's planned SOC low/high is the more extreme of past and future.

The forecast a run carries only looks forward, so a peak that has already
been reached drops out of it. Keeping every forecast ever issued does the
opposite harm: a peak the plan later revised away stays on the card. The
published figure is whichever of the two is more extreme, for today only.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from homeassistant.core import State
import pytest

from custom_components.emhass_companion.coordinator import (
    DayRange,
    latch_past_soc,
    publish_soc_day_range,
    recorded_soc_points,
)
from custom_components.emhass_companion.models import Point, Series

pytestmark = pytest.mark.usefixtures("stockholm_timezone")

TODAY = date(2026, 9, 27)


def _at(hour: int, minute: int = 0, *, day: int = 27) -> datetime:
    """A UTC instant on 27 Sep 2026 (CEST, UTC+2) unless ``day`` says otherwise."""
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def _series(*pairs: tuple[datetime, float]) -> Series:
    return Series(Point(when, value) for when, value in pairs)


def _range() -> DayRange:
    return DayRange(day=TODAY)


def test_a_peak_already_reached_beats_a_lower_remainder() -> None:
    """17:00 local hit 85%. By 19:50 the series only has the decline from 68%."""
    day = _range()
    publish_soc_day_range(
        day,
        _series((_at(15, 0), 84.95), (_at(16, 0), 81.1)),
        _at(15, 2),
    )
    publish_soc_day_range(
        day,
        _series((_at(17, 45), 68.1), (_at(19, 45), 52.9), (_at(11, day=28), 95.0)),
        _at(17, 50),
    )

    assert day.high is not None
    assert day.high.value == 84.95
    assert day.high.time == _at(15, 0)
    # Tomorrow's 95% is a different day. Tonight's 52.9% is still today.
    assert day.low is not None
    assert day.low.value == 52.9


def test_a_revised_future_peak_does_not_stick() -> None:
    """A 100% the plan no longer expects must not outrank the peak it does."""
    day = _range()
    publish_soc_day_range(
        day,
        _series((_at(8, 0), 40.0), (_at(12, 0), 100.0)),
        _at(8, 5),
    )
    assert day.high is not None and day.high.value == 100.0
    assert day.past_high is not None and day.past_high.value == 40.0

    publish_soc_day_range(
        day,
        _series((_at(10, 0), 55.0), (_at(12, 0), 80.0)),
        _at(10, 5),
    )

    assert day.high is not None
    assert day.high.value == 80.0
    assert day.past_high is not None and day.past_high.value == 55.0


def test_a_future_trough_drops_out_when_the_plan_revises_it() -> None:
    day = _range()
    publish_soc_day_range(day, _series((_at(6, 0), 40.0)), _at(6, 5))
    publish_soc_day_range(
        day,
        _series((_at(8, 0), 50.0), (_at(19, 0), 10.0)),
        _at(8, 5),
    )
    assert day.low is not None and day.low.value == 10.0
    assert day.past_low is not None and day.past_low.value == 40.0

    publish_soc_day_range(
        day,
        _series((_at(9, 0), 48.0), (_at(19, 0), 60.0)),
        _at(9, 5),
    )

    assert day.low is not None and day.low.value == 40.0


def test_a_higher_future_still_wins_over_the_past() -> None:
    day = _range()
    publish_soc_day_range(day, _series((_at(8, 0), 70.0)), _at(8, 5))
    publish_soc_day_range(
        day,
        _series((_at(12, 0), 60.0), (_at(16, 0), 90.0)),
        _at(12, 5),
    )

    assert day.high is not None and day.high.value == 90.0
    assert day.past_high is not None and day.past_high.value == 70.0


def test_recorded_history_restores_a_peak_the_series_has_dropped() -> None:
    """After a restart the series starts at 'now'. History still has the peak."""
    now = _at(17, 50)
    states = [
        State("sensor.planned", "unknown", last_changed=_at(0, 15)),
        State("sensor.planned", "18.1", last_changed=_at(4, 30)),
        State("sensor.planned", "84.95", last_changed=_at(15, 0)),
        State("sensor.planned", "68.1", last_changed=_at(17, 45)),
    ]
    day = _range()
    latch_past_soc(day, recorded_soc_points(states, now))
    publish_soc_day_range(day, _series((_at(17, 45), 68.1), (_at(19, 45), 52.9)), now)

    assert day.high is not None and day.high.value == 84.95
    assert day.low is not None and day.low.value == 18.1


def test_a_level_held_since_yesterday_counts_at_midnight() -> None:
    now = _at(1, 0)
    states = [State("sensor.planned", "33.0", last_changed=_at(20, 0, day=26))]
    points = recorded_soc_points(states, now)

    assert len(points) == 1
    assert points[0].value == 33.0
    # Local midnight is 22:00 UTC the previous calendar day.
    assert points[0].time == _at(22, 0, day=26)

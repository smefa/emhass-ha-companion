"""Tests for the PV auto-caution controller.

Times are local and hourly, peak planned PV is 5 kW, so the 10 % sun floor is
500 W and sunrise is 06:00 unless a test says otherwise.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from custom_components.emhass_companion.pv_caution import (
    CautionConfig,
    CautionState,
    Sample,
    advance,
    blend,
    window_ratio,
)

HOUR = timedelta(hours=1)
PEAK = 5000.0
SUNRISE = datetime(2026, 6, 1, 6)
CONFIG = CautionConfig()


def _hours(first: int, pairs: list[tuple[float, float]], *, curtailed=()) -> list[Sample]:
    return [
        Sample(
            start=datetime(2026, 6, 1, first + i),
            duration=HOUR,
            planned_w=planned,
            actual_w=actual,
            curtailed=(first + i) in curtailed,
        )
        for i, (planned, actual) in enumerate(pairs)
    ]


def _step(state: CautionState, samples: list[Sample], hour: int) -> CautionState:
    return advance(
        state, samples, now=datetime(2026, 6, 1, hour), peak_w=PEAK, sunrise=SUNRISE, config=CONFIG
    )


def test_behind_plan_steps_up() -> None:
    samples = _hours(9, [(3000, 2000), (3000, 2000), (3000, 2000)])
    state = _step(CautionState(), samples, 12)
    assert state.ratio == pytest.approx(2 / 3)
    assert state.bias == pytest.approx(0.1)


def test_on_plan_holds() -> None:
    samples = _hours(9, [(3000, 2800), (3000, 2800), (3000, 2800)])
    state = _step(CautionState(day=date(2026, 6, 1), bias=0.3), samples, 12)
    assert state.bias == pytest.approx(0.3)


def test_ahead_of_plan_steps_down_but_not_below_zero() -> None:
    samples = _hours(9, [(3000, 3300), (3000, 3300), (3000, 3300)])
    state = _step(CautionState(day=date(2026, 6, 1), bias=0.3), samples, 12)
    assert state.bias == pytest.approx(0.2)
    assert _step(CautionState(day=date(2026, 6, 1)), samples, 12).bias == 0.0


def test_bias_is_clamped_at_one() -> None:
    samples = _hours(9, [(3000, 500), (3000, 500), (3000, 500)])
    state = _step(CautionState(day=date(2026, 6, 1), bias=0.95), samples, 12)
    assert state.bias == 1.0


def test_night_and_weak_sun_are_ignored() -> None:
    # Planned below 10 % of peak: dawn/dusk shoulders and night, whatever actual did.
    samples = _hours(9, [(0, 0), (400, 0), (450, 0)])
    state = _step(CautionState(), samples, 12)
    assert state.ratio is None
    assert state.bias == 0.0


def test_first_hour_after_sunrise_is_ignored() -> None:
    samples = _hours(6, [(3000, 0), (3000, 3000), (3000, 3000)])
    ratio, coverage = window_ratio(
        samples, now=datetime(2026, 6, 1, 9), peak_w=PEAK, sunrise=SUNRISE, config=CONFIG
    )
    assert ratio == pytest.approx(1.0)
    assert coverage == 2 * HOUR


def test_curtailed_intervals_are_not_a_forecast_miss() -> None:
    samples = _hours(9, [(3000, 1000), (3000, 1000), (3000, 3000)], curtailed={9, 10})
    state = _step(CautionState(), samples, 12)
    assert state.ratio == pytest.approx(1.0)
    assert state.bias == 0.0


def test_too_little_coverage_holds() -> None:
    config = CautionConfig(min_coverage=2 * HOUR)
    samples = _hours(11, [(3000, 500)])
    state = advance(
        CautionState(day=date(2026, 6, 1), bias=0.2),
        samples,
        now=datetime(2026, 6, 1, 12),
        peak_w=PEAK,
        sunrise=SUNRISE,
        config=config,
    )
    assert state.ratio is None
    assert state.bias == pytest.approx(0.2)


def test_only_the_window_counts() -> None:
    # 08:00 was dreadful but sits outside a 3 h window ending 12:00.
    samples = _hours(8, [(3000, 0), (3000, 3000), (3000, 3000), (3000, 3000)])
    state = _step(CautionState(), samples, 12)
    assert state.ratio == pytest.approx(1.0)


def test_incomplete_interval_is_not_counted() -> None:
    # The interval 11:00-12:00 has not finished at 11:30.
    samples = _hours(9, [(3000, 3000), (3000, 3000), (3000, 0)])
    ratio, _ = window_ratio(
        samples,
        now=datetime(2026, 6, 1, 11, 30),
        peak_w=PEAK,
        sunrise=SUNRISE,
        config=CONFIG,
    )
    assert ratio == pytest.approx(1.0)


def test_new_day_resets_to_zero() -> None:
    yesterday = CautionState(day=date(2026, 5, 31), bias=0.8)
    # No qualifying sun yet this morning: the reset alone decides.
    state = _step(yesterday, [], 5)
    assert state.day == date(2026, 6, 1)
    assert state.bias == 0.0


def test_blend_matches_emhass() -> None:
    assert blend(4000, 2000, 0.0) == 4000
    assert blend(4000, 2000, 1.0) == 2000
    assert blend(4000, 2000, 0.25) == pytest.approx(3500)
    assert blend(4000, 2000, 1.5) == 2000


def test_frequent_runs_step_at_most_once_per_interval() -> None:
    samples = _hours(9, [(3000, 2000), (3000, 2000), (3000, 2000)])
    state = CautionState()
    for minute in range(0, 60, 5):
        state = advance(
            state,
            samples,
            now=datetime(2026, 6, 1, 12, minute),
            peak_w=PEAK,
            sunrise=SUNRISE,
            config=CONFIG,
        )
    assert state.bias == pytest.approx(0.1)
    state = _step(state, samples, 13)
    assert state.bias == pytest.approx(0.2)

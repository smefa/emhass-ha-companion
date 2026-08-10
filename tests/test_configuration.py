"""Tests for EmhassConfig's derived, EMHASS-facing values.

``dayahead_num_lags`` in particular has to match EMHASS's own source, not just
look plausible: a non-tuned mlforecaster model can only ever produce
``num_lags`` predictions in one call (``machine_learning_forecaster.py``), and
a day-ahead run needs as many predictions as EMHASS's own ``forecast.py``
builds into ``forecast_dates`` -- which is rounded up to a whole number of
days, not simply the configured horizon in steps.
"""

from __future__ import annotations

import pytest

from custom_components.emhass_companion.configuration import EmhassConfig


@pytest.mark.parametrize(
    ("time_step_minutes", "horizon_hours", "expected"),
    [
        # The exact case this bug class was found and fixed against: 15-minute
        # steps, a 24-hour day-ahead horizon -> 96 steps/day, one day.
        (15, 24, 96),
        # EMHASS's own default time step, same 24h horizon -> 48 steps/day.
        (30, 24, 48),
        # Hourly steps, 24h horizon -> 24 steps/day.
        (60, 24, 24),
        # A horizon that is *not* a multiple of 24h must round up to a whole
        # extra day of steps, not truncate -- this is the case where
        # `horizon_steps` alone would under-provision `num_lags`.
        (15, 30, 192),  # ceil(30/24) = 2 days * 96 steps/day
        (60, 25, 48),  # ceil(25/24) = 2 days * 24 steps/day
        # A horizon shorter than a day still needs one full day of lags.
        (15, 6, 96),
    ],
)
def test_dayahead_num_lags_matches_emhass_forecast_dates_sizing(
    time_step_minutes, horizon_hours, expected
):
    config = EmhassConfig(
        url="http://x", time_step_minutes=time_step_minutes, horizon_hours=horizon_hours
    )
    assert config.dayahead_num_lags == expected


def test_dayahead_num_lags_uses_defaults_when_unset():
    config = EmhassConfig(url="http://x")
    # DEFAULT_TIME_STEP=15, DEFAULT_HORIZON_HOURS=24 -> 96 steps/day, one day.
    assert config.dayahead_num_lags == 96

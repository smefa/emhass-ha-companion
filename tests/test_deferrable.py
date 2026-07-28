"""Tests for deferrable load live state.

The runtime accumulator and the mode override are the two places where a
mistake produces a load that quietly never runs, or one that runs twice as
long as it should.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from custom_components.emhass_companion.const import (
    LOAD_MODE_AUTO,
    LOAD_MODE_FORCE_OFF,
    LOAD_MODE_FORCE_ON,
)
from custom_components.emhass_companion.deferrable import (
    DeferrableRuntime,
    resolve_should_run,
)

T0 = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)


def _load(**overrides) -> DeferrableRuntime:
    defaults = {
        "subentry_id": "abc",
        "name": "Dishwasher",
        "nominal_power_w": 2000.0,
        "operating_hours": 2.0,
    }
    return DeferrableRuntime(**{**defaults, **overrides})


# --- running detection -------------------------------------------------------


def test_running_threshold_is_a_fraction_of_nominal():
    assert _load(nominal_power_w=2000).running_threshold_w == 200


def test_running_threshold_has_a_floor():
    """A tiny nominal power must not make standby draw look like operation."""
    assert _load(nominal_power_w=50).running_threshold_w == 10


def test_power_above_threshold_starts_the_clock():
    load = _load()
    load.observe_power(1900, T0)
    assert load.is_running


def test_power_below_threshold_does_not():
    load = _load()
    load.observe_power(5, T0)
    assert not load.is_running


# --- runtime accumulation ----------------------------------------------------


def test_runtime_accumulates_across_runs():
    load = _load()
    load.observe_power(1900, T0)
    load.observe_power(0, T0 + timedelta(minutes=45))
    assert load.elapsed_today(T0 + timedelta(hours=2)) == timedelta(minutes=45)

    load.observe_power(1900, T0 + timedelta(hours=2))
    load.observe_power(0, T0 + timedelta(hours=2, minutes=15))
    assert load.elapsed_today(T0 + timedelta(hours=3)) == timedelta(minutes=60)


def test_elapsed_includes_an_in_progress_run():
    load = _load()
    load.observe_power(1900, T0)
    assert load.elapsed_today(T0 + timedelta(minutes=20)) == timedelta(minutes=20)


def test_repeated_on_readings_do_not_restart_the_clock():
    load = _load()
    load.observe_power(1900, T0)
    load.observe_power(1950, T0 + timedelta(minutes=10))
    load.observe_power(1800, T0 + timedelta(minutes=20))
    assert load.elapsed_today(T0 + timedelta(minutes=30)) == timedelta(minutes=30)


def test_unavailable_sensor_stops_counting():
    """An unbounded open interval would otherwise inflate runtime forever."""
    load = _load()
    load.observe_power(1900, T0)
    load.observe_power(None, T0 + timedelta(minutes=10))
    assert not load.is_running
    assert load.elapsed_today(T0 + timedelta(hours=5)) == timedelta(minutes=10)


def test_midnight_reset_clears_the_counter():
    """EMHASS has no day boundary for completed timesteps; we supply one."""
    load = _load()
    load.observe_power(1900, T0)
    load.observe_power(0, T0 + timedelta(hours=3))
    assert load.elapsed_today(T0 + timedelta(hours=4)) == timedelta(hours=3)

    load.reset_day(T0 + timedelta(hours=14))
    assert load.elapsed_today(T0 + timedelta(hours=15)) == timedelta()


def test_completed_timesteps_floors_to_whole_steps():
    load = _load()
    load.observe_power(1900, T0)
    load.observe_power(0, T0 + timedelta(minutes=75))

    now = T0 + timedelta(hours=2)
    assert load.completed_timesteps(now, 30) == 2
    assert load.completed_timesteps(now, 15) == 5


# --- projection into the payload ---------------------------------------------


def test_time_window_is_omitted_when_the_switch_is_off():
    """The window switch, not a magic time value, decides whether it applies."""
    load = _load(use_time_window=False, earliest_start=time(22, 0), latest_end=time(6, 0))
    projected = load.to_load(T0, 30)
    assert projected.earliest_start is None
    assert projected.latest_end is None


def test_time_window_is_passed_when_the_switch_is_on():
    load = _load(use_time_window=True, earliest_start=time(22, 0), latest_end=time(6, 0))
    projected = load.to_load(T0, 30)
    assert projected.earliest_start == time(22, 0)
    assert projected.latest_end == time(6, 0)


def test_a_running_load_reports_its_state_and_power():
    """Without this EMHASS re-charges a startup penalty for a running load."""
    load = _load()
    load.observe_power(1900, T0)

    projected = load.to_load(T0 + timedelta(minutes=10), 30)
    assert projected.current_state is True
    assert projected.current_power_w == 2000.0


def test_an_idle_load_reports_no_current_power():
    projected = _load().to_load(T0, 30)
    assert projected.current_state is False
    assert projected.current_power_w == 0.0


def test_force_on_tells_emhass_the_load_is_on():
    projected = _load(mode=LOAD_MODE_FORCE_ON).to_load(T0, 30)
    assert projected.current_state is True


def test_completed_timesteps_reach_the_payload():
    load = _load()
    load.observe_power(1900, T0)
    load.observe_power(0, T0 + timedelta(hours=1))
    projected = load.to_load(T0 + timedelta(hours=2), 30)
    assert projected.completed_timesteps == 2


# --- mode override -----------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "scheduled", "expected"),
    [
        (LOAD_MODE_AUTO, True, True),
        (LOAD_MODE_AUTO, False, False),
        (LOAD_MODE_FORCE_ON, False, True),
        (LOAD_MODE_FORCE_ON, True, True),
        (LOAD_MODE_FORCE_OFF, True, False),
        (LOAD_MODE_FORCE_OFF, False, False),
    ],
)
def test_mode_overrides_the_schedule(mode, scheduled, expected):
    assert resolve_should_run(mode, scheduled) is expected


def test_mode_does_not_remove_a_load_from_the_optimisation():
    """Force off changes the signal, not what EMHASS is asked to solve.

    Leaving a load out of planning is the Enabled switch's job. Conflating the
    two would silently change the problem whenever someone paused a load.
    """
    assert _load(mode=LOAD_MODE_FORCE_OFF).to_load(T0, 30).enabled is True

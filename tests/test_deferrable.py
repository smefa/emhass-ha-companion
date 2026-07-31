"""Tests for deferrable load live state.

The runtime accumulator and the mode override are the two places where a
mistake produces a load that quietly never runs, or one that runs twice as
long as it should.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from homeassistant.core import State
import pytest

from custom_components.emhass_companion.const import (
    LOAD_MODE_AUTO,
    LOAD_MODE_FORCE_OFF,
    LOAD_MODE_FORCE_ON,
    RECURRENCE_DAILY,
    RECURRENCE_ON_DEMAND,
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


# --- what tells a load it is running -----------------------------------------


def test_a_power_sensor_is_preferred_as_the_running_source():
    load = _load(power_sensor="sensor.dishwasher_power", control_entity="switch.dishwasher")
    assert load.running_source == "sensor.dishwasher_power"


def test_the_control_entity_is_the_running_source_without_a_meter():
    """EMHASS remembers nothing between runs, so *something* has to tell it the
    load already ran today. A metered load is ideal, but a load the executor
    switches on is a perfectly good signal -- and one the user already gave us."""
    load = _load(control_entity="switch.dishwasher")
    assert load.running_source == "switch.dishwasher"


def test_a_load_with_nothing_to_observe_has_no_running_source():
    assert _load().running_source is None


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("1900", 1900.0),
        ("0", 0.0),
        # An on/off source reads as the full nominal power, clearing
        # running_threshold_w by construction rather than by luck.
        ("on", 2000.0),
        ("off", 0.0),
        ("unavailable", None),
        ("unknown", None),
        ("", None),
        ("not a number", None),
    ],
)
def test_running_source_states_are_read_as_power(state, expected):
    load = _load(nominal_power_w=2000)
    assert load.state_to_power(State("switch.dishwasher", state)) == expected


def test_a_missing_running_source_state_is_no_reading():
    assert _load().state_to_power(None) is None


@pytest.mark.parametrize(
    ("raw", "unit", "expected_w"),
    [
        (1900, "W", 1900.0),
        (1.9, "kW", 1900.0),
        (0.0019, "MW", 1900.0),
        (1900000, "mW", 1900.0),
    ],
)
def test_a_power_sensor_in_another_unit_is_converted_to_watts(raw, unit, expected_w):
    """nominal_power_w and running_threshold_w are both watts -- a sensor
    reporting kW read without conversion would be 1000x too small, and the
    load would look permanently idle no matter how much power it draws."""
    load = _load(nominal_power_w=2000)
    state = State("sensor.dishwasher_power", str(raw), {"unit_of_measurement": unit})
    assert load.state_to_power(state) == pytest.approx(expected_w)


def test_a_power_sensor_with_no_unit_is_read_as_watts():
    load = _load(nominal_power_w=2000)
    state = State("sensor.dishwasher_power", "1900")
    assert load.state_to_power(state) == 1900.0


def test_an_unrecognised_unit_falls_back_to_the_raw_number():
    """A non-power unit on a power_sensor entity is a misconfiguration outside
    this integration's control -- using the raw number beats discarding the
    reading entirely."""
    load = _load(nominal_power_w=2000)
    state = State("sensor.dishwasher_power", "1900", {"unit_of_measurement": "bogus"})
    assert load.state_to_power(state) == 1900.0


def test_an_on_off_source_accumulates_runtime():
    load = _load(control_entity="switch.dishwasher")
    load.observe(State("switch.dishwasher", "on"), T0)
    load.observe(State("switch.dishwasher", "off"), T0 + timedelta(hours=1))

    assert load.elapsed_today(T0 + timedelta(hours=2)) == timedelta(hours=1)
    assert load.completed_timesteps(T0 + timedelta(hours=2), 30) == 2


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


# --- on-demand recurrence -----------------------------------------------------


def test_a_daily_load_participates_regardless_of_requested():
    load = _load(recurrence=RECURRENCE_DAILY)
    assert load.participates is True
    load.requested = True
    assert load.participates is True


def test_an_on_demand_load_only_participates_once_requested():
    load = _load(recurrence=RECURRENCE_ON_DEMAND)
    assert load.participates is False
    load.requested = True
    assert load.participates is True


def test_an_unrequested_on_demand_load_is_excluded_from_the_payload():
    """Same path a disabled load takes -- payload.py filters on `enabled`."""
    load = _load(recurrence=RECURRENCE_ON_DEMAND)
    assert load.to_load(T0, 30).enabled is False


def test_a_requested_on_demand_load_is_included_in_the_payload():
    load = _load(recurrence=RECURRENCE_ON_DEMAND, requested=True)
    assert load.to_load(T0, 30).enabled is True


def test_disabled_wins_over_a_request():
    load = _load(recurrence=RECURRENCE_ON_DEMAND, requested=True, enabled=False)
    assert load.to_load(T0, 30).enabled is False


def test_auto_disarm_is_a_no_op_for_a_daily_load():
    load = _load(recurrence=RECURRENCE_DAILY, requested=True, operating_hours=1)
    load.observe_power(1900, T0)
    load.observe_power(0, T0 + timedelta(hours=1))
    assert load.check_auto_disarm(T0 + timedelta(hours=1)) is False
    assert load.requested is True


def test_auto_disarm_is_a_no_op_when_nothing_is_requested():
    load = _load(recurrence=RECURRENCE_ON_DEMAND, operating_hours=1)
    assert load.check_auto_disarm(T0) is False


def test_auto_disarm_waits_for_the_target_to_be_reached():
    load = _load(recurrence=RECURRENCE_ON_DEMAND, requested=True, operating_hours=1)
    load.observe_power(1900, T0)
    load.observe_power(0, T0 + timedelta(minutes=30))
    assert load.check_auto_disarm(T0 + timedelta(minutes=30)) is False
    assert load.requested is True


def test_auto_disarm_clears_the_request_once_the_target_is_reached():
    load = _load(recurrence=RECURRENCE_ON_DEMAND, requested=True, operating_hours=1)
    load.observe_power(1900, T0)
    load.observe_power(0, T0 + timedelta(hours=1))
    assert load.check_auto_disarm(T0 + timedelta(hours=1)) is True
    assert load.requested is False

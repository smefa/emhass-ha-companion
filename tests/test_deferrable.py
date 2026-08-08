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
    LOAD_MODE_FORCE_ON,
    LOAD_TYPE_THERMAL,
    RECURRENCE_DAILY,
    RECURRENCE_ON_DEMAND,
    RECURRENCE_SURPLUS,
)
from custom_components.emhass_companion.deferrable import (
    DeferrableRuntime,
    resolve_should_run,
)
from custom_components.emhass_companion.surplus import SurplusBudget

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


def test_completed_timesteps_never_goes_negative():
    """A transition stamped a moment after the ``now`` a payload is built
    against -- the load switched on while the run was being assembled. Floor
    division would answer -1, which EMHASS 0.18 rejects outright."""
    load = _load()
    load.observe_power(1900, T0 + timedelta(milliseconds=300))

    assert load.completed_timesteps(T0, 15) == 0


# --- continuous on/off streaks (minimum on/off time) -------------------------


def test_continuous_on_timesteps_counts_from_running_since():
    load = _load()
    load.observe_power(1900, T0)
    now = T0 + timedelta(minutes=75)
    assert load.continuous_on_timesteps(now, 30) == 2
    assert load.continuous_on_timesteps(now, 15) == 5


def test_continuous_on_timesteps_is_zero_when_not_running():
    load = _load()
    assert load.continuous_on_timesteps(T0, 30) == 0


def test_continuous_off_timesteps_counts_from_off_since():
    load = _load()
    load.observe_power(1900, T0)
    load.observe_power(0, T0 + timedelta(minutes=10))
    now = T0 + timedelta(minutes=10) + timedelta(minutes=75)
    assert load.continuous_off_timesteps(now, 30) == 2


def test_continuous_off_timesteps_is_zero_before_any_transition_is_observed():
    """No off_since yet -- the same conservative fallback runtime_today uses,
    rather than assuming the load has been off since the dawn of time."""
    load = _load()
    assert load.continuous_off_timesteps(T0, 30) == 0


def test_continuous_streaks_never_go_negative():
    """Both streaks are stamped from Home Assistant state changes, so either can
    land just after the ``now`` a payload is built against; -1 fails the whole
    optimisation in EMHASS 0.18, not just this load."""
    load = _load()
    load.observe_power(1900, T0 + timedelta(milliseconds=300))
    assert load.continuous_on_timesteps(T0, 15) == 0

    load.observe_power(0, T0 + timedelta(milliseconds=600))
    assert load.continuous_off_timesteps(T0, 15) == 0


def test_switching_on_clears_the_off_streak():
    load = _load()
    load.observe_power(1900, T0)
    load.observe_power(0, T0 + timedelta(minutes=10))
    load.observe_power(1900, T0 + timedelta(minutes=20))
    assert load.continuous_off_timesteps(T0 + timedelta(minutes=30), 30) == 0
    assert load.continuous_on_timesteps(T0 + timedelta(minutes=30), 30) == 0


def test_switching_off_clears_the_on_streak():
    load = _load()
    load.observe_power(1900, T0)
    assert load.continuous_on_timesteps(T0 + timedelta(minutes=30), 30) == 1
    load.observe_power(0, T0 + timedelta(minutes=30))
    assert load.continuous_on_timesteps(T0 + timedelta(minutes=45), 30) == 0


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
    ],
)
def test_mode_overrides_the_schedule(mode, scheduled, expected):
    assert resolve_should_run(mode, scheduled) is expected


def test_mode_does_not_remove_a_load_from_the_optimisation():
    """Force on changes the signal, not what EMHASS is asked to solve.

    Parking a load is the Enabled switch's job. Conflating the two would
    silently change the problem whenever someone paused a load.
    """
    assert _load(mode=LOAD_MODE_FORCE_ON).to_load(T0, 30).wants_to_run is True


def test_force_run_arms_force_on():
    load = _load()
    load.force_run()
    assert load.mode == LOAD_MODE_FORCE_ON


def test_force_run_disarms_once_the_days_hours_are_reached():
    load = _load(operating_hours=1)
    load.force_run()
    load.observe_power(1900, T0)
    load.observe_power(0, T0 + timedelta(hours=1))
    assert load.check_auto_disarm(T0 + timedelta(hours=1)) is True
    assert load.mode == LOAD_MODE_AUTO


def test_force_run_stays_armed_until_the_days_hours_are_reached():
    load = _load(operating_hours=1)
    load.force_run()
    load.observe_power(1900, T0)
    load.observe_power(0, T0 + timedelta(minutes=30))
    assert load.check_auto_disarm(T0 + timedelta(minutes=30)) is False
    assert load.mode == LOAD_MODE_FORCE_ON


def test_force_run_measures_against_the_whole_day_not_a_fresh_window():
    """Unlike a request, a forced run has no anchor of its own.

    Hours already run earlier today count towards disarming it -- pressing
    the button tops up what is left of today's target, it does not grant a
    fresh hour on top.
    """
    load = _load(operating_hours=1)
    load.observe_power(1900, T0)
    load.observe_power(0, T0 + timedelta(minutes=45))
    load.force_run()
    assert load.check_auto_disarm(T0 + timedelta(minutes=45)) is False
    load.observe_power(1900, T0 + timedelta(minutes=45))
    load.observe_power(0, T0 + timedelta(hours=1))
    assert load.check_auto_disarm(T0 + timedelta(hours=1)) is True
    assert load.mode == LOAD_MODE_AUTO


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


def test_an_unrequested_on_demand_load_asks_for_nothing():
    """It is still described to EMHASS -- parked at zero hours, so that its
    deferrable number does not move -- but it wants no run time."""
    load = _load(recurrence=RECURRENCE_ON_DEMAND)
    assert load.to_load(T0, 30).wants_to_run is False


def test_a_requested_on_demand_load_asks_for_its_hours():
    load = _load(recurrence=RECURRENCE_ON_DEMAND, requested=True)
    assert load.to_load(T0, 30).wants_to_run is True


def test_disabled_wins_over_a_request():
    load = _load(recurrence=RECURRENCE_ON_DEMAND, requested=True, enabled=False)
    assert load.to_load(T0, 30).wants_to_run is False


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


# --- deadlines ----------------------------------------------------------------


def test_a_daily_load_has_no_deadline_however_run_within_is_set():
    """A duration needs an instant to count from, and a daily load has none.

    "Within 4 h of a fixed 06:00 opening" is just a narrower window, which
    latest_end already expresses -- so run_within is not silently reinterpreted
    into one.
    """
    load = _load(recurrence=RECURRENCE_DAILY, run_within_hours=4)
    load.request(T0)
    assert load.deadline_at is None
    assert load.on_demand is False


def test_a_deadline_is_anchored_at_the_request_not_at_now():
    """The whole reason requested_at exists: a deadline recomputed from "now"
    on every optimisation would slide forward and never arrive."""
    load = _load(recurrence=RECURRENCE_ON_DEMAND, run_within_hours=6)
    load.request(T0)
    assert load.deadline_at == T0 + timedelta(hours=6)
    # An hour later it is still the same instant, not T0 + 7h.
    assert load.deadline_at == T0 + timedelta(hours=6)


def test_run_within_zero_means_no_deadline():
    load = _load(recurrence=RECURRENCE_ON_DEMAND, run_within_hours=0)
    load.request(T0)
    assert load.deadline_at is None


def test_an_unrequested_on_demand_load_has_no_deadline():
    load = _load(recurrence=RECURRENCE_ON_DEMAND, run_within_hours=4)
    assert load.deadline_at is None


def test_the_deadline_reaches_the_payload_snapshot():
    load = _load(recurrence=RECURRENCE_ON_DEMAND, run_within_hours=4)
    load.request(T0)
    assert load.to_load(T0, 30).deadline_at == T0 + timedelta(hours=4)


def test_a_deadline_is_independent_of_the_time_window_switch():
    """The window is standing config, the deadline belongs to one request;
    both may apply, and the payload intersects them."""
    load = _load(recurrence=RECURRENCE_ON_DEMAND, run_within_hours=4)
    load.use_time_window = False
    load.earliest_start, load.latest_end = time(22, 0), time(6, 0)
    load.request(T0)
    snapshot = load.to_load(T0, 30)
    assert snapshot.earliest_start is None
    assert snapshot.deadline_at == T0 + timedelta(hours=4)


def test_cancelling_clears_the_anchor_as_well_as_the_flag():
    load = _load(recurrence=RECURRENCE_ON_DEMAND, run_within_hours=4)
    load.request(T0)
    load.cancel()
    assert (load.requested, load.requested_at) == (False, None)
    assert load.deadline_at is None


# --- progress against a request rather than the calendar day -------------------


def test_a_request_that_outlives_midnight_is_not_re_run():
    """The bug deadlines would otherwise make ordinary.

    def_current_operating_timesteps comes from the runtime accumulator, and
    reset_day zeroes the per-day one. A request armed at 23:00 would then be
    credited zero completed steps after midnight, and EMHASS would schedule
    the whole target a second time.
    """
    load = _load(recurrence=RECURRENCE_ON_DEMAND, operating_hours=2)
    load.request(T0)
    load.observe_power(1900, T0)
    midnight = T0 + timedelta(hours=1)
    load.reset_day(midnight)
    later = midnight + timedelta(hours=1)

    assert load.elapsed_today(later) == timedelta(hours=1)  # day counter restarted
    assert load.elapsed_towards_target(later) == timedelta(hours=2)  # request did not
    assert load.completed_timesteps(later, 30) == 4


def test_the_day_reset_keeps_counting_a_load_that_is_still_running():
    """Closing the open span to bank it against yesterday must not also stop
    the accumulator: a control entity left on may not change state for hours."""
    load = _load()
    load.observe_power(1900, T0)
    load.reset_day(T0 + timedelta(hours=1))
    assert load.is_running is True
    assert load.elapsed_today(T0 + timedelta(hours=2)) == timedelta(hours=1)


def test_the_day_reset_does_not_fake_an_off_streak_for_a_still_running_load():
    """_stop's bookkeeping sets off_since as a side effect of closing the
    open span for yesterday's accounting -- reset_day must undo that, or a
    load that never actually switched off would look like it just did."""
    load = _load()
    load.observe_power(1900, T0)
    load.reset_day(T0 + timedelta(hours=1))
    assert load.continuous_off_timesteps(T0 + timedelta(hours=2), 30) == 0
    assert load.continuous_on_timesteps(T0 + timedelta(hours=2), 30) == 2


def test_a_run_already_under_way_only_counts_from_the_request():
    """The user asked for a fresh run; minutes that predate the request would
    disarm it early."""
    load = _load(recurrence=RECURRENCE_ON_DEMAND, operating_hours=2)
    load.observe_power(1900, T0)
    load.request(T0 + timedelta(hours=3))
    assert load.elapsed_since_request(T0 + timedelta(hours=4)) == timedelta(hours=1)


def test_an_armed_request_with_no_anchor_falls_back_to_the_day():
    """A request restored from before anchors existed. Per-day accounting is
    the behaviour it was armed under, so it still completes."""
    load = _load(recurrence=RECURRENCE_ON_DEMAND, operating_hours=1, requested=True)
    load.observe_power(1900, T0)
    load.observe_power(0, T0 + timedelta(hours=1))
    assert load.check_auto_disarm(T0 + timedelta(hours=1)) is True


# --- what "should run" means when a load is not in the optimisation ------------


def test_an_excluded_load_participates_is_false_so_should_run_is_definite():
    """A disabled load, or an unrequested on-demand one, is not in the plan.

    That is a definite "do not run", not "we do not know" -- the plan is
    perfectly good, this load simply is not in it. See
    LoadShouldRunBinarySensor.
    """
    disabled = _load(enabled=False)
    assert disabled.enabled is False

    unrequested = _load(recurrence=RECURRENCE_ON_DEMAND, requested=False)
    assert unrequested.participates is False

    armed = _load(recurrence=RECURRENCE_ON_DEMAND, requested=True)
    assert armed.participates is True


# --- thermal loads -----------------------------------------------------------


def _thermal(**overrides) -> DeferrableRuntime:
    defaults = {
        "subentry_id": "hp",
        "name": "Heat pump",
        "load_type": LOAD_TYPE_THERMAL,
        "nominal_power_w": 3000.0,
    }
    return DeferrableRuntime(**{**defaults, **overrides})


def test_a_standard_load_projects_no_thermal_config():
    assert _load().to_load(T0, 30).thermal is None


def test_a_thermal_load_projects_its_live_comfort_band():
    """to_load reads the entity-owned values at request time, like every other
    optimiser input -- a dashboard tweak must reach the very next run."""
    load = _thermal()
    load.comfort_temperature = 22.0
    load.setback_temperature = 17.0
    load.max_temperature = 25.0
    load.heating_rate = 4.5
    load.cooling_constant = 0.2
    load.comfort_start = time(7, 0)
    load.comfort_end = time(21, 0)

    thermal = load.to_load(T0, 30, current_temperature=19.5).thermal
    assert thermal is not None
    assert thermal.comfort_temperature == 22.0
    assert thermal.setback_temperature == 17.0
    assert thermal.max_temperature == 25.0
    assert thermal.heating_rate == 4.5
    assert thermal.cooling_constant == 0.2
    assert thermal.comfort_start == time(7, 0)
    assert thermal.comfort_end == time(21, 0)
    assert thermal.current_temperature == 19.5


def test_an_unavailable_room_sensor_projects_no_start_temperature():
    thermal = _thermal().to_load(T0, 30, current_temperature=None).thermal
    assert thermal.current_temperature is None


def test_a_thermal_load_reports_no_completed_timesteps():
    """Completed work is measured against an operating-hours target, which a
    thermal load does not have. Reporting observed runtime anyway would tell
    EMHASS the (nonexistent) target is already partly served."""
    load = _thermal()
    load.observe_power(2900, T0)
    projected = load.to_load(T0 + timedelta(hours=2), 30)
    assert projected.completed_timesteps == 0
    # The observation itself still counts -- the runtime sensor shows it, and
    # current_state stops a spurious startup penalty.
    assert projected.current_state is True


def test_a_thermal_load_participates_every_day():
    """Its demand is the comfort band, which stands by nature."""
    load = _thermal()
    assert load.participates is True
    assert load.on_demand is False


def test_a_disabled_thermal_load_does_not_want_to_run():
    load = _thermal(enabled=False)
    assert load.to_load(T0, 30).wants_to_run is False


# --- surplus loads ------------------------------------------------------------


def _surplus(**overrides) -> DeferrableRuntime:
    """A pool: 800 W, on or off, armed and asking for spare solar."""
    defaults = {
        "name": "Pool",
        "nominal_power_w": 800.0,
        "recurrence": RECURRENCE_SURPLUS,
        "semi_continuous": True,
    }
    load = _load(**{**defaults, **overrides})
    load.request(T0)
    return load


def test_a_surplus_load_is_armed_by_the_same_switch_as_an_on_demand_one():
    load = _surplus()
    assert load.armable
    assert load.participates
    # ...but it is not "on demand": that gates the deadline-shaped settings,
    # which need a target run length to count down against.
    assert not load.on_demand
    assert load.deadline_at is None


def test_an_unarmed_surplus_load_asks_for_nothing():
    load = _load(recurrence=RECURRENCE_SURPLUS)
    assert not load.participates
    assert not load.to_load(T0, 15).wants_to_run


def test_a_surplus_load_sends_its_derived_hours_and_window():
    load = _surplus()
    load.operating_hours = 4.0  # configured, and deliberately ignored
    load.surplus_budget = SurplusBudget(
        hours=1.5, window_start=T0 + timedelta(hours=1), window_end=T0 + timedelta(hours=5)
    )

    projected = load.to_load(T0, 15)
    assert projected.operating_hours == 1.5
    assert projected.start_at == T0 + timedelta(hours=1)
    assert projected.end_at == T0 + timedelta(hours=5)


def test_a_surplus_load_ignores_its_standing_time_window():
    """Its window is the sun's, derived fresh from every plan."""
    load = _surplus(use_time_window=True, earliest_start=time(9, 0), latest_end=time(17, 0))
    load.surplus_budget = SurplusBudget(
        hours=1.0, window_start=T0, window_end=T0 + timedelta(hours=3)
    )

    projected = load.to_load(T0, 15)
    assert projected.earliest_start is None
    assert projected.latest_end is None


def test_a_surplus_load_reports_no_completed_timesteps():
    """Its budget is already "what is left", so subtracting progress again
    double-counts every hour served -- a pool two hours into a four-hour
    surplus would be told it needs two more and has done two, and would stop
    at noon with half the sun ahead of it."""
    load = _surplus()
    load.surplus_budget = SurplusBudget(
        hours=2.0, window_start=T0, window_end=T0 + timedelta(hours=4)
    )
    load.observe_power(800, T0)
    load.observe_power(0, T0 + timedelta(hours=2))

    assert load.elapsed_since_request(T0 + timedelta(hours=2)) == timedelta(hours=2)
    assert load.to_load(T0 + timedelta(hours=2), 15).completed_timesteps == 0


def test_a_surplus_load_with_nothing_to_spare_is_parked():
    """Zero hours *and* a window *and* a running state is the contradiction
    EMHASS answers by declaring the whole problem infeasible."""
    load = _surplus()
    assert load.surplus_budget.is_empty
    assert not load.to_load(T0, 15).wants_to_run


def test_a_surplus_load_stays_armed_after_running():
    """No operating-hours target means nothing for the on-demand rule to
    measure: an uncapped surplus load runs until the switch is turned off."""
    load = _surplus()
    load.observe_power(800, T0)
    load.observe_power(0, T0 + timedelta(hours=8))

    assert not load.check_auto_disarm(T0 + timedelta(hours=8))
    assert load.requested


def test_a_surplus_load_disarms_once_its_energy_cap_is_met():
    load = _surplus(energy_needed_kwh=1.6)  # 2 h at 800 W
    load.observe_power(800, T0)
    load.observe_power(0, T0 + timedelta(hours=2))

    assert load.delivered_energy_wh(T0 + timedelta(hours=2)) == 1600.0
    assert load.remaining_energy_wh(T0 + timedelta(hours=2)) == 0
    assert load.check_auto_disarm(T0 + timedelta(hours=2))
    assert not load.requested


def test_an_uncapped_surplus_load_has_no_remaining_energy_to_report():
    assert _surplus().remaining_energy_wh(T0) is None


def test_the_run_floor_follows_the_power_model():
    """A load that can only be on or off cannot use a timestep offering less
    than its nominal power; one that may modulate can go down to its floor."""
    assert _surplus(semi_continuous=True).surplus_run_floor_w == 800.0
    assert _surplus(semi_continuous=False, minimum_power_w=250.0).surplus_run_floor_w == 250.0

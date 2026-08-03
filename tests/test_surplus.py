"""Tests for the surplus budget derivation.

The whole feature is arithmetic the user never sees the inputs to, so these
cover the two ways it can be silently wrong: crediting a load with energy it
cannot actually take, and letting a load's own consumption erase the surplus
that justified it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.emhass_companion.models import Plan, PlanRow, Point, Series
from custom_components.emhass_companion.surplus import (
    SurplusSpec,
    allocate,
    battery_reserved_series,
    surplus_series,
    total_energy_wh,
    window_of,
)

STEP = timedelta(minutes=15)
START = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)


def _series(*values: float) -> Series:
    return Series(Point(START + index * STEP, value) for index, value in enumerate(values))


def _spec(**overrides) -> SurplusSpec:
    defaults = {
        "subentry_id": "pool",
        "nominal_w": 800.0,
        "run_floor_w": 800.0,
        "semi_continuous": True,
        "headroom_w": 0.0,
    }
    return SurplusSpec(**{**defaults, **overrides})


# --- the series ---------------------------------------------------------------


def test_surplus_is_pv_minus_load_with_surplus_loads_added_back():
    """A surplus load's own draw must not erase the surplus that fed it.

    Without the add-back a running surplus load's own consumption would be
    subtracted like any other deferrable's, collapsing the series exactly when
    the load runs -- and the budget derived from it would switch the load off
    every time it switched on.
    """
    plan = Plan(
        generated_at=START,
        schema_version="1.0",
        rows=[PlanRow(timestamp=START, p_pv=4000.0, p_load=1000.0, deferrables=(500.0, 800.0))],
    )

    # Index 1 is the surplus load itself: left out of the subtraction, only
    # the ordinary deferrable at index 0 counts against the 3000 W base.
    assert surplus_series(plan, [1]).values == (2500.0,)
    # Index 0 is an ordinary deferrable: its consumption is a real claim on the
    # house's energy, and promising it to a surplus load as well would sell the
    # same kilowatt-hour twice.
    assert surplus_series(plan, []).values == (1700.0,)


def test_load_exceeding_pv_leaves_no_surplus():
    plan = Plan(
        generated_at=START,
        schema_version="1.0",
        rows=[PlanRow(timestamp=START, p_pv=800.0, p_load=1200.0, deferrables=())],
    )
    assert surplus_series(plan, []).values == (0.0,)


def test_battery_charging_does_not_suppress_the_reading():
    """Spare PV is spare PV even when the plan routes it into the battery.

    Reading ``-p_grid`` instead would let the battery silently outrank a
    surplus load for the same watt any time it still has room to charge --
    exactly the bug this series exists to avoid. 4000 W PV against 1000 W of
    house load is 3000 W spare, whether or not the battery absorbs every watt
    of it and the grid sees nothing.
    """
    plan = Plan(
        generated_at=START,
        schema_version="1.0",
        rows=[
            PlanRow(
                timestamp=START,
                p_pv=4000.0,
                p_load=1000.0,
                p_batt=-3000.0,
                p_grid=0.0,
                deferrables=(),
            )
        ],
    )
    assert surplus_series(plan, []).values == (3000.0,)


def test_rows_without_pv_or_load_figures_are_skipped():
    plan = Plan(
        generated_at=START,
        schema_version="1.0",
        rows=[
            PlanRow(timestamp=START, p_pv=None, p_load=0.0),
            PlanRow(timestamp=START + STEP, p_pv=100.0, p_load=0.0),
        ],
    )
    assert surplus_series(plan, []).values == (100.0,)


# --- what the battery already claims -------------------------------------------


def test_battery_reserved_series_reads_charging_power():
    """P_batt is positive for discharge, negative for charge -- only charging
    is a claim on the surplus; a discharging or idle battery has nothing here
    for a surplus load to be shut out of."""
    plan = Plan(
        generated_at=START,
        schema_version="1.0",
        rows=[
            PlanRow(timestamp=START, p_batt=-1200.0),
            PlanRow(timestamp=START + STEP, p_batt=800.0),
            PlanRow(timestamp=START + 2 * STEP, p_batt=0.0),
        ],
    )
    assert battery_reserved_series(plan).values == (1200.0, 0.0, 0.0)


def test_battery_reserved_series_skips_rows_without_a_figure():
    plan = Plan(
        generated_at=START,
        schema_version="1.0",
        rows=[
            PlanRow(timestamp=START, p_batt=None),
            PlanRow(timestamp=START + STEP, p_batt=-500.0),
        ],
    )
    assert battery_reserved_series(plan).values == (500.0,)


# --- semi-continuous: a slot count -------------------------------------------


def test_semi_continuous_budget_counts_slots_not_energy():
    """A 3 kW slot still only feeds an 800 W pool 800 W.

    Summing the slots' own energy instead would credit the pool with nearly
    2.5x the run time it can actually be given, and EMHASS would import the
    difference to satisfy the hours it was handed.
    """
    budget = allocate(_series(200, 900, 3000, 3000, 900, 400), [_spec()], STEP)["pool"]

    assert budget.steps == 4
    assert budget.hours == 1.0
    assert budget.energy_wh == 800.0


def test_window_brackets_the_qualifying_slots():
    budget = allocate(_series(200, 900, 3000, 400), [_spec()], STEP)["pool"]

    assert budget.window_start == START + STEP
    # Past the last qualifying slot, with a step of slack: the window is
    # re-derived against "now" in the payload, where a start rounds up and an
    # end rounds down, and a window one step too narrow for the hours makes
    # EMHASS declare the whole problem infeasible.
    assert budget.window_end == START + 4 * STEP


def test_window_does_not_reach_across_the_night_into_tomorrow():
    """A horizon starting mid-morning also covers a slice of tomorrow's sun.

    Without a cut at the first real gap, the window would splice today's
    afternoon onto tomorrow's morning and span the whole night in between --
    handing EMHASS a window that technically permits a 3 a.m. start, which is
    exactly what the window exists to rule out.
    """
    day_one = (3000.0, 3000.0, 3000.0, 3000.0)  # 1 h, well above run_floor
    night = (0.0,) * 8  # 2 h with nothing to offer -- not even the floor
    day_two = (3000.0, 3000.0, 3000.0, 3000.0)  # "tomorrow", still in horizon
    budget = allocate(_series(*day_one, *night, *day_two), [_spec()], STEP)["pool"]

    assert budget.window_start == START
    assert budget.window_end == START + 5 * STEP  # end of day_one, not day_two
    assert budget.hours == 1.0
    assert budget.energy_wh == 800.0


def test_a_single_isolated_zero_does_not_end_the_block():
    """The joint solve can legitimately send one whole slot's production to
    the battery instead of exporting it -- p_grid and every surplus load's
    own draw both read zero for that slot, indistinguishable from the sun
    going down unless something rides through it. Ending the block there (as
    a real night-sized gap correctly does) would badly understate a load's
    real remaining hours.
    """
    series = _series(3000, 3000, 0, 3000, 3000, 3000)  # one battery-took-it-all slot
    budget = allocate(series, [_spec()], STEP)["pool"]

    assert budget.window_start == START
    assert budget.window_end == START + 7 * STEP
    assert budget.steps == 5
    assert budget.hours == 1.25


def test_a_gap_longer_than_the_tolerance_still_ends_the_block():
    """The tolerance is for noise, not for genuinely losing the sun for a
    while -- a gap that outlasts it must still end the block, or the fix
    above would simply reopen the original night-spanning bug."""
    day_one = (3000.0, 3000.0)
    long_gap = (0.0,) * 5  # over an hour at a 15 min step -- past the tolerance
    day_two = (3000.0, 3000.0)
    budget = allocate(_series(*day_one, *long_gap, *day_two), [_spec()], STEP)["pool"]

    assert budget.window_end == START + 3 * STEP  # end of day_one, not day_two
    assert budget.hours == 0.5


def test_headroom_excludes_marginal_slots():
    """Slots between the load's power and power+headroom stop counting.

    They stay safe to *run* through, which is what lets a startup penalty
    produce one unbroken run without a hard contiguity constraint.
    """
    budget = allocate(_series(200, 900, 3000, 3000, 900, 400), [_spec(headroom_w=500.0)], STEP)[
        "pool"
    ]

    assert budget.steps == 2
    assert budget.hours == 0.5


# --- variable power: clipped energy ------------------------------------------


def test_variable_power_clips_each_slot_at_nominal():
    """A 2 kW charger cannot absorb a 3 kW slot.

    Without the clip the budget over-commits by the difference -- the failure
    mode a hand-written template avoids only by setting nominal power to the
    day's peak surplus, which stops being true the moment the charger's own
    limit is lower.
    """
    spec = _spec(
        subentry_id="car",
        nominal_w=2000.0,
        run_floor_w=1500.0,
        semi_continuous=False,
    )
    budget = allocate(_series(200, 900, 3000, 3000, 900), [spec], STEP)["car"]

    assert budget.energy_wh == 1000.0
    assert budget.hours == 0.5


def test_variable_power_uses_partial_slots():
    """Clipping still bites even once the ceiling adapts to the day.

    Both slots qualify and the ceiling clamps to the better of the two
    (1600 W), but the weaker slot still only gives up what it actually has --
    the fractional result is that clip, not a ceiling/rated mismatch.
    """
    spec = _spec(nominal_w=2000.0, run_floor_w=1000.0, semi_continuous=False)
    budget = allocate(_series(1600, 1200), [spec], STEP)["pool"]

    assert budget.nominal_w == 1600.0
    assert budget.energy_wh == 700.0
    assert budget.hours == 0.4375


# --- variable power: the ceiling sent to EMHASS --------------------------------


def test_variable_power_ceiling_clamps_to_the_best_qualifying_slot():
    """An 11 kW charger on a 3 kW day should not turn into a short burst.

    Sizing ``hours`` against the configured (unreachable) ceiling understates
    the hours needed and hands EMHASS a power bound the day cannot back up --
    a short, near-ceiling run that imports the difference. Clamping the
    ceiling to what was actually seen turns the same energy into a longer,
    lower-power run instead.
    """
    spec = _spec(nominal_w=11000.0, run_floor_w=1400.0, semi_continuous=False)
    budget = allocate(_series(3000, 3000), [spec], STEP)["pool"]

    assert budget.nominal_w == 3000.0
    assert budget.energy_wh == 1500.0
    assert budget.hours == 0.5


def test_variable_power_ceiling_never_exceeds_the_configured_maximum():
    """The configured value is the appliance's real limit, not a guess.

    A day with more surplus than the load can take must still clamp to what
    the load can actually draw.
    """
    spec = _spec(nominal_w=2000.0, run_floor_w=1000.0, semi_continuous=False)
    budget = allocate(_series(3000, 3000), [spec], STEP)["pool"]

    assert budget.nominal_w == 2000.0


def test_full_power_only_ceiling_is_never_clamped():
    """A semi-continuous load has nothing to ramp -- the configured power is
    always what is sent, exactly as before this existed."""
    spec = _spec(nominal_w=800.0, run_floor_w=800.0, semi_continuous=True)
    budget = allocate(_series(200, 3000, 3000, 400), [spec], STEP)["pool"]

    assert budget.nominal_w == 800.0


# --- sharing ------------------------------------------------------------------


def test_competing_loads_do_not_each_get_the_whole_surplus():
    """The optimiser cannot arbitrate this: neither load is in the plan yet.

    Reading the same series twice tells both loads the surplus is theirs, and
    running them together imports the difference.
    """
    budgets = allocate(
        _series(1000, 1700, 3000),
        [_spec(subentry_id="first"), _spec(subentry_id="second")],
        STEP,
    )

    assert budgets["first"].steps == 3
    # After the first load takes 800 W from each: 200, 900, 2200.
    assert budgets["second"].steps == 2


# --- battery reservation --------------------------------------------------------


def test_reservation_narrows_the_energy_but_not_the_window():
    """The window is about whether the sun clears the floor, full stop.

    Two of the four slots are almost entirely reserved for the battery and
    contribute nothing to the budget -- but they are still gross-qualifying,
    so the window sent to EMHASS still spans all four, exactly as it would
    with no reservation at all. Only ``hours`` shrinks.
    """
    budget = allocate(
        _series(3000, 3000, 3000, 3000),
        [_spec()],
        STEP,
        reserved=_series(2500, 2500, 0, 0),
    )["pool"]

    assert budget.window_start == START
    assert budget.window_end == START + 5 * STEP
    # Net available is 500, 500, 3000, 3000 -- only the last two clear the
    # 800 W floor.
    assert budget.steps == 2
    assert budget.energy_wh == 400.0
    assert budget.hours == 0.5


def test_full_reservation_empties_the_budget_but_keeps_the_window():
    """The battery can claim a gross-qualifying slot entirely.

    ``is_empty`` still reads true off ``hours``, but ``window_start`` stays
    set -- the sun really was up, there was simply nothing left of it.
    """
    budget = allocate(
        _series(3000, 3000),
        [_spec()],
        STEP,
        reserved=_series(3000, 3000),
    )["pool"]

    assert budget.window_start == START
    assert budget.steps == 0
    assert budget.hours == 0.0
    assert budget.is_empty


def test_reservation_is_taken_off_before_priority_sharing():
    """The battery's claim comes off the top, before any surplus load --
    including the highest-priority one -- gets to see the series."""
    budgets = allocate(
        _series(3000, 3000),
        [_spec(subentry_id="first"), _spec(subentry_id="second")],
        STEP,
        reserved=_series(2000, 2000),
    )

    # Net is 1000, 1000 -- enough for one 800 W load to clear the floor twice,
    # leaving 200, 200 for the next in line, which is below its own floor.
    assert budgets["first"].steps == 2
    assert budgets["second"].steps == 0
    assert budgets["second"].is_empty


# --- energy cap ---------------------------------------------------------------


def test_energy_cap_limits_the_budget():
    budget = allocate(_series(3000, 3000, 3000, 3000), [_spec(max_energy_wh=300.0)], STEP)["pool"]

    assert budget.energy_wh == 300.0
    assert budget.hours == 0.375


def test_no_cap_takes_everything_available():
    budget = allocate(_series(3000, 3000), [_spec(max_energy_wh=None)], STEP)["pool"]
    assert budget.steps == 2


# --- nothing to give ----------------------------------------------------------


def test_no_surplus_gives_an_empty_budget():
    budget = allocate(_series(0, 100, 0), [_spec()], STEP)["pool"]

    assert budget.hours == 0.0
    assert budget.is_empty


def test_empty_series_gives_an_empty_budget():
    assert allocate(Series.empty(), [_spec()], STEP)["pool"].is_empty


def test_a_load_with_no_nominal_power_gets_nothing():
    """Dividing energy by nominal power to get hours needs a positive power."""
    budget = allocate(_series(5000), [_spec(nominal_w=0.0, run_floor_w=0.0)], STEP)["pool"]
    assert budget.is_empty


# --- reporting helpers --------------------------------------------------------


def test_window_spans_dips_rather_than_stopping_at_the_first():
    """ "Spare sun until 15:30" is the useful answer; the gaps are the
    optimiser's problem, not the user's."""
    start, end = window_of(_series(100, 600, 200, 900, 50), 500.0)

    assert start == START + STEP
    assert end == START + 3 * STEP


def test_no_window_when_nothing_clears_the_threshold():
    assert window_of(_series(100, 200), 500.0) == (None, None)


def test_total_energy():
    assert total_energy_wh(_series(1000, 1000), STEP) == 500.0


# --- end to end: registry -> payload -----------------------------------------


def _registry(*loads):
    """A registry with no Home Assistant behind it.

    ``apply_surplus`` touches nothing but the loads it holds, and going through
    the real constructor would drag in a ``hass`` fixture for no added coverage.
    """
    from custom_components.emhass_companion.deferrable import DeferrableRegistry

    registry = DeferrableRegistry.__new__(DeferrableRegistry)
    registry._loads = {load.subentry_id: load for load in loads}
    return registry


def _pool():
    from custom_components.emhass_companion.const import RECURRENCE_SURPLUS
    from custom_components.emhass_companion.deferrable import DeferrableRuntime

    pool = DeferrableRuntime(
        subentry_id="pool",
        name="Pool",
        nominal_power_w=800.0,
        recurrence=RECURRENCE_SURPLUS,
        semi_continuous=True,
        surplus_headroom_w=300.0,
    )
    pool.request(START)
    return pool


def _plan_with_surplus_between(start_hour: int, end_hour: int, watts: float = 3000.0):
    """A 12-hour plan with ``watts`` of spare PV between the two hours, from START."""
    rows = []
    for index in range(48):
        hour = START.hour + index / 4
        exporting = start_hour <= hour < end_hour
        rows.append(
            PlanRow(
                timestamp=START + index * STEP,
                p_pv=watts if exporting else 0.0,
                p_load=0.0,
                deferrables=(0.0,),
            )
        )
    return Plan(generated_at=START, schema_version="1.0", rows=rows)


def _payload(loads, now):
    from custom_components.emhass_companion.models import (
        BatteryConfig,
        GridConfig,
        HybridInverterConfig,
    )
    from custom_components.emhass_companion.payload import PayloadInputs, build_payload

    return build_payload(
        PayloadInputs(
            action="naive-mpc-optim",
            now=now,
            time_step_minutes=15,
            horizon_steps=48,
            battery=BatteryConfig(),
            grid=GridConfig(),
            hybrid_inverter=HybridInverterConfig(),
            loads=loads,
        )
    )


def test_a_surplus_load_reaches_the_payload_as_hours_and_a_window():
    """START is 10:00; the plan exports from 11:00 to 15:00."""
    pool = _pool()
    registry = _registry(pool)
    registry.apply_surplus(_plan_with_surplus_between(11, 15), ["pool"], START, 15)

    result = _payload(registry.to_loads(START, 15), START)
    payload = result.payload

    assert payload["operating_timesteps_of_each_deferrable_load"] == [16]
    # 11:00 is four steps after 10:00; the window closes at 15:15 (step 21),
    # leaving 17 steps for a 16-step run. It fits, so EMHASS is not handed a
    # window it would report as an infeasible problem.
    assert payload["start_timesteps_of_each_deferrable_load"] == [4]
    assert payload["end_timesteps_of_each_deferrable_load"] == [21]
    assert not result.warnings


def test_progress_shrinks_the_budget_but_is_never_sent_as_completed():
    """The double-subtraction this feature would otherwise walk into.

    At 13:00 the pool has had two of the four surplus hours and two remain. The
    forward budget is 2 h *because two hours of sun have gone by* -- so sending
    "2 h needed, 2 h done" as well would zero it out and stop the pool at noon
    with half the sun still ahead.
    """
    pool = _pool()
    registry = _registry(pool)
    plan = _plan_with_surplus_between(11, 15)
    later = START + timedelta(hours=3)

    pool.observe_power(800, START + timedelta(hours=1))
    pool.observe_power(0, later)
    registry.apply_surplus(plan, ["pool"], later, 15)

    assert pool.elapsed_since_request(later) == timedelta(hours=2)
    assert pool.surplus_budget.hours == 2.0
    assert _payload(registry.to_loads(later, 15), later).payload[
        "def_current_operating_timesteps"
    ] == [0]


def test_surplus_priority_overrides_name_order():
    """ "Aaa" sorts before "Zzz" by name, but priority is what decides sharing.

    A single 800 W slot's worth of surplus can only feed one semi-continuous
    800 W load. Giving "Zzz" the lower (first-served) priority number must
    make it the one that gets fed, even though the registry's own stable
    order -- used for P_deferrable numbering, not sharing -- would put "Aaa"
    first.
    """
    from custom_components.emhass_companion.const import RECURRENCE_SURPLUS
    from custom_components.emhass_companion.deferrable import DeferrableRuntime

    first_by_name = DeferrableRuntime(
        subentry_id="aaa",
        name="Aaa",
        nominal_power_w=800.0,
        recurrence=RECURRENCE_SURPLUS,
        semi_continuous=True,
        surplus_headroom_w=0.0,
        surplus_priority=1,
    )
    first_by_priority = DeferrableRuntime(
        subentry_id="zzz",
        name="Zzz",
        nominal_power_w=800.0,
        recurrence=RECURRENCE_SURPLUS,
        semi_continuous=True,
        surplus_headroom_w=0.0,
        surplus_priority=0,
    )
    first_by_name.request(START)
    first_by_priority.request(START)

    registry = _registry(first_by_name, first_by_priority)
    registry.apply_surplus(
        _plan_with_surplus_between(11, 12, watts=800.0), ["aaa", "zzz"], START, 15
    )

    assert first_by_priority.surplus_budget.hours == 1.0
    assert first_by_name.surplus_budget.is_empty


def test_no_plan_parks_the_load_rather_than_reusing_the_last_budget():
    """The state after a restart, when the honest answer is "ask for nothing"."""
    pool = _pool()
    registry = _registry(pool)
    registry.apply_surplus(_plan_with_surplus_between(11, 15), ["pool"], START, 15)
    assert pool.surplus_budget.hours > 0

    registry.apply_surplus(None, [], START, 15)

    assert pool.surplus_budget.is_empty
    assert _payload(registry.to_loads(START, 15), START).payload[
        "operating_hours_of_each_deferrable_load"
    ] == [0.0]

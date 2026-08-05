"""Turning the plan's spare PV into a run-time budget.

EMHASS's cost function is imports minus export revenue, with no value term for
consumption at all: any load it is not *forced* to run is pure cost, so the
solver sets it to zero. There is no way to say "run this if it is free" in the
MILP. A load that only ever wants PV the house doesn't need for itself
therefore cannot be expressed directly -- its run time has to be computed here,
from the surplus the previous plan predicted, and handed to EMHASS as an
ordinary ``def_total_hours`` target clamped to the window that surplus occupies.

That makes this a lagged single-pass: the budget for this run comes from the
plan the *last* run produced. A second solve per cycle would remove the lag but
also feed its own output back into its own input, and the budget would oscillate
between runs. At an MPC interval the lag is one cycle, and the whole budget is
re-derived from scratch every time, so an error corrects itself immediately.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import Plan, Point, Series

# How long a stretch of "can't even feed the floor" has to persist before
# allocate() treats it as the block actually ending, rather than noise. A
# real night is many hours long; a passing cloud, or the house load spiking
# for a few minutes, can legitimately dip one whole MPC slot's PV below the
# floor without the sun actually having gone anywhere. An hour comfortably
# rides through a slot like that -- which is what the headroom band and
# startup penalty are already meant to tolerate -- while never being mistaken
# for a real dusk-to-dawn gap.
BLOCK_GAP_TOLERANCE = timedelta(hours=1)


def surplus_series(plan: Plan, surplus_indices: Sequence[int]) -> Series:
    """PV the house doesn't need for itself, before any surplus load takes it.

    ``P_PV - P_load``, minus every *ordinary* deferrable's own draw -- a real
    claim on the house's energy, same as the house load itself. Deliberately
    ignores what the plan does with whatever is left: whether the battery
    charges on it, it gets exported, or curtailment throws it away is the
    optimiser's call, not a statement about whether the sun is up. Reading
    ``-p_grid`` instead would answer a different question -- once the battery
    is free to soak up PV ahead of a surplus load, a slot with plenty of sun
    can export nothing at all, and a load's start would get pushed out for a
    reason that has nothing to do with the weather. A start delay is meant to
    mean "PV can't cover the house and its other loads right now" -- night,
    early morning, heavy cloud -- not "the battery had a better idea for it."

    Only *surplus* loads' own draw is left out of the subtraction. An ordinary
    deferrable's consumption is a real claim on the house's energy -- leaving
    it out too would promise the same kilowatt-hour to the pool as well.
    Leaving a surplus load's own draw in would erase the surplus that justified
    it: once the load is in the plan, its own consumption must not count
    against next cycle's budget, or the load would switch itself off every
    time it switched on.
    """
    points: list[Point] = []
    for row in plan.rows:
        if row.p_pv is None or row.p_load is None:
            continue
        available = row.p_pv - row.p_load
        for index, power in enumerate(row.deferrables):
            if index not in surplus_indices:
                available -= power
        points.append(Point(row.timestamp, max(0.0, available)))
    return Series(points)


@dataclass(frozen=True, slots=True)
class SurplusSpec:
    """What one surplus load would do with the surplus, if it got any."""

    subentry_id: str
    nominal_w: float
    run_floor_w: float
    """The power below which running this load is pointless -- its nominal
    power when it can only be on or off, its minimum power when it can
    modulate. A timestep offering less than this is not a timestep it can
    use."""

    semi_continuous: bool
    headroom_w: float
    max_energy_wh: float | None = None
    """Remaining energy cap, or None for "take everything available"."""


@dataclass(slots=True)
class SurplusBudget:
    """What one load may ask EMHASS for on this run."""

    hours: float = 0.0
    window_start: datetime | None = None
    window_end: datetime | None = None
    energy_wh: float = 0.0
    steps: int = 0
    nominal_w: float = 0.0
    """The ceiling this budget's hours/energy were computed against, and what
    should be sent to EMHASS as this load's nominal power for the run -- see
    ``allocate``. Equal to the spec's configured nominal power for a
    semi-continuous load; for one that may modulate it is clamped to the
    highest surplus actually seen in a qualifying slot, so a config with
    headroom to spare (an 11 kW charger on a 3 kW day) does not turn into a
    short, near-ceiling burst that imports the difference."""

    @property
    def is_empty(self) -> bool:
        return self.hours <= 0 or self.window_start is None


def allocate(
    series: Series,
    specs: Sequence[SurplusSpec],
    step: timedelta,
    *,
    reserved: Series | None = None,
) -> dict[str, SurplusBudget]:
    """Share one surplus series between the loads competing for it.

    ``specs`` is in priority order, and each load's draw is subtracted from
    what the next one sees. Without that every surplus load reads the same
    number, all of them are told the whole surplus is theirs, and running two
    of them together imports the difference. EMHASS cannot arbitrate this for
    us: none of these loads is in the plan the budget was derived from.

    The arithmetic differs by load type, which is the trap worth naming. A
    semi-continuous load draws exactly its nominal power whenever it runs, so a
    timestep offering 3 kW of surplus still only feeds it 800 W -- counting the
    slot's full energy would over-commit it by the difference. Clipping each
    slot's contribution at what the load can actually take makes one formula
    (``hours = usable energy / nominal power``) correct for both: for a
    semi-continuous load it reduces to a slot count, and for a modulating one
    it is the clipped energy sum.

    ``reserved`` is what something outside every spec here has already
    claimed from the same series -- currently just the battery's own charge
    toward its target (see ``battery_reserved_series``). It narrows how much
    *energy* a slot can still give a surplus load, but never whether the slot
    belongs to the window at all: a slot's membership in the block, and the
    window sent to EMHASS, are decided from the *gross* series alone, exactly
    as without ``reserved``. Otherwise the battery would be back to gating
    *when* a surplus load may run, not just how much of a claimed slot is left
    over for it -- the bug ``surplus_series`` exists to avoid (see
    surplus_loads.md).
    """
    step_hours = step.total_seconds() / 3600
    if step_hours <= 0:
        return {}

    reserved_at = {point.time: point.value for point in reserved} if reserved else {}
    # [time, gross, net]: gross alone decides block membership and the window;
    # net -- gross minus whatever is already reserved -- is what a spec may
    # actually draw and count towards its energy. Each load consumes from its
    # own ``net`` column in turn, mutated in place.
    remaining = [
        [point.time, point.value, max(0.0, point.value - reserved_at.get(point.time, 0.0))]
        for point in series
    ]
    budgets: dict[str, SurplusBudget] = {}

    for spec in specs:
        if spec.nominal_w <= 0:
            budgets[spec.subentry_id] = SurplusBudget()
            continue

        # Headroom is what separates "counts towards the budget" from "would
        # actually import". A slot between the two is not worth committing to,
        # but is still safe for the load to run through, which is what lets a
        # startup penalty produce one unbroken run on an ordinary day without a
        # hard contiguity constraint forcing one on a broken-cloud day.
        threshold = spec.run_floor_w + spec.headroom_w

        # Only the current surplus block -- from here to the first stretch
        # that can't even feed the load's floor for longer than
        # BLOCK_GAP_TOLERANCE. A horizon that starts mid-morning reaches past
        # sunset into tomorrow's sunrise too; left unbounded, a block would
        # splice tonight's tail onto tomorrow's, and the window below would
        # span the whole night in between -- exactly what it exists to rule
        # out (see DeferrableRuntime.to_load). Nothing is lost by stopping
        # here: the next block is this load's problem on a later cycle, once
        # "now" has actually moved into it.
        gap_steps = max(1, int(BLOCK_GAP_TOLERANCE / step))
        block = []
        started = False
        gap_run = 0
        # From the gross column alone: whether a slot ends or extends the
        # block, and whether it counts towards the window sent to EMHASS, is
        # about whether the sun clears the floor -- not about how much of
        # that is already reserved. See the ``reserved`` note above.
        window_first: datetime | None = None
        window_last: datetime | None = None
        for entry in remaining:
            when, gross, _ = entry
            if gross < spec.run_floor_w:
                if not started:
                    continue
                gap_run += 1
                if gap_run >= gap_steps:
                    break
                # Still inside a tolerated gap -- kept so the main loop below
                # sees it too, exactly like any other sub-threshold slot: it
                # will not qualify or add to the budget, but does not end the
                # block either.
                block.append(entry)
                continue
            gap_run = 0
            started = True
            block.append(entry)
            if gross >= threshold:
                if window_first is None:
                    window_first = when
                window_last = when

        # A semi-continuous load is either off or at its configured power --
        # there is nothing to clamp. One that may modulate only ever needs as
        # much ceiling as the best qualifying slot actually offers; sizing
        # ``hours`` against a higher, merely-configured ceiling (a charger's
        # rated max, say, on a day that never gets there) understates the
        # hours needed and overstates the power EMHASS may use for them, which
        # trades a short near-ceiling burst -- and the import it causes -- for
        # what should have been a longer run spread across the whole window.
        # The ceiling itself is sized from ``net``: what is left after the
        # battery's own claim is the real limit on what this load can draw,
        # even in a gross-qualifying slot.
        nominal_w = spec.nominal_w
        if not spec.semi_continuous:
            qualifying_peak = max(
                (net for _, gross, net in block if gross >= threshold),
                default=0.0,
            )
            if qualifying_peak > 0:
                nominal_w = min(spec.nominal_w, qualifying_peak)

        # ``credit_wh`` is the *aggregate* surplus the block offers, uncapped
        # by what this load can draw in any one slot -- a run of strong slots
        # makes up for a few only-just-qualifying ones, instead of every slot
        # having to individually clear the load's own floor before it counts
        # at all. That is deliberately not the same total as what the load
        # will physically draw: the shared series (``entry[2]``, what the
        # next-priority load sees) and the energy-needed cap both have to be
        # debited by the load's real, physically-limited draw -- crediting
        # Pool with a slot's full 3 kW must not make that 3 kW invisible to
        # whatever is queued behind it, or to a cap that means "delivered
        # kWh".
        credit_wh = 0.0
        steps = 0

        for entry in block:
            when, _, net = entry
            if net < threshold:
                continue

            draw = nominal_w if spec.semi_continuous else min(net, nominal_w)
            if draw <= 0:
                continue

            credit_wh += net * step_hours
            steps += 1
            entry[2] = max(0.0, net - draw)

        # However generous the block, this load can never be asked to run
        # more hours than the qualifying span itself covers -- inclusive of
        # the last qualifying timestep's own interval, one step wide. That is
        # deliberately the *tight* span, not the wider window below: the
        # window is padded by an extra step to absorb payload.py's later
        # rounding against a different "now", and capping hours against that
        # padding as well would ask this load to run one step nobody's
        # surplus ever actually backed.
        span_hours = (
            0.0
            if window_last is None
            else (window_last - window_first).total_seconds() / 3600 + step_hours
        )
        hours = credit_wh / nominal_w if nominal_w > 0 else 0.0
        hours = min(hours, span_hours)
        if spec.max_energy_wh is not None:
            # A hard cap on delivered energy, not on the aggregate credit --
            # sizing it against nominal_w is exact for a semi-continuous load
            # (it draws exactly that whenever it runs) and the same
            # already-accepted approximation the rest of this budget makes
            # for one that modulates.
            hours = min(hours, spec.max_energy_wh / nominal_w)
        energy_wh = hours * nominal_w

        budgets[spec.subentry_id] = SurplusBudget(
            hours=hours,
            window_start=window_first,
            # Inclusive of the last qualifying timestep, plus one more. The
            # window is re-derived against "now" in payload.resolve_load_window,
            # where a start rounds up and an end rounds down; without the extra
            # step that rounding can hand EMHASS a window one timestep too
            # narrow for the hours being requested, which it reports as an
            # infeasible *problem* rather than a narrow window. Erring wide
            # risks one timestep of running outside the surplus; erring narrow
            # risks taking the battery and every other load down with it. Wider
            # than ``span_hours`` above on purpose -- see that comment.
            window_end=None if window_last is None else window_last + 2 * step,
            energy_wh=energy_wh,
            steps=steps,
            nominal_w=nominal_w,
        )

    return budgets


def battery_reserved_series(plan: Plan) -> Series:
    """What the plan's own battery charging already claims from the surplus.

    Read straight from ``P_batt`` (positive = discharge, negative = charge):
    the plan was solved honouring ``battery_target_state_of_charge`` already,
    so this is exactly what the battery still wants to reach its target, with
    every price and efficiency consideration EMHASS itself already weighed.
    Recomputing that here from SOC/target/capacity would duplicate EMHASS's
    own battery model and risk quietly disagreeing with it. Discharge and idle
    rows reserve nothing -- there is nothing here for a surplus load to be
    shut out of.
    """
    points: list[Point] = []
    for row in plan.rows:
        if row.p_batt is None:
            continue
        points.append(Point(row.timestamp, max(0.0, -row.p_batt)))
    return Series(points)


# Below this, there is effectively no PV at all -- used only to tell a real
# night apart from a passing cloud when cutting the *block* a reporting
# sensor describes. Deliberately not ``surplus_threshold_w``: that number is
# the user's "what counts as spare" setting (see the number's own docstring,
# it's display-only), and a cloud dipping below a high threshold for a couple
# of hours is still broad daylight, not a reason to end the block.
NIGHT_FLOOR_W = 1.0


def current_block(series: Series, step: timedelta) -> Series:
    """The series' current stretch, cut before it reaches a real gap.

    Mirrors the block ``allocate`` cuts per load (see its docstring): a
    horizon that starts mid-afternoon also reaches past sunset into
    tomorrow's sunrise, and without this cut the hub's reporting sensors --
    from/until and the energy total -- would describe that whole stretch as
    still ahead, tomorrow's forecast included. A short dip rides through
    inside ``BLOCK_GAP_TOLERANCE``; a real night does not.
    """
    if step <= timedelta(0):
        return series
    gap_steps = max(1, int(BLOCK_GAP_TOLERANCE / step))
    block: list[Point] = []
    started = False
    gap_run = 0
    for point in series:
        if point.value < NIGHT_FLOOR_W:
            if not started:
                continue
            gap_run += 1
            if gap_run >= gap_steps:
                break
            block.append(point)
            continue
        gap_run = 0
        started = True
        block.append(point)
    return Series(block)


def window_of(
    series: Series, threshold_w: float, step: timedelta
) -> tuple[datetime | None, datetime | None]:
    """First and last moment the surplus is at or above ``threshold_w``.

    Reporting only -- this is what the hub's start/end sensors publish, and it
    deliberately spans any dips in between rather than returning the first
    contiguous run. "Surplus from 10:15 to 15:30" is the useful answer for
    deciding whether to arm something; the gaps inside it are the optimiser's
    problem, not the user's. Restricted to ``current_block`` first, though --
    without that, "spans any dips" also spans the night, and the window
    reaches into tomorrow's forecast whenever it happens to qualify too.
    """
    above = [point.time for point in current_block(series, step) if point.value >= threshold_w]
    if not above:
        return None, None
    return above[0], above[-1]


def total_energy_wh(series: Series, step: timedelta) -> float:
    """Surplus energy across the whole series."""
    return sum(point.value for point in series) * step.total_seconds() / 3600

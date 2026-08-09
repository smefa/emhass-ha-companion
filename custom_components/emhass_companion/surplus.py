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
import logging
import math

from .models import Plan, Point, Series

_LOGGER = logging.getLogger(__name__)

# Relative slack when testing whether a window's surplus covers the energy
# asked of it. Both sides are sums of the same floats in a different order, so
# an exact ``>=`` can miss the slot that genuinely closes the gap and hand back
# a window one step wider than it needs to be.
COVER_TOLERANCE = 1e-9

# How much of the block's surplus a start-as-early-as-possible run deliberately
# leaves on the table, so that the window it fits in is narrower than the block
# and the run has room to follow the surplus curve inside it.
#
# Without this the budget asks for *every* watt-hour the qualifying slots offer,
# and the only window that covers that is the whole block -- the switch then
# hands back exactly the window it was meant to narrow, whatever the load's
# shape. Giving up a few per cent is what buys the placement freedom back.
#
# Scaled as ``1 / run_steps`` because the margin has to cover the gap between a
# slot's forecast surplus and its real one, and a short run averages fewer slots
# so a single bad one moves it further: a four-step run gives up a quarter, a
# twenty-step run a twentieth. Clamped at both ends -- below MIN the margin
# stops opening any window at all, above MAX the load is giving up more sun than
# starting early is worth.
MODULATION_MARGIN_MIN = 0.05
MODULATION_MARGIN_MAX = 0.25

# One timestep past the slot that closes the cover, and deliberately only one.
#
# The temptation is to scale this with the run -- "a longer run needs more room
# to shape itself in" -- but that double-counts the margin above and loses to it.
# The margin is *already* the shaping room: once the ask is a few per cent below
# what the window supplies, that few per cent is slack distributed across every
# slot in the window, which is exactly the freedom the solver needs to track the
# surplus curve. Measured against a real 24-slot block, a proportional slack of a
# quarter of the run added back four timesteps while a 6% margin removed three --
# so the two together narrowed nothing at all, and the switch stayed the no-op it
# had always been.
#
# What is left for this constant to do is cover the discreteness of the cover
# test itself: the covering slot is the first whose *cumulative* surplus clears
# the target, so the run may need part of the next one. Hence one step.
MODULATION_SLACK_STEPS = 1


def modulation_margin(run_steps: int) -> float:
    """Fraction of the block's surplus a start-asap run leaves unclaimed."""
    if run_steps <= 0:
        return MODULATION_MARGIN_MAX
    return min(MODULATION_MARGIN_MAX, max(MODULATION_MARGIN_MIN, 1.0 / run_steps))


def _asap_window_last(
    hours: float,
    nominal_w: float,
    window_first: datetime,
    window_last: datetime,
    deliverable: Sequence[tuple[datetime, float]],
    step: timedelta,
    step_hours: float,
) -> datetime:
    """The last timestep a start-asap run of ``hours`` needs its window to reach.

    Factored out of ``allocate`` because the answer has to be computed twice --
    once for the full ask and once for the ask minus the modulation margin -- so
    that the margin is only charged when it genuinely narrows the window. Never
    returns anything later than the ``window_last`` handed in: this may only
    ever remove window, never invent it.
    """
    run_steps = math.ceil(hours / step_hours)
    # Whole timesteps from the first qualifying one, inclusive: the tight span
    # rounds *up* to the run, never below it, so this can only remove placement
    # freedom and never make the window too narrow for the hours -- which EMHASS
    # answers by declaring the whole problem infeasible rather than just this
    # one load.
    tight_end = window_first + (run_steps - 1) * step

    # ...but the hours alone are the wrong measure of how much window the run
    # needs, because ``nominal_w`` is the block's *peak* and the front of the
    # block is its weakest end. EMHASS holds the energy total as an equality, so
    # a window clamped to the bare hours forces the load to hold peak power
    # through morning slots that never offered it, and the difference is
    # imported -- the exact "short, near-ceiling burst" the ceiling exists to
    # prevent, reintroduced by pinning where the run lands.
    #
    # Widening is what fixes that, not lowering the ceiling: the cost function is
    # imports at the buy price and exports at the sell price, so the marginal
    # cost of one more watt in a slot steps up sharply at the point the slot
    # stops exporting. That kink is convex, which is why the solver spreads a run
    # across slots and tracks the surplus curve rather than running flat out in
    # the fewest slots -- but it can only spread into window it has.
    #
    # So the window has to reach far enough that the surplus inside it actually
    # covers the energy being asked for. Stopping just past the first slot where
    # it does keeps this the earliest window that can deliver the budget without
    # importing, which is what the switch means. The one
    # ``MODULATION_SLACK_STEPS`` past it covers the discreteness of the test
    # rather than buying shaping room -- the margin is what buys that. On a flat
    # block the cover is reached at exactly ``run_steps`` and that single step is
    # the only widening that happens.
    energy_wh = hours * nominal_w
    covered_wh = 0.0
    for when, deliverable_wh in deliverable:
        covered_wh += deliverable_wh
        if covered_wh >= energy_wh * (1 - COVER_TOLERANCE):
            return min(window_last, max(tight_end, when + MODULATION_SLACK_STEPS * step))

    # Never covered means the block cannot deliver the target however wide the
    # window gets -- ``credit_wh`` is uncapped per slot while the draws are not,
    # so an ample block and a load whose configured ceiling sits below the
    # block's peak lands here. The full block is then the most that could help.
    return window_last


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


def seam_carry(previous: Series, incoming: Series) -> Series:
    """The outgoing plan's last row before ``incoming`` picks up.

    EMHASS starts its horizon at the next timestep boundary after launch, so
    at the moment a plan is published "now" sits *before* row zero and every
    surplus sensor reading ``value_at(now)`` gets ``None`` until the horizon
    catches up. That is a gap at every single run, not an edge case, and
    publishing a zero through it would be worse than publishing nothing: a
    fabricated "no spare sun" is indistinguishable from a real one, so
    anything gated on it short-cycles once per optimisation. The outgoing
    plan did cover that stretch, so its last row is carried across instead --
    still a real forecast, just from the previous run.

    Exactly one row, and only one predating the new horizon, so the carry can
    neither accumulate across runs nor outlive the seam it bridges: the moment
    "now" passes ``incoming.start``, hold-last semantics stop consulting it.
    Nothing here reaches the budgets -- those come off the plan itself in
    ``allocate`` -- and an absent plan on either side carries nothing, because
    "we have no plan" must keep reading unknown.
    """
    if not previous or not incoming:
        return Series.empty()
    return Series([point for point in previous if point.time < incoming.start][-1:])


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

    start_asap: bool = False
    """Whether this load wants the *front* of the block rather than anywhere in
    it. See ``allocate``: it narrows the window to the earliest stretch that
    can actually deliver the budget, leaving EMHASS as little room as possible
    to place the run later. Changes only the window -- never the hours, the
    energy or which slots were credited."""


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
    *energy* a qualifying slot can still give a surplus load, and nothing
    else: it decides neither the block, nor the window, nor whether a slot
    qualifies in the first place. Those are read off the sun -- ``gross``, and
    what the higher-priority surplus loads have left of it. Otherwise the
    battery would be back to gating *when* a surplus load may run, not just
    how much of a claimed slot is left over for it -- the bug
    ``surplus_series`` exists to avoid (see surplus_loads.md).
    """
    step_hours = step.total_seconds() / 3600
    if step_hours <= 0:
        return {}

    reserved_at = {point.time: point.value for point in reserved} if reserved else {}
    # [time, gross, available, reserved]: three claims on one slot, kept apart
    # on purpose rather than collapsed into a single net figure.
    #
    # ``gross`` is the spare sun itself, and decides block membership and the
    # window. ``available`` is what is left of it once the higher-priority
    # surplus loads have taken their draw -- mutated in place as each spec is
    # served, because another load running really does empty the slot -- and
    # decides whether a slot qualifies for this one. ``reserved`` is the
    # battery's claim on the same slot, and caps only how much energy a
    # qualifying slot can credit.
    #
    # Keeping the battery out of the qualification test is what stops the
    # budget oscillating between cycles. The reservation is read from the
    # previous plan, and that plan charged the battery on precisely the PV this
    # load did not take -- so once the load is in the plan, gross minus the
    # reservation collapses to the load's own draw, which by construction never
    # clears its own ``run_floor_w + headroom_w``. Gating on that figure
    # switched the load off every time it switched on: budget, run, collapse to
    # zero, battery takes the block back, full budget again, one MPC cycle
    # apart, forever.
    remaining = [
        [point.time, point.value, point.value, reserved_at.get(point.time, 0.0)] for point in series
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
            when, gross, _, _ = entry
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
        # The ceiling itself is sized from what a qualifying slot can actually
        # hand over -- after the higher-priority loads and after the battery's
        # claim -- because that is the real limit on what this load can draw,
        # however strong the sun in that slot was.
        nominal_w = spec.nominal_w
        if not spec.semi_continuous:
            qualifying_peak = max(
                (
                    max(0.0, available - reserve)
                    for _, _, available, reserve in block
                    if available >= threshold
                ),
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
        # What each qualifying slot can hand *this* load, in order: the draw
        # already computed below, which is what the slot can still deliver
        # clipped to the load's ceiling. Kept separately from ``credit_wh``
        # because that is the aggregate credit, deliberately uncapped by what
        # any one slot can deliver (see above) -- the start-asap rule needs the
        # physical figure, and needs it before ``entry[2]`` is debited.
        deliverable: list[tuple[datetime, float]] = []

        for entry in block:
            when, _, available, reserve = entry
            # The sun test, against what the higher-priority loads left behind.
            # ``headroom_w`` is margin against the *forecast* being wrong, so it
            # belongs on the slot itself and not on whatever survives the
            # battery's claim below -- measuring a forecast margin against a
            # residual the load's own draw is already inside of is what made
            # this oscillate (see ``remaining`` above).
            if available < threshold:
                continue

            # ...and the delivery test, against what the battery leaves. A slot
            # the battery has taken entirely credits nothing and counts for
            # nothing -- but it has disqualified neither the slots around it nor
            # the window, and it is not claimed on this load's behalf either.
            usable = max(0.0, available - reserve)
            if usable <= 0:
                continue

            draw = nominal_w if spec.semi_continuous else min(usable, nominal_w)
            if draw <= 0:
                continue

            credit_wh += usable * step_hours
            steps += 1
            deliverable.append((when, draw * step_hours))
            entry[2] = max(0.0, available - draw)

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
        hours_from_block = credit_wh / nominal_w if nominal_w > 0 else 0.0
        hours_from_block = min(hours_from_block, span_hours)

        # A hard cap on delivered energy, not on the aggregate credit -- sizing
        # it against nominal_w is exact for a semi-continuous load (it draws
        # exactly that whenever it runs) and the same already-accepted
        # approximation the rest of this budget makes for one that modulates.
        cap_hours = (
            spec.max_energy_wh / nominal_w
            if spec.max_energy_wh is not None and nominal_w > 0
            else None
        )
        hours = hours_from_block if cap_hours is None else min(hours_from_block, cap_hours)
        energy_wh = hours * nominal_w

        # Below one timestep, there is no run to ask for.
        #
        # EMHASS can only place a load in whole timesteps, and
        # ``payload.operating_timesteps`` floors any non-zero request at one --
        # "run this for six minutes" turning into "never run" being the worse
        # answer there. So a budget under a single step's worth of energy does
        # not buy a shorter run: it buys a *full* step at nominal power, of
        # which only the fraction below is backed by surplus and the rest is
        # imported. A load asking for 100 Wh gets a 212 Wh step.
        #
        # Which is precisely what a surplus load exists not to do, and the
        # choice here is only ever between one step of partial import and
        # nothing at all.
        #
        # Only for a budget the *block* sized, though. When ``max_energy_wh``
        # is what binds, the number came from the user -- "put 300 Wh into the
        # car" -- and answering a small ask with nothing at all is not this
        # function's call to make; payload.operating_timesteps already warns
        # about the rounding. Tested the same way the modulation margin tests
        # it, so a cap landing just under a step behaves the same in both.
        #
        # The window is deliberately left set, exactly as for a block the
        # battery reserved outright -- the sun really was up, there simply was
        # not enough of it left over to be worth a timestep. ``is_empty`` reads
        # off ``hours``, so the load is parked either way.
        cap_binds = cap_hours is not None and cap_hours < hours_from_block
        if not cap_binds and 0 < hours < step_hours:
            hours = 0.0
            energy_wh = 0.0

        # "Start as early as possible" narrows the *window* and nothing else.
        # The budget above is already "however much the surplus supports"; all
        # that is left to decide is where in the block those hours land, and
        # every gap between ``hours`` and ``span_hours`` is a gap EMHASS is
        # free to place them in. On flat prices it has no reason to prefer
        # early, so which end of the block a thin day's run lands on comes down
        # to solver tie-break.
        #
        # Clamping the window to as many timesteps as the run needs leaves only
        # one place they fit. Applied after every cap above, because those are
        # what decide how much gap there is to close -- an energy-capped car on
        # an abundant day is the clearest case, asking for two hours inside an
        # eight-hour block.
        #
        # Of those caps only ``max_energy_wh`` can open a gap by itself.
        # ``credit_wh`` sums ``net`` over the same slots whose
        # ``min(net, nominal_w)`` the deliverables are, so it is always the
        # larger of the two; and ``span_hours`` spans at least as many steps as
        # there are qualifying slots, so ``span_hours * nominal_w`` is too. Which
        # is why an uncapped run gives up ``modulation_margin`` of the block
        # first: without it the ask *is* the block, the one window covering the
        # block is the block, and this clamp would hand back precisely the window
        # it exists to narrow. That slice of surplus is the placement freedom.
        if spec.start_asap and window_last is not None and hours > 0:
            plain_last = _asap_window_last(
                hours, nominal_w, window_first, window_last, deliverable, step, step_hours
            )

            # The margin is only ever worth taking out of a *block-derived* ask.
            # When the cap is what is binding, the ask already sits below what
            # the block delivers -- which is the whole gap the margin exists to
            # manufacture -- and a number the user typed must be delivered in
            # full, not 6% short of it. Testing ``cap_hours < hours_from_block``
            # rather than derating first and clamping after matters for a cap
            # that lands *inside* the margin: 9.5 h of cap against a 10 h block
            # would otherwise come out as 7.5 h.
            margin = (
                0.0
                if cap_hours is not None and cap_hours < hours_from_block
                else modulation_margin(math.ceil(hours_from_block / step_hours))
            )

            # ...and only worth taking at all if it actually buys a shorter
            # window. Whether it does is a property of the block's *shape*: a
            # margin removes energy from the ask, but if the tail slots it stops
            # needing are the weakest ones, the covering slot barely moves and
            # ``MODULATION_SLACK_STEPS`` puts it straight back. Measured on a real
            # 13-slot block, a 10% margin gave up 1.36 kWh for exactly zero
            # timesteps. Asking "is there placement freedom in principle" --
            # comparing the run against the qualifying slot count -- does not
            # catch that; only computing both windows does.
            derated_hours = hours_from_block * (1.0 - margin)
            if margin > 0.0 and derated_hours > 0.0:
                derated_last = _asap_window_last(
                    derated_hours,
                    nominal_w,
                    window_first,
                    window_last,
                    deliverable,
                    step,
                    step_hours,
                )
                if derated_last < plain_last:
                    hours = derated_hours
                    energy_wh = hours * nominal_w
                    window_last = derated_last
                else:
                    margin = 0.0
                    window_last = plain_last
            else:
                margin = 0.0
                window_last = plain_last

            _LOGGER.debug(
                "%s: start-as-early-as-possible asking for %.0f Wh at %.0f W "
                "(%.0f%% of the block's %.0f Wh, %s) in %d timesteps, %d of them run",
                spec.subentry_id,
                energy_wh,
                nominal_w,
                100 * (energy_wh / credit_wh) if credit_wh > 0 else 100.0,
                credit_wh,
                f"keeping {margin * 100:.0f}% to modulate with"
                if margin > 0
                else "no margin -- it would not have narrowed the window",
                int((window_last - window_first) / step) + 1,
                math.ceil(hours / step_hours),
            )

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

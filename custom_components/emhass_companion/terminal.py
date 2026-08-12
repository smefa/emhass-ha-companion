"""Decide the battery's end-of-horizon SOC target.

EMHASS pins the battery's end-of-horizon SOC to whatever ``soc_final`` a
request carries (softly from 0.18: deviation is penalised far above the
dearest import slot, so the target is honoured whenever physics allows). Its
API has no way to say "you decide" -- a terminal condition is always the
caller's job -- so this module is where the companion decides what stored
energy is worth having when the known world ends.

Optimized mode is governed by one rule (:func:`_shadow_plan`, Test 7):

    **Plan the whole window that can be seen -- the horizon and the 24 h past
    it -- and take the SOC that plan holds when the horizon runs out.**

The pin exists only because EMHASS's horizon stops after 24 h, so it is the
one channel through which knowledge of hour 25 reaches hour 24. Given that,
the honest answer is not a rule about what stored energy ought to be worth: it
is the SOC an optimisation over everything visible actually passes through.
Everything a heuristic here would have asserted -- carry the night, buy the
trough, leave room for the sun -- falls out of the arithmetic where it is true
and does not where it is not.

What a solver cannot do is decline to believe its inputs, and past the
day-ahead close half the window is yesterday's curve copied forward. So a
guessed price keeps its *level* and loses its *shape*
(:func:`_flatten_guesses`): the plan still knows that energy will be needed and
will cost roughly what it costs today, and has nothing left to arbitrage. That
is this module's oldest discipline in a new form -- see :func:`_sale_credit`,
which refuses the same trade by refusing to price it.

The rules it replaced are still computed on every run, and the asymmetry they
were built on still holds and still shows up in the tie-breaks: arriving short
means importing across a night at ``spot x multiplier + adder`` while the cheap
way to fill a battery, sunlight, is hours away; arriving heavy costs at most a
spread. Where the plan is indifferent, it arrives heavy.

Everything here is pure computation on :class:`~.models.Series`; fetching them
is the coordinator's job.

Candidates
----------

There is no ground truth for a terminal-value heuristic by inspection, so this
module computes *several* and reports them all. :data:`CANDIDATES` is evaluated
in full on every run against one shared view of the world (:class:`_Tail`);
:data:`ACTIVE_CANDIDATE` names the one whose number actually reaches EMHASS.
The rest ride along in the End SOC sensor's ``tests`` attribute, where recorded
history says which one should have been driving.

Adding another is three lines: write ``_my_idea(tail) -> EndSocDecision``, add
``_Candidate("my_idea", "Test 9 -- my idea", _my_idea)`` to :data:`CANDIDATES`,
and it shows up in the sensor on the next run. Point :data:`ACTIVE_CANDIDATE`
at it to promote it.

A candidate's *key* is what recorded history is keyed on, so editing one in
place silently rebases that history: improvements go in as a new candidate
rather than as a change to an old one, and a retired number is never reused.
Tests 1, 2 and 5 were retired on 2026-08-12 after a backtest over 685 real days
of this house (8106 decisions, priced against a perfect-foresight dispatch)
found all three dominated by Test 4 in every season -- Test 1 returned the same
number as Test 4 on 79% of runs and a worse one on the rest, Test 2 emptied the
battery before every sunny day exactly as its own docstring feared, and both of
Test 5's ideas cost money. Test 3 stays because it is the pre-companion
behaviour and the yardstick everything else is measured against; it is also,
still, the best *heuristic* here in winter.

See docs/end_soc_plan.md for the full design, the live A/B numbers that
motivated it, and the backtest that settled the slate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from math import ceil, inf
from typing import Any

from homeassistant.util import dt as dt_util

from .const import END_SOC_FIXED_50, END_SOC_OPTIMIZED, END_SOC_SAME_AS_START
from .models import BatteryConfig, GridConfig, Series

# How far past the horizon the heuristic looks. One day always contains the
# next night's price trough and the next solar noon, and keeps the surplus
# scan inside a single day cycle.
LOOKAHEAD: timedelta = timedelta(hours=24)

# A price at or below this percentile of the whole known buy-price series
# counts as "cheap enough to refill from the grid".
CHEAP_PERCENTILE: float = 0.25

# A PV surplus block only counts as a refill opportunity once its cumulative
# energy reaches this fraction of capacity -- a five-minute glint of sun
# through the clouds refills nothing.
SURPLUS_MATERIAL_FRACTION: float = 0.10

# A surplus block survives this much continuous deficit before it is called
# over. Passing clouds cut a solar day into fragments that are one refill
# opportunity, not several.
SURPLUS_BLOCK_GAP: timedelta = timedelta(hours=2)

# Weight given to PV inferred from the previous day's shape rather than
# forecast (see _sample_tail). Solar has huge day-to-day variance, so a
# guessed sunny day may only free part of the battery: half is enough to stop
# hoarding through a sunny spell without stranding the house when the guess is
# wrong. Deliberately one-sided -- proxied PV may lower the ceiling, never
# shorten the bridge, so sun that never came costs a partial charge rather
# than an empty battery at dawn.
PV_PROXY_CONFIDENCE: float = 0.5

# A recomputed target within this band of the previous one keeps the previous
# value: the target is a pin on the solver's endpoint, and a pin that jitters
# run-to-run makes consecutive plans thrash.
HYSTERESIS_BAND: float = 0.02

REPLENISH_SOLAR: str = "solar"
REPLENISH_CHEAP_GRID: str = "cheap_grid"
REPLENISH_NONE: str = "none"

PRICE_TAIL_KNOWN: str = "known"
PRICE_TAIL_PROXY: str = "assumed from today"
PV_TAIL_KNOWN: str = "known"
PV_TAIL_PROXY: str = "assumed from the previous day"
PV_TAIL_ZERO: str = "assumed zero"

# What decided the number, for the sensor's clamped_by attribute.
BOUND_NIGHT_COVER: str = "night_cover"
BOUND_SALE: str = "profitable_sale"
BOUND_PRICED_LIFT: str = "priced_lift"
BOUND_RESERVE: str = "reserve"
BOUND_RANGE: str = "range"
BOUND_HYSTERESIS: str = "hysteresis"

# Why the cover was carried whole rather than sold down, for the sensor.
SALE_NO_SELL_PRICE: str = "no sell price available"
SALE_PRICES_UNPUBLISHED: str = "overnight prices not published yet"
SALE_NO_MARGIN: str = "no spread worth the round trip"

# Where the load series came from, for the sensor -- this module cannot tell
# on its own, since a borrowed series looks like any other. See
# coordinator.EmhassCoordinator._load_for_terminal.
LOAD_SOURCE_PROFILE: str = "own forecast"
LOAD_SOURCE_LAST_PLAN: str = "borrowed from last plan"

TEST_DAILY_RATIO: str = "daily_ratio"
TEST_NIGHT_COVER: str = "night_cover"
TEST_PRICED_COVER: str = "priced_cover"
TEST_SHADOW_PLAN: str = "shadow_plan"

# The plant-scale constant from the original Jinja template this module's
# third candidate reproduces: the daily yield that counts as a *strong* day.
# Hard-coded because the candidate is a shadow calculation -- promoting it to
# ACTIVE_CANDIDATE means turning this into a setting first.
TEMPLATE_REFERENCE_YIELD_KWH: float = 40.0

# One day, the lag every proxy in this module reaches back by and the length of
# the lookahead. Named because two candidates read that lag *backwards* to
# recover what the horizon looked like.
DAY: timedelta = timedelta(hours=24)

# --- the shadow plan's solver ------------------------------------------------

# SOC levels the shadow plan discretises the usable span into. 33 puts a level
# every ~2.5% (550 Wh on a 22 kWh battery), finer than a soft pin is ever
# honoured to, and keeps the whole solve inside a tenth of a second in pure
# Python on a Raspberry Pi.
SOC_LEVELS: int = 33

# Currency band inside which two pins are the same answer, per kWh of battery.
PIN_TOLERANCE: float = 0.05

# How far a reconstructed import tariff may miss the tail's own prices before
# it stops being believed, as a fraction of the mean price.
FIT_TOLERANCE: float = 0.01

PRICE_MODEL_TARIFF: str = "tariff recovered from sell prices"
PRICE_MODEL_UNTRUSTED: str = "tariff not recoverable; horizon prices treated as guessed"


@dataclass(slots=True)
class EndSocDecision:
    """A chosen end-of-horizon SOC and the explanation it needs to be trusted."""

    soc: float
    """Final fraction in [0, 1], all clamps applied."""
    reason: str
    """One human sentence for the sensor state's tooltip-level story."""
    details: dict[str, Any] = field(default_factory=dict)
    """Structured attributes for the End SOC target sensor."""


@dataclass(slots=True)
class _Sample:
    """One timestep of the world past the horizon."""

    when: datetime
    pv_w: float
    """Effective PV: forecast where forecast, else the discounted proxy, else 0."""
    pv_proxied: bool
    """True when ``pv_w`` is the previous day's shape rather than a forecast."""
    load_w: float
    price: float | None
    price_proxied: bool = False
    """True when ``price`` is yesterday's curve rather than a published one.
    Per-sample, not per-tail: at 18:00 the first half of the coming night is
    published and the second half is not, and a rule that may only act on
    published prices has to be able to tell those two halves apart."""
    sell: float | None = None


@dataclass(slots=True)
class _Block:
    """A run of timesteps where PV exceeds load."""

    start: datetime
    energy_wh: float
    proxied: bool


@dataclass(slots=True)
class _Tail:
    """The world past the horizon, sampled once and shared by every candidate."""

    battery: BatteryConfig
    grid: GridConfig
    soc_init: float
    now: datetime
    horizon_end: datetime
    step: timedelta
    samples: list[_Sample]
    blocks: list[_Block]
    cheap_threshold: float
    material_wh: float
    pv: Series
    sell: Series
    price_proxied: bool
    pv_proxied: bool
    pv_unknown: bool
    load_source: str

    @property
    def step_hours(self) -> float:
        return self.step.total_seconds() / 3600

    @property
    def reserve(self) -> float:
        """The user's comfort floor -- ``soc_target``, in its natural meaning."""
        return self.battery.soc_target

    def clamp(self, soc: float) -> float:
        return min(max(soc, self.battery.soc_min), self.battery.soc_max)

    def last_block(self, *, proxied: bool | None = None) -> _Block | None:
        """The last surplus block big enough to count as a refill.

        Where the backward walk in :func:`_required_soc` stops. Past the final
        sun the lookahead can see, every remaining hour is darkness with no
        visible end -- an artefact of the window, not a real night, and one
        that would otherwise propagate back and demand cover for it.
        """
        return next(
            (
                block
                for block in reversed(self.blocks)
                if block.energy_wh >= self.material_wh
                and (proxied is None or block.proxied is proxied)
            ),
            None,
        )

    def first_cheap(self) -> datetime | None:
        for sample in self.samples:
            if sample.price is not None and sample.price <= self.cheap_threshold:
                return sample.when
        return None

    def deficit_until(self, until: datetime | None) -> float:
        """Energy the battery must supply before ``until``, in Wh.

        Proxied PV counts as darkness: this is the number that keeps the
        battery from running out, and it is not shortened on a guess about
        the weather.
        """
        total = 0.0
        for sample in self.samples:
            if until is not None and sample.when >= until:
                break
            pv_w = 0.0 if sample.pv_proxied else sample.pv_w
            total += max(0.0, sample.load_w - pv_w) * self.step_hours
        return total / (self.battery.discharge_efficiency or 1.0)


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One way of choosing the end SOC, run in parallel with the others."""

    key: str
    label: str
    compute: Callable[[_Tail], EndSocDecision]


def decide_end_soc(
    *,
    mode: str,
    soc_init: float,
    battery: BatteryConfig,
    now: datetime,
    horizon_end: datetime,
    step: timedelta,
    pv: Series | None,
    load: Series | None,
    buy_price: Series | None,
    sell_price: Series | None = None,
    grid: GridConfig | None = None,
    load_source: str = LOAD_SOURCE_PROFILE,
    previous: EndSocDecision | None = None,
) -> EndSocDecision:
    """Choose ``soc_final`` for one optimisation request.

    ``sell_price`` and ``grid`` are optional because only the priced rules use
    them: without a sell curve nothing can be sold, which the decision reports
    rather than assumes, and the grid limits only cap how fast energy could
    leave. Both absent degrades to carrying the whole cover, the safe side.

    ``previous`` is the last decision (any mode), kept by the coordinator in
    memory only -- hysteresis needs no persistence, a restart just computes
    fresh.

    ``load_source`` describes where ``load`` came from -- this module has no
    way to tell on its own, since a borrowed series looks like any other.
    Recorded verbatim into the Optimized decision's ``details["load_tail"]``
    for the sensor; callers that always supply a real forecast can ignore it.
    """
    if mode == END_SOC_FIXED_50:
        target = battery.soc_target
        soc = min(max(target, battery.soc_min), battery.soc_max)
        details: dict[str, Any] = {"mode": mode}
        if soc != target:
            details["clamped_by"] = BOUND_RANGE
        return EndSocDecision(
            soc=soc,
            reason=f"Fixed at {target:.0%} by the End SOC setting.",
            details=details,
        )

    if mode != END_SOC_OPTIMIZED:
        # same_as_start, and the safe answer to a mode value from the future.
        return EndSocDecision(
            soc=soc_init,
            reason="Pinned to the start SOC by the End SOC setting.",
            details={"mode": END_SOC_SAME_AS_START},
        )

    missing = [
        name for name, series in (("load forecast", load), ("buy price", buy_price)) if not series
    ]
    if missing or battery.capacity_wh <= 0:
        if battery.capacity_wh <= 0:
            missing.append("battery capacity")
        return EndSocDecision(
            soc=soc_init,
            reason=(
                "Optimized end SOC needs a load forecast, a buy price and a "
                f"battery capacity; missing: {', '.join(missing)}. Falling "
                "back to the start SOC."
            ),
            details={"mode": mode, "fallback": ", ".join(missing)},
        )

    tail = _build_tail(
        soc_init=soc_init,
        battery=battery,
        now=now,
        horizon_end=horizon_end,
        step=step,
        pv=pv if pv else Series.empty(),
        load=load,
        buy_price=buy_price,
        sell_price=sell_price if sell_price else Series.empty(),
        grid=grid if grid is not None else GridConfig(),
        load_source=load_source,
    )

    results = {candidate.key: candidate.compute(tail) for candidate in CANDIDATES}
    decision = results[ACTIVE_CANDIDATE]
    decision.details.update(
        mode=END_SOC_OPTIMIZED,
        reserve=round(tail.reserve, 4),
        price_tail=PRICE_TAIL_PROXY if tail.price_proxied else PRICE_TAIL_KNOWN,
        pv_tail=_pv_tail(tail),
        load_tail=load_source,
        active_test=ACTIVE_CANDIDATE,
        # Every candidate's answer for the same moment. The active one is
        # repeated here on purpose, so a recorded history of this attribute
        # compares like with like -- and shows the pre-hysteresis value the
        # candidate actually produced.
        tests={
            candidate.key: {
                "label": candidate.label,
                "soc": results[candidate.key].soc,
                "reason": results[candidate.key].reason,
            }
            for candidate in CANDIDATES
        },
    )
    _annotate_reach(decision, tail)

    if (
        previous is not None
        and previous.details.get("mode") == END_SOC_OPTIMIZED
        and "fallback" not in previous.details
        and decision.soc != previous.soc
        and abs(decision.soc - previous.soc) <= HYSTERESIS_BAND
    ):
        decision.details["clamped_by"] = BOUND_HYSTERESIS
        decision.details["raw_target"] = round(decision.soc, 4)
        decision.soc = previous.soc
    return decision


# --- candidates --------------------------------------------------------------


def _daily_ratio(tail: _Tail) -> EndSocDecision:
    """Test 3: the Jinja template this feature replaced, ported verbatim.

    Drifts the live SOC by how much sunnier tomorrow looks than today, then
    floors it on a sliding scale -- little sun tomorrow, high floor; a lot of
    sun, low floor. Included because it ran this house for a year and is the
    yardstick the others have to beat. Its two weaknesses are visible in the
    arithmetic: it is anchored on a drifting live SOC (the ratchet), and it
    knows nothing about price or load at all.

    Deviates from the original in one place: the pre-10:00 branch read
    yesterday's saved total for "today", which the companion does not keep, so
    the daily totals always come from the PV series as it stands now.
    """
    day = timedelta(days=1)
    local_now = dt_util.as_local(tail.now)
    today_start = dt_util.start_of_local_day(local_now)
    tomorrow_start = dt_util.start_of_local_day(local_now + day)
    day_after = dt_util.start_of_local_day(local_now + 2 * day)

    today_kwh = _energy_kwh(tail.pv, today_start, tomorrow_start)
    if tail.pv and tail.pv.covers(tomorrow_start + timedelta(hours=18)):
        tomorrow_kwh = _energy_kwh(tail.pv, tomorrow_start, day_after)
        coverage = ""
    else:
        # No forecast for tomorrow: the original template would have compared
        # today with itself, which is a zero drift.
        tomorrow_kwh = today_kwh
        coverage = " (tomorrow not forecast; assumed same as today)"

    reference = TEMPLATE_REFERENCE_YIELD_KWH
    raw = tail.soc_init + ((tomorrow_kwh - today_kwh) / reference) * 0.4
    if tomorrow_kwh >= reference * 1.125:
        # A monster day: never plan below where the battery already sits.
        raw = max(raw, tail.soc_init)
    floor = 0.5 - min(tomorrow_kwh / reference, 1.0) * 0.4
    soc = tail.clamp(min(max(raw, floor), 0.9))

    return EndSocDecision(
        soc=round(soc, 4),
        reason=(
            f"Template drift: {tomorrow_kwh:.0f} kWh forecast tomorrow against "
            f"{today_kwh:.0f} kWh today, on a {floor:.0%} sliding floor.{coverage}"
        ),
        details={
            "today_kwh": round(today_kwh, 1),
            "tomorrow_kwh": round(tomorrow_kwh, 1),
            "sliding_floor": round(floor, 4),
        },
    )


def _night_cover(tail: _Tail) -> EndSocDecision:
    """Test 4: carry what the night needs; sell only what pays to buy back.

    The shipping rule. Two halves, in strict order.

    The floor is :func:`_required_soc`, the lowest SOC at the pin that never
    puts the battery under its reserve for the whole lookahead. It replaces
    Test 1's "reserve plus the deficit until the next refill" with a walk that
    also survives a pin landing *inside* a sunny morning: Test 1 sees the sun
    at the pin, calls the bridge zero and hands back the bare reserve, which
    on a weak day sells off the morning's own charge before the night that
    follows it. The walk carries that night's shortfall instead.

    The ceiling is a *trade*, not a preference. Nothing lowers the floor except
    :func:`_sale_credit` finding hours before the pin where selling clears the
    cost of buying the same energy back before the battery would hit its
    reserve. On this house's tariff -- buy at 1.25x spot + 0.80, sell at spot,
    0.10/kWh of wear -- a 0.20 night needs a 1.15 evening to clear, so the
    exception sleeps through the summer and wakes on winter peaks. That is the
    intended shape: emptying a battery into a night is a cost, and it should
    have to justify itself against the meter rather than against a story about
    tomorrow's sun.

    One grid refill *is* accepted, and only one: when the whole lookahead holds
    no forecast surplus at all, the requirement stops at the cheapest hour
    instead. The rule refuses to buy at night in order to make room for sun,
    and a week of December overcast is precisely the case where there is no sun
    to make room for -- without this the cover would run the full 24 h and pin
    every winter run at ``soc_max``, which is the hoarding the whole feature
    exists to end. Sun still outranks price everywhere it exists.
    """
    capacity = tail.battery.capacity_wh
    reset_from, kind = _cover_horizon(tail)
    cover = _required_soc(tail, reset_from=reset_from)
    sale = _sale_credit(tail, cover.binding_at, cover.energy_wh)

    target = cover.soc - sale.energy_wh / capacity
    bound = BOUND_SALE if sale.energy_wh > 0 else BOUND_NIGHT_COVER
    if target < tail.reserve:
        target, bound = tail.reserve, BOUND_RESERVE
    soc = tail.clamp(target)
    if soc != target or (bound == BOUND_NIGHT_COVER and cover.raw_soc > cover.soc + 1e-9):
        # Either the range clamped the answer, or the walk did: a night longer
        # than the battery is a bound that bit, and a bound that bites in
        # silence is how a heuristic gets believed for the wrong reason.
        bound = BOUND_RANGE

    details: dict[str, Any] = {
        "cover_energy_wh": round(cover.energy_wh),
        "cover_target": round(cover.soc, 4),
        "replenishment_kind": kind,
    }
    if cover.binding_at is not None:
        details["cover_until"] = cover.binding_at.isoformat()
    if sale.energy_wh > 0:
        details["sale_energy_wh"] = round(sale.energy_wh)
        details["sale_margin"] = round(sale.margin, 4)
        details["sale_sell_price"] = round(sale.sell_price, 4)
        details["sale_buy_price"] = round(sale.buy_price, 4)
    elif sale.blocked:
        details["sale_blocked"] = sale.blocked
    if bound != BOUND_NIGHT_COVER:
        details["clamped_by"] = bound
        details["raw_target"] = round(max(cover.soc, cover.raw_soc), 4)

    return EndSocDecision(
        soc=round(soc, 4),
        reason=_cover_reason(tail=tail, soc=soc, cover=cover, sale=sale, kind=kind),
        details=details,
    )


def _priced_cover(tail: _Tail) -> EndSocDecision:
    """Test 6: buy the tail's expensive hours forward, one kWh at a time.

    Test 4 answers only *how much energy does the house physically need past
    the horizon*, and its one price mechanism (:func:`_sale_credit`) can only
    ever subtract from that answer. So it can never arrive at the pin heavier
    than the house strictly needs -- which is the trade a winter offers: a spot
    curve that runs 0.2 to 4.5 SEK/kWh inside one week, no sun to speak of, and
    a battery that can carry today's trough across tomorrow's peaks.

    This makes that an explicit merit order rather than a preference. Two
    staircases are built and cleared against each other:

    *Demand* is the tail's own deficit, hour by hour, priced by what a carried
    kWh would actually save there -- the import it displaces, but never more
    than the cheapest way the *tail itself* could have got the same kWh, which
    is buying at the lowest tail price seen so far. Sun and hours the house
    covers itself displace nothing and never enter the ladder.

    *Supply* is every way of having a kWh in the battery at the pin: a purchase
    in each published horizon hour, grossed up for charge losses and wear and
    rationed by what the charger and the connection could take in that slot,
    plus whatever already sits above the reserve at ``soc_init``, priced at the
    best thing that energy could otherwise have done inside the horizon.

    Clearing cheapest supply against dearest demand gives the kWh where value
    stops covering cost. Everything below that line is worth carrying.

    Three things the arithmetic has to get right:

    *The tariff is asymmetric.* Buy is ``1.25x spot + 0.80``, sell is bare spot,
    so every price stays on the side of the meter it belongs to.

    *Wear is not double counted.* A kWh leaves the battery exactly once whatever
    it is spent on, so discharge wear sits on both sides of every comparison
    against stored energy and cancels. Charge wear and charge losses fall only
    on the supply side, on the kWh actually bought.

    *Proxied prices may raise this rule, never lower it.* Used naively a proxied
    tail price would win a trade every day, because past the day-ahead close the
    "expensive tail hour" is a horizon hour reflected forward and the horizon's
    own trough is what would pay for it -- a curve arbitraged against itself.
    What kills that is not a rule about provenance but the alternative the same
    proxy supplies: if tomorrow looks like today then tomorrow has today's
    trough too, and buying *then* beats carrying. That is the cap the demand
    ladder applies, so a proxied hour can only earn a carry when it sits ahead
    of every trough the same proxy predicts.

    The floor is Test 4's own answer, and the backtest is why: the merit order
    standing alone, free to undercut the physical cover, scored 1.70 SEK of mean
    regret against 1.12 for the same ladder floored at Test 4. Lifting is where
    the idea pays; lowering is where Test 4 was already right.
    """
    battery = tail.battery
    capacity = battery.capacity_wh
    reserve = tail.reserve

    reset_from, kind = _cover_horizon(tail)
    cover = _required_soc(tail, reset_from=reset_from)
    sale = _sale_credit(tail, cover.binding_at, cover.energy_wh)
    floor_wh = max(0.0, cover.energy_wh - sale.energy_wh)

    horizon = _horizon_view(tail)
    # Sun resets the requirement outright, so demand stops where the cover's
    # own walk stops. A cheap grid hour does not: that is a price, and pricing
    # it against the horizon's own trough is the entire point of the ladder.
    demand = _demand_ladder(tail, stop=reset_from if kind == REPLENISH_SOLAR else None)
    supply = _supply_ladder(tail, horizon)
    ceiling_wh = max(0.0, (battery.soc_max - reserve) * capacity)
    priced_wh, marginal = _clear_ladders(demand, supply, ceiling_wh)

    carry_wh = max(floor_wh, priced_wh)
    if priced_wh > floor_wh + 1e-6:
        bound = BOUND_PRICED_LIFT
    elif sale.energy_wh > 0:
        bound = BOUND_SALE
    else:
        bound = BOUND_NIGHT_COVER

    target = reserve + carry_wh / capacity
    if target < reserve:
        target, bound = reserve, BOUND_RESERVE
    soc = tail.clamp(target)
    if soc != target:
        bound = BOUND_RANGE

    details: dict[str, Any] = {
        "cover_energy_wh": round(cover.energy_wh),
        "cover_target": round(cover.soc, 4),
        "priced_energy_wh": round(priced_wh),
        "carry_energy_wh": round(carry_wh),
        "replenishment_kind": kind,
        "demand_rungs": len(demand),
        "supply_rungs": len(supply),
    }
    if marginal > 0:
        details["marginal_cost"] = round(marginal, 4)
    if sale.energy_wh > 0:
        details["sale_energy_wh"] = round(sale.energy_wh)
    if bound != BOUND_PRICED_LIFT:
        details["clamped_by"] = bound
        details["raw_target"] = round(target, 4)

    return EndSocDecision(
        soc=round(soc, 4),
        reason=_priced_reason(tail, soc, carry_wh, bound),
        details=details,
    )


def _shadow_plan(tail: _Tail) -> EndSocDecision:
    """Test 7: stop guessing the terminal condition and solve for it.

    Every other candidate is a rule about what stored energy *ought* to be worth
    at the pin. This one asks the only question with an answer: if the companion
    optimised the whole window it can see -- the horizon plus the 24 h tail
    :func:`_build_tail` already samples -- what SOC would that plan be passing
    through at the horizon's end? That SOC is the terminal condition, derived
    rather than asserted, and it is exactly what the pin is for: EMHASS's
    horizon stops after 24 h, so the pin is the only channel through which
    knowledge of hour 25 reaches hour 24.

    The machinery is :func:`_solve_window`, a dynamic program over a coarse SOC
    grid: one backward pass from the far end of the tail to the pin, one forward
    pass from ``soc_init`` to the pin, and the pin is the argmin of their sum.

    Where it loses, it loses to *forecast error* rather than to a missing case
    in a rule -- which is why it also bounds what any heuristic here could
    achieve on the same information. Over 685 days of this house it beat Test 4
    by 0.26 SEK per decision overall and 0.23 in winter, on better mean, median
    and p90 in every season.

    **Cost.** 33 SOC levels over ~96 half-hour steps: about 22 ms per call on
    this Raspberry Pi 4, measured, against a whole ``decide_end_soc`` of 23 ms
    -- so the solver is essentially the entire cost of the module, once per
    optimisation cycle. Halve it by dropping :data:`SOC_LEVELS` if that ever
    stops being cheap enough.

    Within :data:`PIN_TOLERANCE` of the optimum the curve is flat and the choice
    is arbitrary, so the *highest* such pin is taken: arriving heavy costs the
    spread, arriving short costs a night at the evening tariff, and where the
    solver is indifferent the asymmetry the rest of this module is built on
    decides.
    """
    battery = tail.battery
    floor = max(battery.soc_min, tail.reserve)
    ceiling = battery.soc_max
    if battery.capacity_wh <= 0 or ceiling - floor < 1e-6 or not tail.samples:
        soc = tail.clamp(floor)
        return EndSocDecision(
            soc=round(soc, 4),
            reason=f"No room to plan in: holding {soc:.0%}.",
            details={"solver": "degenerate"},
        )

    slots, pin, notes = _plan_window(tail)
    residual = _residual_value(tail, slots)
    soc_grid, total = _solve_window(
        tail, slots, pin=pin, floor=floor, ceiling=ceiling, residual=residual
    )

    best = min(total)
    choice = max(index for index, value in enumerate(total) if value <= best + PIN_TOLERANCE)
    soc = tail.clamp(soc_grid[choice])

    at_reserve = _value_at(soc_grid, total, floor)
    at_full = _value_at(soc_grid, total, ceiling)
    hours = len(slots) * tail.step_hours
    pin_hours = pin * tail.step_hours
    details: dict[str, Any] = {
        "solver": "dynamic program",
        # Two spans, both worth stating: the plan runs over `window_hours`, and
        # the SOC it reports is the one it holds at `horizon_hours`. Every cost
        # below is for the whole window, not for the horizon.
        "window_hours": round(hours, 1),
        "horizon_hours": round(pin_hours, 1),
        "levels": len(soc_grid),
        "plan_cost": round(best, 2),
        "cost_at_reserve": round(at_reserve, 2),
        "cost_at_full": round(at_full, 2),
        "residual_value": round(residual, 4),
        "guessed_slots": sum(1 for slot in slots if slot.proxied),
        **notes,
    }
    return EndSocDecision(
        soc=round(soc, 4),
        reason=_plan_reason(
            hours=hours,
            pin_hours=pin_hours,
            soc=soc,
            best=best,
            at_reserve=at_reserve,
            at_full=at_full,
        ),
        details=details,
    )


CANDIDATES: tuple[_Candidate, ...] = (
    _Candidate(TEST_DAILY_RATIO, "Test 3 -- daily-yield template", _daily_ratio),
    _Candidate(TEST_NIGHT_COVER, "Test 4 -- night cover, sold only when it pays", _night_cover),
    _Candidate(TEST_PRICED_COVER, "Test 6 -- priced cover, merit order", _priced_cover),
    _Candidate(TEST_SHADOW_PLAN, "Test 7 -- shadow plan over the visible window", _shadow_plan),
)

ACTIVE_CANDIDATE: str = TEST_SHADOW_PLAN


# --- the merit order -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _HorizonSlot:
    """One in-horizon timestep, read back out of the tail's proxy lag."""

    when: datetime
    price: float | None
    """The published buy price, or ``None`` where the market had not opened."""
    load_w: float


@dataclass(frozen=True, slots=True)
class _Rung:
    """One step of a merit order: this many battery Wh at this price per kWh."""

    price: float
    energy_wh: float


def _horizon_view(tail: _Tail) -> list[_HorizonSlot]:
    """The horizon's published buy curve and load, recovered from the tail.

    :class:`_Tail` keeps no buy series and no load series -- only the samples
    past the pin -- but each of those was filled from ``when - DAY`` whenever
    the real thing ran out, which for a 24 h lookahead is a horizon instant.
    Load always falls back that way (the borrowed forecast is one horizon
    long); price only where the auction has not cleared, which is why an
    unpublished horizon hour comes back as ``None`` rather than as a guess.
    """
    slots: list[_HorizonSlot] = []
    for sample in tail.samples:
        when = sample.when - DAY
        if when < tail.now or when >= tail.horizon_end:
            continue
        slots.append(
            _HorizonSlot(when, sample.price if sample.price_proxied else None, sample.load_w)
        )
    return slots


def _demand_ladder(tail: _Tail, *, stop: datetime | None) -> list[_Rung]:
    """What a kWh carried past the pin is worth, dearest hour first.

    Two ceilings, and the second is what makes the ladder safe to run on a
    proxied curve. A carried kWh saves the import it displaces -- but only
    while the tail could not have supplied itself more cheaply, and the tail
    can always buy at the lowest price it has already seen. Past that trough
    the value of carrying collapses to the cost of the trough itself, however
    dear the hour looks.
    """
    battery = tail.battery
    charge = battery.charge_efficiency or 1.0
    discharge = battery.discharge_efficiency or 1.0

    rungs: list[_Rung] = []
    cheapest_so_far = float("inf")
    for sample in tail.samples:
        if stop is not None and sample.when >= stop:
            break
        if sample.price is None:
            continue
        cheapest_so_far = min(cheapest_so_far, sample.price)
        pv_w = 0.0 if sample.pv_proxied else sample.pv_w
        deficit_wh = max(0.0, sample.load_w - pv_w) * tail.step_hours
        if deficit_wh <= 0:
            continue
        displaced = discharge * (sample.price - battery.weight_battery_discharge)
        refill_in_tail = (cheapest_so_far + battery.weight_battery_charge) / charge
        rungs.append(_Rung(min(displaced, refill_in_tail), deficit_wh / discharge))
    rungs.sort(key=lambda rung: rung.price, reverse=True)
    return rungs


def _supply_ladder(tail: _Tail, horizon: list[_HorizonSlot]) -> list[_Rung]:
    """Every way of having a kWh in the battery at the pin, cheapest first.

    A purchase in a published horizon hour is rationed by what the charger and
    what is left of the connection could take in that slot: a target nobody can
    reach is not free, because the solver spends the whole horizon chasing it.
    Energy already above the reserve is not free either -- keeping it forgoes
    the best thing it could have done before the pin, which on this tariff is
    usually displacing the horizon's own evening rather than selling into it.
    """
    battery = tail.battery
    charge = battery.charge_efficiency or 1.0
    discharge = battery.discharge_efficiency or 1.0

    rungs = [
        _Rung(
            (slot.price + battery.weight_battery_charge) / charge,
            min(battery.charge_power_max_w, max(0.0, tail.grid.import_max_w - slot.load_w))
            * tail.step_hours
            * charge,
        )
        for slot in horizon
        if slot.price is not None
    ]
    held_wh = max(0.0, (tail.soc_init - tail.reserve) * battery.capacity_wh)
    best = _best_horizon_use(tail, horizon)
    if held_wh > 0 and best is not None:
        rungs.append(_Rung(discharge * (best - battery.weight_battery_discharge), held_wh))
    rungs = [rung for rung in rungs if rung.energy_wh > 0]
    rungs.sort(key=lambda rung: rung.price)
    return rungs


def _best_horizon_use(tail: _Tail, horizon: list[_HorizonSlot]) -> float | None:
    """The most a kWh already in the battery could earn before the pin.

    Kept on both sides of the meter on purpose: exporting earns the sell curve,
    self-consumption saves the buy curve, and on a ``1.25x spot + 0.80`` import
    the second is worth far more almost everywhere. Taking the larger is what
    stops the rule carrying energy past a horizon peak that wanted it more than
    the tail does.
    """
    prices = [slot.price for slot in horizon if slot.price is not None]
    best_sell = max(
        (point.value for point in tail.sell if tail.now <= point.time < tail.horizon_end),
        default=None,
    )
    options = [value for value in (max(prices) if prices else None, best_sell) if value is not None]
    return max(options) if options else None


def _clear_ladders(
    demand: list[_Rung], supply: list[_Rung], ceiling_wh: float
) -> tuple[float, float]:
    """Match the ladders; return the energy carried and what the last kWh cost.

    Demand falls and supply rises, so the first rung where value stops covering
    cost is the last one worth taking -- no need to look further down either
    ladder.
    """
    carried = 0.0
    marginal = 0.0
    index = 0
    left = supply[0].energy_wh if supply else 0.0
    for rung in demand:
        need = rung.energy_wh
        while need > 0 and index < len(supply) and carried < ceiling_wh:
            if supply[index].price > rung.price + 1e-9:
                return carried, marginal
            take = min(need, left, ceiling_wh - carried)
            carried += take
            need -= take
            left -= take
            marginal = supply[index].price
            if left <= 1e-9:
                index += 1
                left = supply[index].energy_wh if index < len(supply) else 0.0
        if index >= len(supply) or carried >= ceiling_wh:
            break
    return carried, marginal


# --- the shadow plan -----------------------------------------------------------


@dataclass(slots=True)
class _PlanSlot:
    """One timestep of the window the shadow plan is solved over."""

    when: datetime
    net_w: float
    """Load minus PV at the meter, before the battery does anything."""
    buy: float
    sell: float
    proxied: bool
    """True when the price is yesterday's curve rather than a cleared market."""


def _plan_window(tail: _Tail) -> tuple[list[_PlanSlot], int, dict[str, Any]]:
    """Every timestep from now to the far end of the tail, and where the pin sits.

    The tail half is read straight off ``tail.samples``. The horizon half has to
    be reconstructed, because :class:`_Tail` was built for backward-walking
    rules and carries the world inside the horizon only in pieces:

    *Load* is exact, by accident of how the series are shaped. The companion's
    load series is one horizon long, so ``_build_tail`` filled every tail sample
    from ``load(when - DAY)``: the tail's load profile *is* the horizon's,
    shifted forward a day, and reading it back gives the in-horizon load to the
    watt. Where a longer forecast makes the tail genuine, the same read is the
    ordinary daily-profile assumption the tail would have used anyway.

    *Buy prices* are recovered, not read: both curves come off the same spot
    price through an affine tariff, so :func:`_buy_from_sell` fits one against
    the other. Where the fit does not hold, the prices are marked guessed and
    fall under the discipline in :func:`_flatten_guesses`.

    *PV* is read straight from ``tail.pv``, which is carried whole.
    """
    step = tail.step
    horizon = tail.horizon_end - tail.now
    pin = max(0, round(horizon / step))
    # Where the in-horizon step at index i finds its twin in the tail: the
    # sample one day later. Zero whenever the horizon is the usual 24 h.
    shift = round((DAY - horizon) / step)
    samples = tail.samples
    slope, intercept, trusted = _buy_from_sell(samples)
    fallback_buy = _mean([sample.price for sample in samples if sample.price is not None])
    guessed_pv = False

    slots: list[_PlanSlot] = []
    for index in range(pin):
        when = tail.now + index * step
        twin = samples[min(max(index + shift, 0), len(samples) - 1)]

        pv_w = _real_value(tail.pv, when)
        if pv_w is None:
            previous = _real_value(tail.pv, when - DAY)
            # Inside the horizon the discounted proxy is the pin-*raising*
            # reading of a guessed sun and zero is not, which is the same
            # one-sidedness the tail applies with the sign flipped: there, sun
            # shortens what must be carried, so guessed sun counts as darkness.
            pv_w = 0.0 if previous is None else previous * PV_PROXY_CONFIDENCE
            guessed_pv = guessed_pv or pv_w > 0

        sell = _real_value(tail.sell, when)
        proxied = False
        if sell is None:
            sell = _real_value(tail.sell, when - DAY)
            proxied = True
        if sell is None or not trusted:
            # No sell curve to read the tariff off, or a tariff that would not
            # fit: fall back to the twin's own price, which is what a
            # backward-walking rule would have seen, and distrust it.
            buy = twin.price if twin.price is not None else fallback_buy
            sell = twin.sell if twin.sell is not None else 0.0
            proxied = True
        else:
            buy = slope * sell + intercept
        slots.append(_PlanSlot(when, twin.load_w - pv_w, buy, sell, proxied))

    for sample in samples:
        pv_w = 0.0 if sample.pv_proxied else sample.pv_w
        buy = sample.price if sample.price is not None else fallback_buy
        slots.append(
            _PlanSlot(
                sample.when,
                sample.load_w - pv_w,
                buy,
                sample.sell if sample.sell is not None else 0.0,
                sample.price_proxied or sample.price is None,
            )
        )

    notes: dict[str, Any] = {
        "price_model": PRICE_MODEL_TARIFF if trusted else PRICE_MODEL_UNTRUSTED,
    }
    if guessed_pv:
        notes["horizon_pv_guessed"] = True
    notes.update(_flatten_guesses(slots))
    return slots, pin, notes


def _buy_from_sell(samples: list[_Sample]) -> tuple[float, float, bool]:
    """Least-squares ``buy = a x sell + b`` over the tail's own price pairs.

    :class:`_Tail` carries the sell series whole and the buy series not at all,
    but both are the same spot curve through an affine tariff, and the tail
    samples hold one matched pair per timestep. Two distinct prices are enough
    to recover the tariff; the fit is over all of them so that a single rounded
    value cannot set it. The residual check decides whether the result may be
    treated as data or only as a guess.
    """
    pairs = [
        (sample.sell, sample.price)
        for sample in samples
        if sample.sell is not None and sample.price is not None
    ]
    if len(pairs) < 2:
        return 0.0, 0.0, False
    mean_sell = sum(sell for sell, _ in pairs) / len(pairs)
    mean_buy = sum(buy for _, buy in pairs) / len(pairs)
    variance = sum((sell - mean_sell) ** 2 for sell, _ in pairs)
    if variance <= 1e-12:
        return 0.0, mean_buy, False
    slope = sum((sell - mean_sell) * (buy - mean_buy) for sell, buy in pairs) / variance
    intercept = mean_buy - slope * mean_sell
    worst = max(abs(slope * sell + intercept - buy) for sell, buy in pairs)
    return slope, intercept, worst <= FIT_TOLERANCE * max(abs(mean_buy), 1e-6)


def _flatten_guesses(slots: list[_PlanSlot]) -> dict[str, Any]:
    """Strip the shape out of every guessed price, and keep only its level.

    The one place this design could quietly ruin itself. A rule can decline to
    act on a proxied input; a solver has no such gesture -- it believes its
    inputs absolutely, and handed yesterday's curve it will trade yesterday's
    troughs against today's peaks as confidently as if the auction had cleared.
    That is precisely the calculation :func:`_sale_credit` refuses to make.

    So a guessed price keeps its *level* and loses its *shape*: every guessed
    slot becomes the median of the guessed slots, which leaves the solver
    knowing that energy past the close will be needed and will cost roughly
    what it costs today, and leaves it nothing to trade against. Floored (buys)
    and capped (sells) at the published median, so a guess can never make the
    future look cheaper to buy or richer to sell than the market that has
    actually cleared.
    """
    guessed = [slot for slot in slots if slot.proxied]
    if not guessed:
        return {}
    published = [slot for slot in slots if not slot.proxied]
    buy = _median([slot.buy for slot in guessed])
    sell = _median([slot.sell for slot in guessed])
    if published:
        buy = max(buy, _median([slot.buy for slot in published]))
        sell = min(sell, _median([slot.sell for slot in published]))
    for slot in guessed:
        slot.buy, slot.sell = buy, sell
    return {"guessed_buy": round(buy, 4), "guessed_sell": round(sell, 4)}


def _residual_value(tail: _Tail, slots: list[_PlanSlot]) -> float:
    """What a kWh above the reserve is worth at the far end of the tail.

    Moving the horizon out 24 h does not remove the need for a terminal value,
    it relocates it; without one the program simply empties the battery into the
    last half hour. The import it would displace, at the window's median price,
    is the only valuation of stored energy that needs no assumption beyond "it
    will be used" -- a median over ~96 slots cannot be moved by one bad guess,
    and it now sits a whole day of dispatch away from the pin. Floored at the
    published median under the same rule as everything else.
    """
    published = [slot.buy for slot in slots if not slot.proxied]
    level = _median([slot.buy for slot in slots])
    if published:
        level = max(level, _median(published))
    return level * (tail.battery.discharge_efficiency or 1.0)


def _solve_window(
    tail: _Tail,
    slots: list[_PlanSlot],
    *,
    pin: int,
    floor: float,
    ceiling: float,
    residual: float,
) -> tuple[list[float], list[float]]:
    """``cost_to_reach + cost_to_go`` at every SOC on the grid, at the pin.

    Losses fall on whichever side of the meter the energy crosses, wear is
    charged per kWh of throughput, and export is capped at the connection and
    earns nothing at a price of zero or below.

    The connection limit is applied to the *increment* the battery adds over
    what the house would have imported idle, and applied hard. The plan does
    not choose the load: an hour whose load alone exceeds the limit is not one
    the battery may be charged for, only one it may not make worse. Nothing can
    go infeasible, because the do-nothing jump has zero increment by
    construction, so every SOC always keeps one affordable move.

    The transition cost depends only on how far the SOC moves, never on where
    it started, so one kernel of ``2 x span + 1`` jump costs per timestep drives
    a min-plus sweep over the whole grid -- which is what keeps a 96-step
    program inside a tenth of a second without numpy.
    """
    battery = tail.battery
    capacity = battery.capacity_wh
    charge_efficiency = battery.charge_efficiency or 1.0
    discharge_efficiency = battery.discharge_efficiency or 1.0
    step_hours = tail.step_hours

    levels = SOC_LEVELS
    soc_grid = [floor + (ceiling - floor) * index / (levels - 1) for index in range(levels)]
    level_wh = (ceiling - floor) / (levels - 1) * capacity

    reach = (
        max(
            battery.charge_power_max_w * charge_efficiency,
            battery.discharge_power_max_w / discharge_efficiency,
        )
        * step_hours
    )
    span = min(levels - 1, max(1, ceil(reach / level_wh)))

    power: list[float] = []
    wear: list[float] = []
    blocked: list[bool] = []
    for jump in range(-span, span + 1):
        delta_wh = jump * level_wh
        charge_w = delta_wh / (charge_efficiency * step_hours) if delta_wh > 0 else 0.0
        discharge_w = -delta_wh * discharge_efficiency / step_hours if delta_wh < 0 else 0.0
        power.append(charge_w - discharge_w)
        wear.append(
            (
                charge_w * battery.weight_battery_charge
                + discharge_w * battery.weight_battery_discharge
            )
            * step_hours
            / 1000
        )
        blocked.append(
            charge_w > battery.charge_power_max_w + 1e-6
            or discharge_w > battery.discharge_power_max_w + 1e-6
        )

    import_max = tail.grid.import_max_w
    export_max = tail.grid.export_max_w

    def kernel(slot: _PlanSlot) -> list[tuple[int, float]]:
        """Finite (jump offset, cost) pairs for one timestep."""
        ceiling_w = max(import_max, slot.net_w)
        moves: list[tuple[int, float]] = []
        for index, p_ac in enumerate(power):
            if blocked[index]:
                continue
            grid_w = slot.net_w + p_ac
            if grid_w > ceiling_w + 1e-6:
                continue
            imported = grid_w if grid_w > 0.0 else 0.0
            exported = min(-grid_w, export_max) if grid_w < 0.0 else 0.0
            cost = imported * slot.buy - exported * max(slot.sell, 0.0)
            moves.append((index, cost * step_hours / 1000 + wear[index]))
        return moves

    def sweep(value: list[float], moves: list[tuple[int, float]]) -> list[float]:
        """``out[i] = min over jumps of cost(jump) + value[i + jump]``."""
        out = [inf] * levels
        for level in range(levels):
            best = inf
            base = level - span
            for offset, cost in moves:
                target = base + offset
                if 0 <= target < levels:
                    candidate = cost + value[target]
                    if candidate < best:
                        best = candidate
            out[level] = best
        return out

    to_go = [-(soc - floor) * capacity / 1000 * residual for soc in soc_grid]
    for index in range(len(slots) - 1, pin - 1, -1):
        to_go = sweep(to_go, kernel(slots[index]))

    start = min(
        range(levels),
        key=lambda index: abs(soc_grid[index] - min(max(tail.soc_init, floor), ceiling)),
    )
    to_reach = [inf] * levels
    to_reach[start] = 0.0
    for index in range(pin):
        # Arriving is the same walk read backwards: where the value pass asks
        # what a jump leads to, this asks what it came from.
        moves = [(2 * span - offset, cost) for offset, cost in kernel(slots[index])]
        to_reach = sweep(to_reach, moves)

    return soc_grid, [
        reach_cost + go_cost for reach_cost, go_cost in zip(to_reach, to_go, strict=True)
    ]


# --- the shared view ---------------------------------------------------------


def _build_tail(
    *,
    soc_init: float,
    battery: BatteryConfig,
    now: datetime,
    horizon_end: datetime,
    step: timedelta,
    pv: Series,
    load: Series,
    buy_price: Series,
    sell_price: Series,
    grid: GridConfig,
    load_source: str,
) -> _Tail:
    """Walk the lookahead window once, filling every gap with a flagged guess."""
    day = timedelta(hours=24)
    step_hours = step.total_seconds() / 3600
    load_mean = sum(load.values) / len(load)
    samples: list[_Sample] = []
    price_proxied = pv_proxied = pv_unknown = False

    when = horizon_end
    while when < horizon_end + LOOKAHEAD:
        pv_w = _real_value(pv, when)
        sample_proxied = False
        if pv_w is None:
            # Where the forecast ends, the previous day's shape is the only
            # evidence there is. Discounted hard, and barred from the bridge
            # (see _Tail.deficit_until), because unlike prices the daily
            # *shape* of PV is weather, not structure.
            previous_day = _real_value(pv, when - day)
            if previous_day is None:
                pv_w = 0.0
                pv_unknown = True
            else:
                pv_w = previous_day * PV_PROXY_CONFIDENCE
                sample_proxied = pv_w > 0
                pv_proxied = pv_proxied or sample_proxied

        load_w = _real_value(load, when)
        if load_w is None:
            # The load profile is a daily pattern, so yesterday's same slot is
            # the honest statistical guess; the mean is the last resort.
            load_w = _real_value(load, when - day)
            if load_w is None:
                load_w = load_mean

        price = _real_value(buy_price, when)
        sample_price_proxied = False
        if price is None:
            # Prices repeat their daily *shape* (night trough, evening peak),
            # so before tomorrow's market publishes, today's curve shifted a
            # day is a usable proxy -- flagged, so nobody mistakes it for data.
            price = _real_value(buy_price, when - day)
            if price is not None:
                sample_price_proxied = True
                price_proxied = True

        sell = _real_value(sell_price, when)
        if sell is None:
            sell = _real_value(sell_price, when - day)

        samples.append(
            _Sample(when, pv_w, sample_proxied, load_w, price, sample_price_proxied, sell)
        )
        when += step

    return _Tail(
        battery=battery,
        grid=grid,
        soc_init=soc_init,
        now=now,
        horizon_end=horizon_end,
        step=step,
        samples=samples,
        blocks=_surplus_blocks(samples, step_hours),
        cheap_threshold=_percentile(buy_price.values, CHEAP_PERCENTILE),
        material_wh=SURPLUS_MATERIAL_FRACTION * battery.capacity_wh,
        pv=pv,
        sell=sell_price,
        price_proxied=price_proxied,
        pv_proxied=pv_proxied,
        pv_unknown=pv_unknown,
        load_source=load_source,
    )


@dataclass(slots=True)
class _Cover:
    """What the battery must hold at the pin to stay off its reserve."""

    soc: float
    energy_wh: float
    """``soc`` above the reserve, in battery Wh -- what could be sold at all."""
    binding_at: datetime | None
    """When the battery would touch the reserve. ``None`` means it never does."""
    raw_soc: float = 0.0
    """The requirement before the battery's own size was applied. Above
    ``soc_max`` it says the night is longer than the battery, which is a fact
    worth reporting rather than a number to round away."""


@dataclass(slots=True)
class _Sale:
    """A profitable round trip: sell before the pin, buy back before sunrise."""

    energy_wh: float = 0.0
    margin: float = 0.0
    sell_price: float = 0.0
    buy_price: float = 0.0
    blocked: str = ""
    """Why nothing was sold, when nothing was. Empty when a sale was made."""


def _required_soc(
    tail: _Tail,
    *,
    refills: set[datetime] | None = None,
    reset_from: datetime | None = None,
) -> _Cover:
    """The lowest SOC at the pin that never puts the battery under its reserve.

    A backward pass, which is what makes it a single rule rather than a set of
    cases. Walking from the far end of the lookahead towards the pin,
    ``required`` is the minimum SOC at each step for the remainder to survive:
    a deficit raises it by what the battery must supply, a surplus lowers it by
    what the sun will put back, and the floor at the reserve is what makes sun
    beyond the battery's capacity stop counting -- 40 kWh of Sunday cannot
    excuse arriving empty on Saturday evening, it just resets the requirement
    to the reserve and no further.

    The ceiling at ``soc_max`` matters for the same reason in the other
    direction: a requirement above full is unreachable, and carrying it
    backwards through a later surplus would understate what that surplus can
    really pay for.

    Proxied PV counts as darkness, as it does in :meth:`_Tail.deficit_until`:
    guessed sun may never shorten what the house has to carry.

    Two ways to say the requirement stops. ``reset_from`` is an instant past
    which the battery is somebody else's problem; ``refills`` is a set of
    individual timestamps that reset it. Both exist because "the sun comes
    back" is not the only way a night can end -- see :func:`_night_cover` for
    the one case where the shipping rule accepts a grid purchase as the end of
    one, and :func:`_priced_bridge` for the version that accepts every cheap
    hour.
    """
    battery = tail.battery
    capacity = battery.capacity_wh
    charge_efficiency = battery.charge_efficiency or 1.0
    discharge_efficiency = battery.discharge_efficiency or 1.0
    required = raw = tail.reserve
    binding: datetime | None = None

    for sample in reversed(tail.samples):
        covered = (reset_from is not None and sample.when >= reset_from) or (
            refills is not None and sample.when in refills
        )
        if covered:
            required = raw = tail.reserve
            binding = None
            continue
        pv_w = 0.0 if sample.pv_proxied else sample.pv_w
        net_wh = (sample.load_w - pv_w) * tail.step_hours
        # Losses fall on whichever side of the meter the energy crosses: a
        # deficit costs more out of the battery than it delivers, a surplus
        # banks less than it offers.
        delta_wh = net_wh / discharge_efficiency if net_wh >= 0 else net_wh * charge_efficiency
        raised = required + delta_wh / capacity
        if raised <= tail.reserve:
            required = raw = tail.reserve
            binding = None
        else:
            if required <= tail.reserve:
                # Coming out of a covered stretch: this is where the drawdown
                # that sets the requirement ends, i.e. the moment the battery
                # would run down to its reserve.
                binding = sample.when + tail.step
            required = min(battery.soc_max, raised)
            raw = max(tail.reserve, raw + delta_wh / capacity)

    return _Cover(
        soc=required,
        energy_wh=max(0.0, (required - tail.reserve) * capacity),
        binding_at=binding,
        raw_soc=raw,
    )


def _cover_horizon(tail: _Tail) -> tuple[datetime | None, str]:
    """Where the cover's responsibility ends, and what ends it.

    The *last* forecast surplus block, not the first. Stopping at the first
    would ignore a weak day that cannot pay for the night behind it -- exactly
    the case a pin landing mid-morning has to get right -- while running to the
    edge of the lookahead would demand cover for the darkness after the final
    sunrise, which is a property of the window rather than of the world.

    Only forecast blocks qualify. A block inferred from the previous day may
    lengthen what has to be carried but never shorten it, the same asymmetry
    :func:`_replenishment` applies.

    With no forecast sun at all, a cheap hour ends the night instead: a week of
    December overcast has no sun to wait for, and refusing the grid there would
    pin every run at ``soc_max``.
    """
    block = tail.last_block(proxied=False)
    if block is not None:
        return block.start, REPLENISH_SOLAR
    cheap_at = tail.first_cheap()
    if cheap_at is not None:
        return cheap_at, REPLENISH_CHEAP_GRID
    return None, REPLENISH_NONE


def _sale_credit(tail: _Tail, binding_at: datetime | None, carry_wh: float) -> _Sale:
    """How much of the cover is worth selling before the pin instead of carrying.

    The trade is explicit: release a kWh into a high-priced hour before the
    pin, buy it back at the cheapest hour before the battery would have hit its
    reserve. It pays when

        ``sell x discharge_eff  >  buy / charge_eff + wear``

    with wear the configured charge and discharge cycle costs. Both prices are
    the tariff's own -- on a ``1.25x spot + adder`` import this is a high bar,
    which is the honest answer rather than a conservative one.

    Only *published* buy prices qualify. Tomorrow night's curve is a proxy for
    half the evening on most runs, and a spread computed against a guess is
    exactly the calculation that talks a battery into arriving empty. Refusing
    it fails towards carrying the energy, which is the recoverable mistake.

    The credit is an energy, not a verdict: it is capped by what could
    physically leave in the profitable hours, and by the cover itself, so a
    single expensive slot releases one slot's worth rather than the night.
    """
    if carry_wh <= 0 or binding_at is None:
        return _Sale()
    if not tail.sell:
        return _Sale(blocked=SALE_NO_SELL_PRICE)

    published = [
        sample.price
        for sample in tail.samples
        if sample.when < binding_at and sample.price is not None and not sample.price_proxied
    ]
    if not published:
        return _Sale(blocked=SALE_PRICES_UNPUBLISHED)
    buy = min(published)

    battery = tail.battery
    wear = battery.weight_battery_discharge + battery.weight_battery_charge
    restore = buy / (battery.charge_efficiency or 1.0) + wear
    # Selling is limited by whichever gives way first, the battery or the
    # meter; on a hybrid inverter both are usually the same number.
    power_w = min(tail.grid.export_max_w, battery.discharge_power_max_w)
    slot_hours = (tail.sell.step() or tail.step).total_seconds() / 3600

    sellable_wh = 0.0
    best_margin = 0.0
    best_price = 0.0
    for point in tail.sell:
        if point.time < tail.now or point.time >= tail.horizon_end:
            continue
        margin = point.value * (battery.discharge_efficiency or 1.0) - restore
        if margin <= 0:
            continue
        sellable_wh += power_w * slot_hours
        if margin > best_margin:
            best_margin, best_price = margin, point.value

    if sellable_wh <= 0:
        return _Sale(blocked=SALE_NO_MARGIN, buy_price=buy)
    return _Sale(
        energy_wh=min(sellable_wh, carry_wh),
        margin=best_margin,
        sell_price=best_price,
        buy_price=buy,
    )


def _surplus_blocks(samples: list[_Sample], step_hours: float) -> list[_Block]:
    """Contiguous runs of PV above load, tolerant of short cloudy gaps."""
    gap_limit = SURPLUS_BLOCK_GAP.total_seconds() / 3600
    blocks: list[_Block] = []
    start: datetime | None = None
    energy = 0.0
    proxied = False
    gap = 0.0

    for sample in samples:
        surplus_wh = (sample.pv_w - sample.load_w) * step_hours
        if surplus_wh > 0:
            if start is None:
                start, energy, proxied = sample.when, 0.0, False
            energy += surplus_wh
            proxied = proxied or sample.pv_proxied
            gap = 0.0
        elif start is not None:
            gap += step_hours
            if gap >= gap_limit:
                blocks.append(_Block(start, energy, proxied))
                start = None
    if start is not None:
        blocks.append(_Block(start, energy, proxied))
    return blocks


def _annotate_reach(decision: EndSocDecision, tail: _Tail) -> None:
    """Flag a target the battery cannot physically reach in the time left.

    Annotate, do not clamp: from EMHASS 0.18 an over-ambitious ``soc_final``
    is a soft constraint the solver approaches as closely as power limits
    allow, warning rather than going infeasible. It knows the physics better
    than a bound recomputed here would.
    """
    hours = (tail.horizon_end - tail.now).total_seconds() / 3600
    if hours <= 0:
        return
    capacity = tail.battery.capacity_wh
    reach_up = tail.battery.charge_power_max_w * hours / capacity
    reach_down = tail.battery.discharge_power_max_w * hours / capacity
    if (
        decision.soc > tail.soc_init + reach_up + 1e-6
        or decision.soc < tail.soc_init - reach_down - 1e-6
    ):
        decision.details["unreachable"] = True


# --- explanations ------------------------------------------------------------


def _cover_reason(*, tail: _Tail, soc: float, cover: _Cover, sale: _Sale, kind: str) -> str:
    if cover.energy_wh <= 0 or cover.binding_at is None:
        what = "sun" if kind == REPLENISH_SOLAR else "cheap grid price"
        return (
            f"The {what} returns before the battery is needed, so ending at "
            f"the {tail.reserve:.0%} reserve."
        )
    local = dt_util.as_local(cover.binding_at)
    until = {
        REPLENISH_SOLAR: f"the sun is back, around {local:%a %H:%M}",
        REPLENISH_CHEAP_GRID: f"grid prices dip, around {local:%a %H:%M}",
    }.get(kind, f"the forecast runs out at {local:%a %H:%M}")
    sentence = (
        f"Covering {cover.energy_wh / 1000:.1f} kWh on top of the "
        f"{tail.reserve:.0%} reserve: that is what the house needs before "
        f"{until}."
    )
    if sale.energy_wh > 0:
        return (
            f"{sentence} Selling {sale.energy_wh / 1000:.1f} kWh of it first, "
            f"at {sale.sell_price:.2f} against {sale.buy_price:.2f} to buy back "
            f"-- {sale.margin:.2f}/kWh clear -- so ending at {soc:.0%}."
        )
    return sentence


def _pv_tail(tail: _Tail) -> str:
    if tail.pv_proxied:
        return PV_TAIL_PROXY
    if tail.pv_unknown:
        return PV_TAIL_ZERO
    return PV_TAIL_KNOWN


def _priced_reason(tail: _Tail, soc: float, carry_wh: float, bound: str) -> str:
    if bound == BOUND_PRICED_LIFT:
        return (
            f"Carrying {carry_wh / 1000:.1f} kWh past the horizon -- more than "
            "the house strictly needs -- because cheap hours before the pin beat "
            f"what the tail would otherwise import: ending at {soc:.0%}."
        )
    if bound == BOUND_RESERVE:
        return f"Nothing past the horizon pays for itself, so ending at the {soc:.0%} reserve."
    return (
        f"Covering {carry_wh / 1000:.1f} kWh on top of the {tail.reserve:.0%} "
        "reserve: no hour before the pin is cheap enough to be worth carrying "
        f"more, so ending at {soc:.0%}."
    )


def _plan_reason(
    *,
    hours: float,
    pin_hours: float,
    soc: float,
    best: float,
    at_reserve: float,
    at_full: float,
) -> str:
    """One sentence naming all three spans, because they are all different.

    The plan runs over ``hours`` (the horizon plus the lookahead), the number
    it reports is the SOC at ``pin_hours`` (the horizon's end, which is the
    only instant EMHASS can be told about), and the money it quotes is the
    difference in *whole-window* cost between that pin and the worst one. Say
    "at the horizon" alone in a sentence that has just said "48 h" and a reader
    has no way to tell which of the two the percentage belongs to.
    """
    saved = max(at_reserve, at_full) - best
    if saved <= PIN_TOLERANCE:
        return (
            f"Planning {hours:.0f} h ahead, every SOC at the {pin_hours:.0f} h mark "
            f"costs about the same over those {hours:.0f} h; taking {soc:.0%}."
        )
    against = "arriving at the reserve" if at_reserve >= at_full else "arriving full"
    return (
        f"Planning {hours:.0f} h ahead, the cheapest way through holds {soc:.0%} at "
        f"the {pin_hours:.0f} h horizon -- {saved:.2f} cheaper across the whole "
        f"{hours:.0f} h than {against}."
    )


# --- series helpers ----------------------------------------------------------


def _energy_kwh(series: Series, start: datetime, end: datetime) -> float:
    """Integrate a power series over ``[start, end)``, in kWh.

    Each point covers the span to the next one, so an irregular series (or one
    that stops mid-window) integrates honestly instead of assuming a grid.
    """
    if not series:
        return 0.0
    points = list(series)
    tail_step = series.step() or timedelta(minutes=30)
    start, end = start.astimezone(dt_util.UTC), end.astimezone(dt_util.UTC)
    total_wh = 0.0
    for index, point in enumerate(points):
        if point.time < start or point.time >= end:
            continue
        following = points[index + 1].time if index + 1 < len(points) else point.time + tail_step
        span = min(following, end) - point.time
        total_wh += point.value * max(0.0, span.total_seconds() / 3600)
    return total_wh / 1000


def _percentile(values: tuple[float, ...], fraction: float) -> float:
    """The value ``fraction`` of the way up the sorted values (no interpolation)."""
    ordered = sorted(values)
    index = int(fraction * (len(ordered) - 1))
    return ordered[max(0, min(len(ordered) - 1, index))]


def _real_value(series: Series, when: datetime) -> float | None:
    """``value_at``, but only within the span the series actually covers.

    ``Series.value_at`` holds its last value forever forward; past the end
    that is exactly the flat-lined-forecast trap this module exists to avoid,
    so coverage is checked first.
    """
    if not series or not series.covers(when):
        return None
    return series.value_at(when)


def _value_at(soc_grid: list[float], total: list[float], soc: float) -> float:
    """The shadow plan's cost at one SOC, over the levels it could actually reach."""
    finite = [(grid, cost) for grid, cost in zip(soc_grid, total, strict=True) if cost < inf]
    if not finite:
        return inf
    if soc <= finite[0][0]:
        return finite[0][1]
    for (low, low_cost), (high, high_cost) in pairwise(finite):
        if soc <= high:
            weight = (soc - low) / (high - low) if high > low else 0.0
            return low_cost + weight * (high_cost - low_cost)
    return finite[-1][1]


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0

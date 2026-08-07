"""Decide the battery's end-of-horizon SOC target.

EMHASS pins the battery's end-of-horizon SOC to whatever ``soc_final`` a
request carries (softly from 0.18: deviation is penalised far above the
dearest import slot, so the target is honoured whenever physics allows). Its
API has no way to say "you decide" -- a terminal condition is always the
caller's job -- so this module is where the companion decides what stored
energy is worth having when the known world ends.

Optimized mode is governed by one rule:

    **Never plan to arrive at the pin with less than the house needs before
    the sun comes back -- unless selling that energy first demonstrably pays
    for buying it again.**

The rule is deliberately one-sided. Arriving short means importing across a
night at ``spot x multiplier + adder`` while the cheap way to fill a battery,
sunlight, is hours away; arriving heavy costs at most the spread between what
the surplus would have sold for and what it displaces later. Those are not
symmetric mistakes, so the cover is a floor and everything else negotiates
above it.

"What the house needs before the sun comes back" is not a special case for
evenings. :func:`_required_soc` walks the whole lookahead backwards and asks
for the lowest SOC at the pin that never puts the battery under its reserve --
which is the night's deficit when the pin lands after sunset, ~0 when it lands
at dawn with the sun about to rise, and the shortfall the day cannot cover
when it lands mid-morning on a weak one. One walk, no calendar.

The exception is priced, not judged: a kWh may leave the battery before the
pin only when ``sell x discharge_eff`` beats ``buy / charge_eff + wear`` on the
cheapest hour before the battery would hit its reserve -- and only on *published*
prices. A proxied price curve may never talk the cover down, the same asymmetry
:func:`_build_tail` already applies to proxied PV.

Everything here is pure computation on :class:`~.models.Series`; fetching them
is the coordinator's job.

Candidates
----------

There is no ground truth for a terminal-value heuristic short of running one
against a real house for a season, so this module computes *several* and
reports them all. :data:`CANDIDATES` is evaluated in full on every run against
one shared view of the world (:class:`_Tail`); :data:`ACTIVE_CANDIDATE` names
the one whose number actually reaches EMHASS. The rest ride along in the End
SOC sensor's ``tests`` attribute, where a month of history says which one
should have been driving.

Adding another is three lines: write ``_my_idea(tail) -> EndSocDecision``, add
``_Candidate("my_idea", "Test 6 -- my idea", _my_idea)`` to
:data:`CANDIDATES`, and it shows up in the sensor on the next run. Point
:data:`ACTIVE_CANDIDATE` at it to promote it.

Tests 1 to 3 are frozen on purpose. They are the yardsticks a month of
recorded ``tests`` history is measured against, and editing one silently
rebases that history, so improvements go in as a new candidate rather than as
a change to an old one.

See docs/end_soc_plan.md for the full design and the live A/B numbers that
motivated it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
BOUND_BRIDGE: str = "bridge"
BOUND_SOLAR_HEADROOM: str = "solar_headroom"
BOUND_NIGHT_COVER: str = "night_cover"
BOUND_SALE: str = "profitable_sale"
BOUND_CURTAILMENT: str = "curtailment"
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

TEST_BRIDGE_ONLY: str = "bridge_only"
TEST_SOLAR_HEADROOM: str = "solar_headroom"
TEST_DAILY_RATIO: str = "daily_ratio"
TEST_NIGHT_COVER: str = "night_cover"
TEST_PRICED_BRIDGE: str = "priced_bridge"

# The plant-scale constant from the original Jinja template this module's
# third candidate reproduces: the daily yield that counts as a *strong* day.
# Hard-coded because the candidate is a shadow calculation -- promoting it to
# ACTIVE_CANDIDATE means turning this into a setting first.
TEMPLATE_REFERENCE_YIELD_KWH: float = 40.0


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

    def first_block(self, *, proxied: bool | None = None) -> _Block | None:
        """The first surplus block big enough to count as a refill.

        ``proxied`` filters on where the PV came from: ``False`` for blocks
        the forecast actually predicts, ``True`` for ones inferred from the
        previous day, ``None`` for either.
        """
        return next(
            (
                block
                for block in self.blocks
                if block.energy_wh >= self.material_wh
                and (proxied is None or block.proxied is proxied)
            ),
            None,
        )

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


def _bridge_only(tail: _Tail) -> EndSocDecision:
    """Test 1: the reserve plus the energy needed to reach the next refill.

    The calculation as it shipped. One-directional by construction -- it can
    only ever add to the reserve -- so on any day whose next refill is close
    it returns the reserve itself, which is what made it look inert.
    """
    replenish_at, kind, _ = _replenishment(tail, allow_guess=False)
    bridge_wh = tail.deficit_until(replenish_at)
    raw = tail.reserve + bridge_wh / tail.battery.capacity_wh
    soc = tail.clamp(raw)

    details: dict[str, Any] = {
        "replenishment_kind": kind,
        "bridge_energy_wh": round(bridge_wh),
        "bridge_target": round(raw, 4),
    }
    if replenish_at is not None:
        details["next_replenishment"] = replenish_at.isoformat()
    if soc != raw:
        details["clamped_by"] = BOUND_RANGE
        details["raw_target"] = round(raw, 4)
    return EndSocDecision(
        soc=round(soc, 4),
        reason=_bridge_reason(tail.reserve, bridge_wh, kind, replenish_at),
        details=details,
    )


def _solar_headroom(tail: _Tail) -> EndSocDecision:
    """Test 2: squeeze the target between a bridge floor and a solar ceiling.

    The floor is Test 1's: enough to reach the next refill without buying.
    The ceiling is the room the next surplus needs in order to land in the
    battery rather than on the grid, applied at the moment that surplus
    *starts* -- by which time the bridge has already been drawn down, so the
    target may sit that much higher than the raw surplus suggests. The two
    meet at ``soc_max - reserve >= surplus / capacity``, so the ceiling binds
    only when the coming sun genuinely exceeds the usable span, and is inert
    all winter.
    """
    battery = tail.battery
    capacity = battery.capacity_wh

    replenish_at, kind, guessed = _replenishment(tail, allow_guess=True)
    bridge_wh = tail.deficit_until(replenish_at)
    need = tail.reserve + bridge_wh / capacity

    # A guessed block counts here, at its already-discounted weight: the cost
    # of being wrong is a partly-charged battery, not a flat one.
    absorb = tail.first_block()
    room: float | None = None
    surplus_wh = 0.0
    if absorb is not None:
        surplus_wh = absorb.energy_wh * battery.charge_efficiency
        drawdown_wh = tail.deficit_until(absorb.start)
        room = battery.soc_max - max(0.0, surplus_wh - drawdown_wh) / capacity

    target = need
    bound = BOUND_BRIDGE
    if room is not None and room < target:
        target = room
        bound = BOUND_SOLAR_HEADROOM
    if target < tail.reserve:
        # Solar headroom may push the target down to the user's comfort floor
        # but never through it.
        target = tail.reserve
        bound = BOUND_RESERVE
    soc = tail.clamp(target)
    if soc != target:
        bound = BOUND_RANGE

    details: dict[str, Any] = {
        "replenishment_kind": kind,
        "bridge_energy_wh": round(bridge_wh),
        "surplus_ahead_wh": round(surplus_wh),
        "bridge_target": round(need, 4),
    }
    if replenish_at is not None:
        details["next_replenishment"] = replenish_at.isoformat()
    if guessed:
        details["replenishment_guessed"] = True
    if room is not None:
        details["headroom_cap"] = round(room, 4)
    if bound != BOUND_BRIDGE:
        details["clamped_by"] = bound
        details["raw_target"] = round(need, 4)

    return EndSocDecision(
        soc=round(soc, 4),
        reason=_headroom_reason(
            soc=soc,
            reserve=tail.reserve,
            bridge_wh=bridge_wh,
            surplus_wh=surplus_wh,
            kind=kind,
            replenish_at=replenish_at,
            bound=bound,
        ),
        details=details,
    )


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


def _priced_bridge(tail: _Tail) -> EndSocDecision:
    """Test 5: the two ideas Test 4 deliberately refuses, run where they cost nothing.

    Both are lowering mechanisms Test 4's floor forbids, and both are worth a
    month of shadow history before anyone believes an argument about them.

    *A cheap hour counts as a refill.* Test 4 carries the whole night because
    on a ``1.25x + 0.80`` import tariff buying it back rarely pays. Where the
    adder is small or the trough deep that stops being true, so here a
    published cheap hour resets the requirement exactly as sunrise does. Only
    published: a proxied trough is a guess about a market that has not opened.

    *Room, but only for surplus nobody wants.* Test 2 made room for all coming
    sun and so emptied the battery on every clear day (see its docstring for
    why its binding region is its refutation). The narrower question is how
    much surplus would be *lost* rather than merely exported: power above the
    grid's export limit, plus everything in hours whose sell price has gone to
    zero or below. That is the only surplus a stored kWh genuinely rescues, and
    on a 9 kW export limit against a 7 kW array it is usually nothing at all,
    which is the point -- the ceiling should be inert unless something is
    actually being thrown away.
    """
    capacity = tail.battery.capacity_wh
    reset_from, _ = _cover_horizon(tail)
    refills = {
        sample.when
        for sample in tail.samples
        if sample.price is not None
        and not sample.price_proxied
        and sample.price <= tail.cheap_threshold
    }
    cover = _required_soc(tail, refills=refills, reset_from=reset_from)

    lost_wh, drawdown_wh = _lost_surplus(tail)
    target = cover.soc
    bound = BOUND_NIGHT_COVER
    room: float | None = None
    if lost_wh > 0:
        room = tail.battery.soc_max - max(0.0, lost_wh - drawdown_wh) / capacity
        if room < target:
            target, bound = room, BOUND_CURTAILMENT
    if target < tail.reserve:
        target, bound = tail.reserve, BOUND_RESERVE
    soc = tail.clamp(target)
    if soc != target or (bound == BOUND_NIGHT_COVER and cover.raw_soc > cover.soc + 1e-9):
        bound = BOUND_RANGE

    details: dict[str, Any] = {
        "cover_energy_wh": round(cover.energy_wh),
        "cover_target": round(cover.soc, 4),
        "cheap_refills": len(refills),
        "lost_surplus_wh": round(lost_wh),
    }
    if room is not None:
        details["curtailment_cap"] = round(room, 4)
    if bound != BOUND_NIGHT_COVER:
        details["clamped_by"] = bound
        details["raw_target"] = round(max(cover.soc, cover.raw_soc), 4)

    if lost_wh > 0 and bound in (BOUND_CURTAILMENT, BOUND_RESERVE):
        reason = (
            f"Leaving room for {lost_wh / 1000:.1f} kWh of surplus that would "
            f"otherwise be clipped or given away: ending at {soc:.0%}."
        )
    elif refills:
        reason = (
            f"Covering {cover.energy_wh / 1000:.1f} kWh on top of the "
            f"{tail.reserve:.0%} reserve; {len(refills)} published cheap "
            "hours count as refills."
        )
    else:
        reason = _cover_reason(tail=tail, soc=soc, cover=cover, sale=_Sale(), kind=REPLENISH_SOLAR)
    return EndSocDecision(soc=round(soc, 4), reason=reason, details=details)


CANDIDATES: tuple[_Candidate, ...] = (
    _Candidate(TEST_BRIDGE_ONLY, "Test 1 -- bridge to the next refill", _bridge_only),
    _Candidate(TEST_SOLAR_HEADROOM, "Test 2 -- bridge floor, solar ceiling", _solar_headroom),
    _Candidate(TEST_DAILY_RATIO, "Test 3 -- daily-yield template", _daily_ratio),
    _Candidate(TEST_NIGHT_COVER, "Test 4 -- night cover, sold only when it pays", _night_cover),
    _Candidate(TEST_PRICED_BRIDGE, "Test 5 -- priced bridge, curtailment ceiling", _priced_bridge),
)

ACTIVE_CANDIDATE: str = TEST_NIGHT_COVER


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


def _lost_surplus(tail: _Tail) -> tuple[float, float]:
    """Surplus in the next block that would be thrown away, and the drawdown to it.

    Thrown away means one of two things: power above what the grid connection
    will take, which is physically curtailed, or any surplus at all in an hour
    whose sell price has reached zero, where exporting earns nothing. Ordinary
    exported surplus is not lost -- it is sold -- which is the distinction
    Test 2 never drew.
    """
    block = tail.first_block()
    if block is None:
        return 0.0, 0.0

    gap_limit = SURPLUS_BLOCK_GAP.total_seconds() / 3600
    export_max = tail.grid.export_max_w
    total_wh = 0.0
    gap = 0.0
    for sample in tail.samples:
        if sample.when < block.start:
            continue
        surplus_w = sample.pv_w - sample.load_w
        if surplus_w <= 0:
            gap += tail.step_hours
            if gap >= gap_limit:
                break
            continue
        gap = 0.0
        worthless_w = surplus_w if sample.sell is not None and sample.sell <= 0 else 0.0
        clipped_w = max(0.0, surplus_w - export_max)
        total_wh += max(worthless_w, clipped_w) * tail.step_hours

    return total_wh * tail.battery.charge_efficiency, tail.deficit_until(block.start)


def _replenishment(tail: _Tail, *, allow_guess: bool) -> tuple[datetime | None, str, bool]:
    """When the battery can next be refilled, and by what.

    Free solar outranks a cheap grid price however soon the dip comes: buying
    anything, even at the cheap end of the tariff, to skip a short wait for
    free PV is a bad trade for self-consumption. Only a lookahead with no
    surplus at all falls through to price.

    With ``allow_guess``, a block inferred from the previous day may be that
    event too -- but only when it lands at or after the price trough. A
    guessed sunrise may extend the wait for a refill (which only holds more
    energy back, and the next MPC cycle corrects it) and must never shorten
    it, which is what would strand the house on a battery emptied for sun
    that never arrived.
    """
    forecast = tail.first_block(proxied=False)
    if forecast is not None:
        return forecast.start, REPLENISH_SOLAR, False

    cheap_at = tail.first_cheap()
    if allow_guess:
        guess = tail.first_block(proxied=True)
        if guess is not None and (cheap_at is None or guess.start >= cheap_at):
            return guess.start, REPLENISH_SOLAR, True

    if cheap_at is not None:
        return cheap_at, REPLENISH_CHEAP_GRID, False
    return None, REPLENISH_NONE, False


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


def _bridge_reason(
    reserve: float, bridge_wh: float, kind: str, replenish_at: datetime | None
) -> str:
    if kind == REPLENISH_NONE or replenish_at is None:
        return (
            f"No refill opportunity visible within {int(LOOKAHEAD.total_seconds() // 3600)} h "
            f"past the horizon; holding the {reserve:.0%} reserve plus "
            f"{bridge_wh / 1000:.1f} kWh for the whole stretch."
        )
    local = dt_util.as_local(replenish_at)
    what = "solar surplus expected" if kind == REPLENISH_SOLAR else "cheap grid prices expected"
    return (
        f"Holding {bridge_wh / 1000:.1f} kWh on top of the {reserve:.0%} reserve "
        f"to bridge until {what} around {local:%a %H:%M}."
    )


def _headroom_reason(
    *,
    soc: float,
    reserve: float,
    bridge_wh: float,
    surplus_wh: float,
    kind: str,
    replenish_at: datetime | None,
    bound: str,
) -> str:
    if bound in (BOUND_SOLAR_HEADROOM, BOUND_RESERVE) and surplus_wh > 0:
        floor = f" Held at the {reserve:.0%} reserve floor." if bound == BOUND_RESERVE else ""
        return (
            f"Leaving room for {surplus_wh / 1000:.1f} kWh of expected solar "
            f"surplus: ending at {soc:.0%} so the sun charges the battery "
            f"instead of being exported.{floor}"
        )
    return _bridge_reason(reserve, bridge_wh, kind, replenish_at)


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

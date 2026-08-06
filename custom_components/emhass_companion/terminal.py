"""Decide the battery's end-of-horizon SOC target.

EMHASS pins the battery's end-of-horizon SOC to whatever ``soc_final`` a
request carries (softly from 0.18: deviation is penalised far above the
dearest import slot, so the target is honoured whenever physics allows). Its
API has no way to say "you decide" -- a terminal condition is always the
caller's job -- so this module is where the companion decides what stored
energy is worth having when the known world ends.

Optimized mode judges the battery's end state against one goal:
**self-consumption**. Every kWh held at horizon end is a kWh of tomorrow's sun
that will be exported instead of stored, and every kWh missing is a kWh that
has to be bought. Solar leads and price follows in both directions: a PV
surplus block outranks a price trough as the refill event however soon the dip
comes, and the solar ceiling only exists at all when there is sun to make room
for. In winter -- short, weak days, no surplus worth the name -- the ceiling
never binds and price alone decides, which is the intended fallback rather
than a degraded mode.

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
``_Candidate("my_idea", "Test 4 -- my idea", _my_idea)`` to
:data:`CANDIDATES`, and it shows up in the sensor on the next run. Point
:data:`ACTIVE_CANDIDATE` at it to promote it.

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
from .models import BatteryConfig, Series

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
BOUND_RESERVE: str = "reserve"
BOUND_RANGE: str = "range"
BOUND_HYSTERESIS: str = "hysteresis"

# Where the load series came from, for the sensor -- this module cannot tell
# on its own, since a borrowed series looks like any other. See
# coordinator.EmhassCoordinator._load_for_terminal.
LOAD_SOURCE_PROFILE: str = "own forecast"
LOAD_SOURCE_LAST_PLAN: str = "borrowed from last plan"

TEST_BRIDGE_ONLY: str = "bridge_only"
TEST_SOLAR_HEADROOM: str = "solar_headroom"
TEST_DAILY_RATIO: str = "daily_ratio"

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
    soc_init: float
    now: datetime
    horizon_end: datetime
    step_hours: float
    samples: list[_Sample]
    blocks: list[_Block]
    cheap_threshold: float
    material_wh: float
    pv: Series
    price_proxied: bool
    pv_proxied: bool
    pv_unknown: bool
    load_source: str

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
    load_source: str = LOAD_SOURCE_PROFILE,
    previous: EndSocDecision | None = None,
) -> EndSocDecision:
    """Choose ``soc_final`` for one optimisation request.

    ``previous`` is the last decision (any mode), kept by the coordinator in
    memory only -- hysteresis needs no persistence, a restart just computes
    fresh.

    ``load_source`` describes where ``load`` came from -- this module has no
    way to tell on its own, since a borrowed series looks like any other.
    Recorded verbatim into the Optimized decision's ``details["load_tail"]``
    for the sensor; callers that always supply a real forecast can ignore it.
    """
    if mode == END_SOC_FIXED_50:
        soc = min(max(0.5, battery.soc_min), battery.soc_max)
        details: dict[str, Any] = {"mode": mode}
        if soc != 0.5:
            details["clamped_by"] = BOUND_RANGE
        return EndSocDecision(
            soc=soc,
            reason="Fixed at 50% by the End SOC setting.",
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


CANDIDATES: tuple[_Candidate, ...] = (
    _Candidate(TEST_BRIDGE_ONLY, "Test 1 -- bridge to the next refill", _bridge_only),
    _Candidate(TEST_SOLAR_HEADROOM, "Test 2 -- bridge floor, solar ceiling", _solar_headroom),
    _Candidate(TEST_DAILY_RATIO, "Test 3 -- daily-yield template", _daily_ratio),
)

ACTIVE_CANDIDATE: str = TEST_SOLAR_HEADROOM


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
        if price is None:
            # Prices repeat their daily *shape* (night trough, evening peak),
            # so before tomorrow's market publishes, today's curve shifted a
            # day is a usable proxy -- flagged, so nobody mistakes it for data.
            price = _real_value(buy_price, when - day)
            if price is not None:
                price_proxied = True

        samples.append(_Sample(when, pv_w, sample_proxied, load_w, price))
        when += step

    return _Tail(
        battery=battery,
        soc_init=soc_init,
        now=now,
        horizon_end=horizon_end,
        step_hours=step_hours,
        samples=samples,
        blocks=_surplus_blocks(samples, step_hours),
        cheap_threshold=_percentile(buy_price.values, CHEAP_PERCENTILE),
        material_wh=SURPLUS_MATERIAL_FRACTION * battery.capacity_wh,
        pv=pv,
        price_proxied=price_proxied,
        pv_proxied=pv_proxied,
        pv_unknown=pv_unknown,
        load_source=load_source,
    )


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

"""Decide the battery's end-of-horizon SOC target.

EMHASS pins the battery's end-of-horizon SOC to whatever ``soc_final`` a
request carries (softly from 0.18: deviation is penalised far above the
dearest import slot, so the target is honoured whenever physics allows). Its
API has no way to say "you decide" -- a terminal condition is always the
caller's job -- so this module is where the companion decides what stored
energy is worth having when the known world ends.

The Optimized mode's core idea: a kWh in the battery at horizon end is worth
what it displaces *after* the horizon. So look past the horizon with the
tails of the same forecasts the payload is built from, find the next moment
the battery could be refilled cheaply or for free (a PV surplus block or a
price trough), and hold exactly enough to bridge the house until then, on
top of the user's configured reserve. Everything here is pure computation on
:class:`~.models.Series`; fetching them is the coordinator's job.

See docs/end_soc_plan.md for the full design and the live A/B numbers that
motivated it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .const import END_SOC_FIXED_50, END_SOC_OPTIMIZED, END_SOC_SAME_AS_START
from .models import BatteryConfig, Series

# How far past the horizon the heuristic looks for a refill opportunity. One
# day always contains the next night's price trough and the next solar noon,
# and forecasts further out add error faster than information.
LOOKAHEAD: timedelta = timedelta(hours=24)

# A price at or below this percentile of the whole known buy-price series
# counts as "cheap enough to refill from the grid".
CHEAP_PERCENTILE: float = 0.25

# A PV surplus block only counts as a refill opportunity once its cumulative
# energy reaches this fraction of capacity -- a five-minute glint of sun
# through the clouds refills nothing.
SURPLUS_MATERIAL_FRACTION: float = 0.10

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
PV_TAIL_ZERO: str = "assumed zero"

# Where the load series came from, for the sensor -- this module cannot tell
# on its own, since a borrowed series looks like any other. See
# coordinator.EmhassCoordinator._load_for_terminal.
LOAD_SOURCE_PROFILE: str = "own forecast"
LOAD_SOURCE_LAST_PLAN: str = "borrowed from last plan"


@dataclass(slots=True)
class EndSocDecision:
    """A chosen end-of-horizon SOC and the explanation it needs to be trusted."""

    soc: float
    """Final fraction in [0, 1], all clamps applied."""
    reason: str
    """One human sentence for the sensor state's tooltip-level story."""
    details: dict[str, Any] = field(default_factory=dict)
    """Structured attributes for the End SOC target sensor."""


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
            details["clamped_by"] = "range"
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

    decision = _optimized(
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

    if (
        previous is not None
        and previous.details.get("mode") == END_SOC_OPTIMIZED
        and "fallback" not in previous.details
        and decision.soc != previous.soc
        and abs(decision.soc - previous.soc) <= HYSTERESIS_BAND
    ):
        decision.details["clamped_by"] = "hysteresis"
        decision.details["raw_target"] = round(decision.soc, 4)
        decision.soc = previous.soc
    return decision


def _optimized(
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
) -> EndSocDecision:
    step_hours = step.total_seconds() / 3600
    cheap_threshold = _percentile(buy_price.values, CHEAP_PERCENTILE)
    surplus_material_wh = SURPLUS_MATERIAL_FRACTION * battery.capacity_wh
    load_mean = sum(load.values) / len(load)

    # Walk the lookahead window step by step. value_at holds the last value
    # forever, so "real data" is bounded by series.end, not by a None return.
    times = _steps(horizon_end, horizon_end + LOOKAHEAD, step)
    price_proxied = False
    pv_assumed_zero = False

    replenish_at: datetime | None = None
    replenish_kind = REPLENISH_NONE
    bridge_wh = 0.0
    surplus_block_start: datetime | None = None
    surplus_block_wh = 0.0

    for when in times:
        pv_w = _real_value(pv, when)
        if pv_w is None:
            pv_w = 0.0
            pv_assumed_zero = True

        load_w = _real_value(load, when)
        if load_w is None:
            # The load profile is a daily pattern, so yesterday's same slot is
            # the honest statistical guess; the mean is the last resort.
            load_w = _real_value(load, when - timedelta(hours=24))
            if load_w is None:
                load_w = load_mean

        price = _real_value(buy_price, when)
        if price is None:
            # Prices repeat their daily *shape* (night trough, evening peak),
            # so before tomorrow's market publishes, today's curve shifted a
            # day is a usable proxy -- flagged, so nobody mistakes it for data.
            price = _real_value(buy_price, when - timedelta(hours=24))
            if price is not None:
                price_proxied = True

        if price is not None and price <= cheap_threshold:
            replenish_at = when
            replenish_kind = REPLENISH_CHEAP_GRID
            break

        surplus_w = pv_w - load_w
        if surplus_w > 0:
            if surplus_block_start is None:
                surplus_block_start = when
            surplus_block_wh += surplus_w * step_hours
            if surplus_block_wh >= surplus_material_wh:
                # The bridge only has to reach the block's *start*, and the
                # running sum already stops there by itself: every step inside
                # the block has pv > load, contributing zero deficit.
                replenish_at = surplus_block_start
                replenish_kind = REPLENISH_SOLAR
                break
        else:
            surplus_block_start = None
            surplus_block_wh = 0.0

        bridge_wh += max(0.0, load_w - pv_w) * step_hours

    if battery.discharge_efficiency > 0:
        bridge_wh /= battery.discharge_efficiency
    bridge_wh = max(0.0, bridge_wh)

    reserve = battery.soc_target
    raw = reserve + bridge_wh / battery.capacity_wh
    soc = min(max(raw, battery.soc_min), battery.soc_max)

    details: dict[str, Any] = {
        "mode": END_SOC_OPTIMIZED,
        "replenishment_kind": replenish_kind,
        "bridge_energy_wh": round(bridge_wh),
        "reserve": round(reserve, 4),
        "price_tail": PRICE_TAIL_PROXY if price_proxied else PRICE_TAIL_KNOWN,
        "pv_tail": PV_TAIL_ZERO if pv_assumed_zero else PV_TAIL_KNOWN,
        "load_tail": load_source,
    }
    if replenish_at is not None:
        details["next_replenishment"] = replenish_at.isoformat()
    if soc != raw:
        details["clamped_by"] = "range"
        details["raw_target"] = round(raw, 4)

    # Annotate, do not clamp: from EMHASS 0.18 an over-ambitious soc_final is
    # a soft constraint the solver approaches as closely as power limits
    # allow, warning rather than going infeasible. It knows the physics
    # better than a bound recomputed here would.
    horizon_hours = (horizon_end - now).total_seconds() / 3600
    if battery.capacity_wh > 0 and horizon_hours > 0:
        reach_up = battery.charge_power_max_w * horizon_hours / battery.capacity_wh
        reach_down = battery.discharge_power_max_w * horizon_hours / battery.capacity_wh
        if soc > soc_init + reach_up + 1e-6 or soc < soc_init - reach_down - 1e-6:
            details["unreachable"] = True

    return EndSocDecision(
        soc=round(soc, 4),
        reason=_reason(soc, reserve, bridge_wh, replenish_kind, replenish_at),
        details=details,
    )


def _reason(
    soc: float,
    reserve: float,
    bridge_wh: float,
    kind: str,
    replenish_at: datetime | None,
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


def _percentile(values: tuple[float, ...], fraction: float) -> float:
    """The value ``fraction`` of the way up the sorted values (no interpolation)."""
    ordered = sorted(values)
    index = int(fraction * (len(ordered) - 1))
    return ordered[max(0, min(len(ordered) - 1, index))]


def _steps(start: datetime, end: datetime, step: timedelta) -> list[datetime]:
    times: list[datetime] = []
    when = start
    while when < end:
        times.append(when)
        when += step
    return times


def _real_value(series: Series, when: datetime) -> float | None:
    """``value_at``, but only within the span the series actually covers.

    ``Series.value_at`` holds its last value forever forward; past the end
    that is exactly the flat-lined-forecast trap this module exists to avoid,
    so coverage is checked first.
    """
    if not series or not series.covers(when):
        return None
    return series.value_at(when)

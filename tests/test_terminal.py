"""Tests for the end-of-horizon SOC heuristic.

Each scenario is a day the pinned soc_final=soc_init behaviour got wrong; the
assertions state what the freed target should be instead. Times are UTC and
hourly to keep the arithmetic legible: capacity 10 kWh, so every 1000 Wh of
bridge energy is exactly 0.1 SOC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.emhass_companion.const import (
    END_SOC_FIXED_50,
    END_SOC_OPTIMIZED,
    END_SOC_SAME_AS_START,
)
from custom_components.emhass_companion.models import BatteryConfig, Point, Series
from custom_components.emhass_companion.terminal import (
    LOAD_SOURCE_LAST_PLAN,
    LOAD_SOURCE_PROFILE,
    PRICE_TAIL_PROXY,
    PV_TAIL_ZERO,
    REPLENISH_CHEAP_GRID,
    REPLENISH_NONE,
    REPLENISH_SOLAR,
    decide_end_soc,
)

HOUR = timedelta(hours=1)
T0 = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
HORIZON_END = T0 + timedelta(hours=24)


def _battery(**overrides) -> BatteryConfig:
    defaults = dict(
        enabled=True,
        capacity_wh=10_000,
        charge_power_max_w=5_000,
        discharge_power_max_w=5_000,
        soc_min=0.10,
        soc_max=0.90,
        soc_target=0.30,  # the Optimized reserve floor
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
    )
    defaults.update(overrides)
    return BatteryConfig(**defaults)


def _series(values: list[float], start: datetime = T0) -> Series:
    return Series(Point(start + i * HOUR, value) for i, value in enumerate(values))


def _decide(mode=END_SOC_OPTIMIZED, previous=None, **overrides):
    kwargs = dict(
        mode=mode,
        soc_init=0.50,
        battery=_battery(),
        now=T0,
        horizon_end=HORIZON_END,
        step=HOUR,
        pv=None,
        load=_series([1000.0] * 48),
        buy_price=_series([2.0] * 48),
        previous=previous,
    )
    kwargs.update(overrides)
    return decide_end_soc(**kwargs)


# --- passthrough modes -------------------------------------------------------


def test_same_as_start_pins_to_the_live_soc():
    decision = _decide(mode=END_SOC_SAME_AS_START, soc_init=0.62)
    assert decision.soc == 0.62
    assert decision.details["mode"] == END_SOC_SAME_AS_START


def test_an_unknown_mode_behaves_as_same_as_start():
    decision = _decide(mode="mode_from_the_future", soc_init=0.62)
    assert decision.soc == 0.62
    assert decision.details["mode"] == END_SOC_SAME_AS_START


def test_fixed_50_is_half():
    assert _decide(mode=END_SOC_FIXED_50).soc == 0.5


def test_fixed_50_respects_the_soc_range():
    decision = _decide(mode=END_SOC_FIXED_50, battery=_battery(soc_min=0.60))
    assert decision.soc == 0.60
    assert decision.details["clamped_by"] == "range"


# --- the optimized heuristic -------------------------------------------------


def test_cheap_night_past_the_horizon_frees_the_battery():
    """A price trough two hours past the horizon: hold reserve + two hours."""
    # Hours 0-11 cheap (in-horizon night), tail expensive until hour 26.
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    decision = _decide(buy_price=_series(prices))

    assert decision.details["replenishment_kind"] == REPLENISH_CHEAP_GRID
    # Bridge: hours 24 and 25 at 1000 W = 2000 Wh = 0.2 SOC on the reserve.
    assert decision.soc == pytest.approx(0.50)
    assert decision.details["bridge_energy_wh"] == 2000
    assert decision.details["next_replenishment"] == (HORIZON_END + 2 * HOUR).isoformat()


def test_flat_prices_mean_refill_anytime_so_hold_only_the_reserve():
    decision = _decide()
    assert decision.details["replenishment_kind"] == REPLENISH_CHEAP_GRID
    assert decision.soc == pytest.approx(0.30)


def test_a_sunny_morning_counts_as_the_refill():
    """PV surplus at tail hour 8 caps the bridge there, not at 24 h."""
    # Rising prices so no tail hour is "cheap": threshold lands in-horizon.
    prices = [1.0 + i * 0.01 for i in range(48)]
    pv = [0.0] * 32 + [5000.0] * 5 + [0.0] * 11
    decision = _decide(
        buy_price=_series(prices),
        pv=_series(pv),
        load=_series([500.0] * 48),
    )

    assert decision.details["replenishment_kind"] == REPLENISH_SOLAR
    # Bridge: tail hours 24-31 at 500 W = 4000 Wh = 0.4 SOC on the reserve.
    assert decision.soc == pytest.approx(0.70)
    assert decision.details["next_replenishment"] == (HORIZON_END + 8 * HOUR).isoformat()


def test_no_refill_in_sight_holds_the_whole_stretch_clamped_to_soc_max():
    prices = [1.0 + i * 0.01 for i in range(48)]
    decision = _decide(buy_price=_series(prices))

    assert decision.details["replenishment_kind"] == REPLENISH_NONE
    # 24 h at 1000 W = 24 kWh -- far past capacity, so the range clamp bites.
    assert decision.soc == 0.90
    assert decision.details["clamped_by"] == "range"


def test_a_missing_price_tail_borrows_todays_curve_and_says_so():
    """Before ~13:00 tomorrow's Nordpool prices do not exist yet."""
    # Only 24 h of prices: cheap night hours 0-5, expensive rest. The proxy
    # shifts them a day, so tail hour 24 looks like hour 0: cheap at once.
    prices = [1.0] * 6 + [2.0] * 18
    decision = _decide(buy_price=_series(prices))

    assert decision.details["price_tail"] == PRICE_TAIL_PROXY
    assert decision.details["replenishment_kind"] == REPLENISH_CHEAP_GRID
    assert decision.soc == pytest.approx(0.30)


def test_a_missing_pv_tail_assumes_darkness_and_says_so():
    decision = _decide()
    assert decision.details["pv_tail"] == PV_TAIL_ZERO


def test_load_source_defaults_to_the_profile():
    """The default when a caller doesn't say otherwise -- a real forecast."""
    decision = _decide()
    assert decision.details["load_tail"] == LOAD_SOURCE_PROFILE


def test_a_borrowed_load_source_is_recorded_verbatim():
    """This module cannot tell a borrowed series from the profile's own --
    the coordinator has to say so. See coordinator._load_for_terminal."""
    decision = _decide(load_source=LOAD_SOURCE_LAST_PLAN)
    assert decision.details["load_tail"] == LOAD_SOURCE_LAST_PLAN


def test_an_unreachable_target_is_annotated_not_clamped():
    """From EMHASS 0.18 the solver degrades softly; we only explain."""
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    decision = _decide(
        buy_price=_series(prices),
        soc_init=0.10,
        battery=_battery(charge_power_max_w=100),
    )

    assert decision.soc == pytest.approx(0.50)  # the target itself is untouched
    assert decision.details["unreachable"] is True


def test_hysteresis_holds_the_previous_target_against_jitter():
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    first = _decide(buy_price=_series(prices))
    assert first.soc == pytest.approx(0.50)

    # 1050 W load: bridge 2100 Wh -> 0.51, inside the 2 pp band.
    second = _decide(
        buy_price=_series(prices),
        load=_series([1050.0] * 48),
        previous=first,
    )
    assert second.soc == first.soc
    assert second.details["clamped_by"] == "hysteresis"

    # 1500 W load: bridge 3000 Wh -> 0.60, outside the band, moves freely.
    third = _decide(
        buy_price=_series(prices),
        load=_series([1500.0] * 48),
        previous=first,
    )
    assert third.soc == pytest.approx(0.60)


def test_missing_inputs_fall_back_to_the_start_soc_with_the_cause():
    decision = _decide(load=None, soc_init=0.42)
    assert decision.soc == 0.42
    assert "load forecast" in decision.details["fallback"]

    decision = _decide(buy_price=None, soc_init=0.42)
    assert decision.soc == 0.42
    assert "buy price" in decision.details["fallback"]


def test_a_fallback_decision_is_no_hysteresis_anchor():
    """A pin to the drifting live SOC must not hold the next real target."""
    fallback = _decide(load=None, soc_init=0.51)
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    decision = _decide(buy_price=_series(prices), previous=fallback)
    assert decision.soc == pytest.approx(0.50)
    assert decision.details.get("clamped_by") is None

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
    ACTIVE_CANDIDATE,
    CANDIDATES,
    LOAD_SOURCE_LAST_PLAN,
    LOAD_SOURCE_PROFILE,
    PRICE_TAIL_PROXY,
    PV_TAIL_PROXY,
    PV_TAIL_ZERO,
    REPLENISH_CHEAP_GRID,
    REPLENISH_NONE,
    REPLENISH_SOLAR,
    TEST_BRIDGE_ONLY,
    TEST_DAILY_RATIO,
    TEST_SOLAR_HEADROOM,
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


def test_fixed_50_uses_the_soc_target_slider():
    decision = _decide(mode=END_SOC_FIXED_50, battery=_battery(soc_target=0.42))
    assert decision.soc == 0.42
    assert "clamped_by" not in decision.details


def test_fixed_50_respects_the_soc_range():
    decision = _decide(mode=END_SOC_FIXED_50, battery=_battery(soc_target=0.30, soc_min=0.60))
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
    pv = [0.0] * 32 + [2000.0] * 3 + [0.0] * 13
    decision = _decide(
        buy_price=_series(prices),
        pv=_series(pv),
        load=_series([500.0] * 48),
    )

    assert decision.details["replenishment_kind"] == REPLENISH_SOLAR
    # Bridge: tail hours 24-31 at 500 W = 4000 Wh = 0.4 SOC on the reserve.
    assert decision.soc == pytest.approx(0.70)
    assert decision.details["next_replenishment"] == (HORIZON_END + 8 * HOUR).isoformat()
    # Surplus 3 h x 1500 W = 4500 Wh; the 4000 Wh bridge is spent before it
    # arrives, so only 500 Wh has to fit above the target -- a 0.85 ceiling,
    # well clear of the 0.70 floor.
    assert decision.details["surplus_ahead_wh"] == 4500
    assert decision.details["headroom_cap"] == pytest.approx(0.85)
    assert decision.details.get("clamped_by") is None


# --- solar headroom: the ceiling ---------------------------------------------


def test_a_big_solar_day_empties_the_battery_down_to_the_reserve():
    """Self-consumption first: 22 kWh of sun into a 10 kWh battery needs room.

    Holding the bridge energy would mean exporting that much more of the
    surplus at the sell price and buying it back at the buy price, so the
    ceiling wins all the way down to the user's reserve.
    """
    prices = [1.0 + i * 0.01 for i in range(48)]
    pv = [0.0] * 32 + [5000.0] * 5 + [0.0] * 11
    decision = _decide(
        buy_price=_series(prices),
        pv=_series(pv),
        load=_series([500.0] * 48),
    )

    assert decision.soc == pytest.approx(0.30)
    assert decision.details["clamped_by"] == "reserve"
    assert decision.details["raw_target"] == pytest.approx(0.70)
    assert decision.details["surplus_ahead_wh"] == 22500


def test_a_moderate_surplus_caps_the_target_between_bridge_and_reserve():
    prices = [1.0 + i * 0.01 for i in range(48)]
    # 4 h at 3200 W against a 500 W load: 10.8 kWh of surplus.
    pv = [0.0] * 32 + [3200.0] * 4 + [0.0] * 12
    decision = _decide(
        buy_price=_series(prices),
        pv=_series(pv),
        load=_series([500.0] * 48),
    )

    # Bridge target 0.70; 10 800 Wh of surplus less the 4000 Wh drawn down
    # before it arrives leaves 0.9 - 0.68 = 0.22 of room... capped at the
    # 0.30 reserve.
    assert decision.details["headroom_cap"] == pytest.approx(0.22)
    assert decision.soc == pytest.approx(0.30)
    assert decision.details["clamped_by"] == "reserve"


def test_a_surplus_smaller_than_the_bridge_never_lowers_the_target():
    """The battery is empty by sunrise anyway -- all of it fits."""
    prices = [1.0 + i * 0.01 for i in range(48)]
    pv = [0.0] * 32 + [1500.0] * 2 + [0.0] * 14
    decision = _decide(
        buy_price=_series(prices),
        pv=_series(pv),
        load=_series([500.0] * 48),
    )

    assert decision.details["surplus_ahead_wh"] == 2000
    assert decision.details["headroom_cap"] == pytest.approx(0.90)
    assert decision.soc == pytest.approx(0.70)


def test_winter_has_no_ceiling_and_lets_price_decide():
    """Too little sun to make a material block: price alone sets the floor."""
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    pv = [0.0] * 30 + [600.0] * 4 + [0.0] * 14  # peaks below the 1000 W load
    decision = _decide(buy_price=_series(prices), pv=_series(pv))

    assert decision.details["replenishment_kind"] == REPLENISH_CHEAP_GRID
    assert "headroom_cap" not in decision.details
    assert decision.soc == pytest.approx(0.50)


# --- the previous-day PV proxy -----------------------------------------------


def test_pv_past_the_forecast_reuses_the_previous_day_at_half_weight():
    """The common case: a today+tomorrow forecast and a horizon that outruns it."""
    # PV known for 30 h only, with a 4 kW block at hours 8-13.
    pv = [0.0] * 8 + [4000.0] * 6 + [0.0] * 16
    decision = _decide(
        pv=_series(pv),
        load=_series([500.0] * 48),
    )

    assert decision.details["pv_tail"] == PV_TAIL_PROXY
    # Hours 32-37 mirror hours 8-13 at half weight: 6 h x 1500 W = 9000 Wh.
    assert decision.details["surplus_ahead_wh"] == 9000
    assert decision.details["replenishment_kind"] == REPLENISH_SOLAR
    assert decision.details["replenishment_guessed"] is True
    assert decision.details["next_replenishment"] == (HORIZON_END + 8 * HOUR).isoformat()
    # Bridge 8 h x 500 W = 4000 Wh -> 0.70; ceiling 0.9 - (9000-4000)/10000.
    assert decision.soc == pytest.approx(0.40)
    assert decision.details["clamped_by"] == "solar_headroom"


def test_a_guessed_sunrise_may_never_shorten_the_bridge():
    """Sun inferred from yesterday can push the refill later, never earlier."""
    # Cheap only from tail hour 12 (36), after the guessed block at hour 32.
    prices = [2.0] * 36 + [1.0] * 12
    pv = [0.0] * 8 + [4000.0] * 6 + [0.0] * 16
    decision = _decide(
        buy_price=_series(prices),
        pv=_series(pv),
        load=_series([500.0] * 48),
    )

    assert decision.details["replenishment_kind"] == REPLENISH_CHEAP_GRID
    assert "replenishment_guessed" not in decision.details
    assert decision.details["next_replenishment"] == (HORIZON_END + 12 * HOUR).isoformat()
    # The guess is still trusted for the ceiling, where being wrong only
    # costs a partial charge: 0.9 - (9000 - 4000) / 10000.
    assert decision.soc == pytest.approx(0.40)


def test_solar_wins_over_an_earlier_cheap_price():
    """Free PV always outranks grid price, however soon the price dips."""
    # Same price curve as the cheap-night test: cheap at tail hour 2 (26).
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    # A material PV block starts at tail hour 5 (29) -- later than the price
    # dip, but it should still win outright.
    pv = [0.0] * 29 + [5000.0] + [0.0] * 18
    decision = _decide(buy_price=_series(prices), pv=_series(pv))

    assert decision.details["replenishment_kind"] == REPLENISH_SOLAR
    assert decision.details["next_replenishment"] == (HORIZON_END + 5 * HOUR).isoformat()
    # Bridge: tail hours 24-28 (5 h) at 1000 W = 5000 Wh = 0.5 SOC on the
    # reserve -- the cheap grid hour along the way is not used as a shortcut.
    assert decision.details["bridge_energy_wh"] == 5000
    assert decision.soc == pytest.approx(0.80)


def test_a_weak_pv_blip_does_not_preempt_the_cheap_price_fallback():
    """Winter: a PV glint too small to matter still falls back to price."""
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    # Surplus of 200 W for one hour = 200 Wh, short of the 1000 Wh (10 % of
    # 10 kWh) material threshold, and it never accumulates further.
    pv = [0.0] * 27 + [1200.0] + [0.0] * 20
    decision = _decide(buy_price=_series(prices), pv=_series(pv))

    assert decision.details["replenishment_kind"] == REPLENISH_CHEAP_GRID
    assert decision.soc == pytest.approx(0.50)
    assert decision.details["bridge_energy_wh"] == 2000


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


# --- the candidate registry --------------------------------------------------


def _big_solar_day():
    """22.5 kWh of surplus into a 10 kWh battery -- the two ideas disagree."""
    return _decide(
        buy_price=_series([1.0 + i * 0.01 for i in range(48)]),
        pv=_series([0.0] * 32 + [5000.0] * 5 + [0.0] * 11),
        load=_series([500.0] * 48),
    )


def test_every_candidate_reports_its_own_answer():
    decision = _big_solar_day()
    tests = decision.details["tests"]

    assert set(tests) == {candidate.key for candidate in CANDIDATES}
    for candidate in CANDIDATES:
        entry = tests[candidate.key]
        assert entry["label"] == candidate.label
        assert 0.0 <= entry["soc"] <= 1.0
        assert entry["reason"]


def test_the_active_candidate_is_the_one_that_drives_the_plan():
    decision = _big_solar_day()
    assert decision.details["active_test"] == ACTIVE_CANDIDATE
    assert decision.details["tests"][ACTIVE_CANDIDATE]["soc"] == decision.soc


def test_the_shipped_calculation_rides_along_as_test_1():
    """Test 1 can only add to the reserve, so it hoards through a sunny day."""
    tests = _big_solar_day().details["tests"]

    # Reserve 0.30 plus an 8 h, 4000 Wh bridge to the sun at tail hour 8.
    assert tests[TEST_BRIDGE_ONLY]["soc"] == pytest.approx(0.70)
    # The ceiling sees 22.5 kWh of surplus arriving into a 10 kWh battery.
    assert tests[TEST_SOLAR_HEADROOM]["soc"] == pytest.approx(0.30)


def test_the_template_candidate_reads_daily_yields():
    """Test 3 ports the Jinja template: drift on tomorrow-minus-today."""
    # T0 is 12:00 UTC, so "today" catches hours 0-11 and "tomorrow" 12-35.
    pv = [1000.0] * 4 + [0.0] * 20 + [2000.0] * 6 + [0.0] * 18
    decision = _decide(pv=_series(pv))

    # today 4 kWh, tomorrow 12 kWh -> drift +0.2 * 0.4 on a 0.50 start SOC;
    # the sliding floor is 0.5 - (12/40) * 0.4 = 0.38, so the drift wins.
    assert decision.details["tests"][TEST_DAILY_RATIO]["soc"] == pytest.approx(0.58)


def test_the_candidate_answers_are_recorded_before_hysteresis():
    """The pin may be held back; what each idea actually computed is not."""
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    first = _decide(buy_price=_series(prices))
    second = _decide(
        buy_price=_series(prices),
        load=_series([1050.0] * 48),
        previous=first,
    )

    assert second.soc == first.soc
    assert second.details["tests"][ACTIVE_CANDIDATE]["soc"] == pytest.approx(0.51)

"""Tests for the end-of-horizon SOC heuristic.

Each scenario is a day the pinned soc_final=soc_init behaviour got wrong; the
assertions state what the freed target should be instead. Times are UTC and
hourly to keep the arithmetic legible: capacity 10 kWh, so every 1000 Wh of
cover energy is exactly 0.1 SOC.

Every candidate runs on every call, but only :data:`ACTIVE_CANDIDATE` reaches
``decision.soc`` and ``decision.details``. Scenarios that check one candidate's
own arithmetic say which with the ``active`` fixture, so a scenario reads as a
claim about a named rule rather than about whichever rule happens to ship.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.emhass_companion import terminal
from custom_components.emhass_companion.const import (
    END_SOC_FIXED_50,
    END_SOC_OPTIMIZED,
    END_SOC_SAME_AS_START,
)
from custom_components.emhass_companion.models import (
    BatteryConfig,
    GridConfig,
    Point,
    Series,
)
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
    SALE_NO_MARGIN,
    SALE_NO_SELL_PRICE,
    SALE_PRICES_UNPUBLISHED,
    TEST_BRIDGE_ONLY,
    TEST_DAILY_RATIO,
    TEST_NIGHT_COVER,
    TEST_PRICED_BRIDGE,
    TEST_SOLAR_HEADROOM,
    decide_end_soc,
)

HOUR = timedelta(hours=1)
T0 = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
HORIZON_END = T0 + timedelta(hours=24)


@pytest.fixture
def active(monkeypatch):
    """Point ACTIVE_CANDIDATE at the rule a scenario is actually about."""

    def _use(key: str) -> None:
        monkeypatch.setattr(terminal, "ACTIVE_CANDIDATE", key)

    return _use


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


def test_cheap_night_past_the_horizon_frees_the_battery(active):
    """A price trough two hours past the horizon: hold reserve + two hours."""
    active(TEST_BRIDGE_ONLY)
    # Hours 0-11 cheap (in-horizon night), tail expensive until hour 26.
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    decision = _decide(buy_price=_series(prices))

    assert decision.details["replenishment_kind"] == REPLENISH_CHEAP_GRID
    # Bridge: hours 24 and 25 at 1000 W = 2000 Wh = 0.2 SOC on the reserve.
    assert decision.soc == pytest.approx(0.50)
    assert decision.details["bridge_energy_wh"] == 2000
    assert decision.details["next_replenishment"] == (HORIZON_END + 2 * HOUR).isoformat()


def test_flat_prices_mean_refill_anytime_so_hold_only_the_reserve(active):
    active(TEST_BRIDGE_ONLY)
    decision = _decide()
    assert decision.details["replenishment_kind"] == REPLENISH_CHEAP_GRID
    assert decision.soc == pytest.approx(0.30)


def test_a_sunny_morning_counts_as_the_refill(active):
    """PV surplus at tail hour 8 caps the bridge there, not at 24 h."""
    active(TEST_SOLAR_HEADROOM)
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


def test_a_big_solar_day_empties_the_battery_down_to_the_reserve(active):
    """Self-consumption first: 22 kWh of sun into a 10 kWh battery needs room.

    Holding the bridge energy would mean exporting that much more of the
    surplus at the sell price and buying it back at the buy price, so the
    ceiling wins all the way down to the user's reserve.
    """
    active(TEST_SOLAR_HEADROOM)
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


def test_a_moderate_surplus_caps_the_target_between_bridge_and_reserve(active):
    active(TEST_SOLAR_HEADROOM)
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


def test_a_surplus_smaller_than_the_bridge_never_lowers_the_target(active):
    """The battery is empty by sunrise anyway -- all of it fits."""
    active(TEST_SOLAR_HEADROOM)
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


def test_winter_has_no_ceiling_and_lets_price_decide(active):
    """Too little sun to make a material block: price alone sets the floor."""
    active(TEST_SOLAR_HEADROOM)
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    pv = [0.0] * 30 + [600.0] * 4 + [0.0] * 14  # peaks below the 1000 W load
    decision = _decide(buy_price=_series(prices), pv=_series(pv))

    assert decision.details["replenishment_kind"] == REPLENISH_CHEAP_GRID
    assert "headroom_cap" not in decision.details
    assert decision.soc == pytest.approx(0.50)


# --- the previous-day PV proxy -----------------------------------------------


def test_pv_past_the_forecast_reuses_the_previous_day_at_half_weight(active):
    """The common case: a today+tomorrow forecast and a horizon that outruns it."""
    active(TEST_SOLAR_HEADROOM)
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


def test_a_guessed_sunrise_may_never_shorten_the_bridge(active):
    """Sun inferred from yesterday can push the refill later, never earlier."""
    active(TEST_SOLAR_HEADROOM)
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


def test_solar_wins_over_an_earlier_cheap_price(active):
    """Free PV always outranks grid price, however soon the price dips."""
    active(TEST_SOLAR_HEADROOM)
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


def test_a_weak_pv_blip_does_not_preempt_the_cheap_price_fallback(active):
    """Winter: a PV glint too small to matter still falls back to price."""
    active(TEST_SOLAR_HEADROOM)
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    # Surplus of 200 W for one hour = 200 Wh, short of the 1000 Wh (10 % of
    # 10 kWh) material threshold, and it never accumulates further.
    pv = [0.0] * 27 + [1200.0] + [0.0] * 20
    decision = _decide(buy_price=_series(prices), pv=_series(pv))

    assert decision.details["replenishment_kind"] == REPLENISH_CHEAP_GRID
    assert decision.soc == pytest.approx(0.50)
    assert decision.details["bridge_energy_wh"] == 2000


def test_no_refill_in_sight_holds_the_whole_stretch_clamped_to_soc_max(active):
    active(TEST_BRIDGE_ONLY)
    prices = [1.0 + i * 0.01 for i in range(48)]
    decision = _decide(buy_price=_series(prices))

    assert decision.details["replenishment_kind"] == REPLENISH_NONE
    # 24 h at 1000 W = 24 kWh -- far past capacity, so the range clamp bites.
    assert decision.soc == 0.90
    assert decision.details["clamped_by"] == "range"


def test_a_missing_price_tail_borrows_todays_curve_and_says_so(active):
    """Before ~13:00 tomorrow's Nordpool prices do not exist yet."""
    active(TEST_BRIDGE_ONLY)
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


def test_an_unreachable_target_is_annotated_not_clamped(active):
    """From EMHASS 0.18 the solver degrades softly; we only explain."""
    active(TEST_BRIDGE_ONLY)
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    decision = _decide(
        buy_price=_series(prices),
        soc_init=0.10,
        battery=_battery(charge_power_max_w=100),
    )

    assert decision.soc == pytest.approx(0.50)  # the target itself is untouched
    assert decision.details["unreachable"] is True


def test_hysteresis_holds_the_previous_target_against_jitter(active):
    active(TEST_BRIDGE_ONLY)
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


def test_a_fallback_decision_is_no_hysteresis_anchor(active):
    """A pin to the drifting live SOC must not hold the next real target."""
    active(TEST_BRIDGE_ONLY)
    fallback = _decide(load=None, soc_init=0.51)
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    decision = _decide(buy_price=_series(prices), previous=fallback)
    assert decision.soc == pytest.approx(0.50)
    assert decision.details.get("clamped_by") is None


# --- test 4: the night cover, the shipping rule ------------------------------


def _cover_day(**overrides):
    """A pin at dusk with sunrise 8 tail hours out and a big day behind it.

    Rising prices so nothing reads as cheap; the sun is the only refill.
    """
    kwargs = dict(
        buy_price=_series([1.0 + i * 0.01 for i in range(48)]),
        pv=_series([0.0] * 32 + [5000.0] * 5 + [0.0] * 11),
        load=_series([500.0] * 48),
    )
    kwargs.update(overrides)
    return _decide(**kwargs)


def test_the_night_cover_carries_what_the_house_needs_until_sunrise():
    """8 tail hours at 500 W = 4000 Wh on top of the reserve."""
    decision = _cover_day()

    assert decision.details["active_test"] == TEST_NIGHT_COVER
    assert decision.soc == pytest.approx(0.70)
    assert decision.details["cover_energy_wh"] == 4000
    assert decision.details["replenishment_kind"] == REPLENISH_SOLAR
    assert decision.details["cover_until"] == (HORIZON_END + 8 * HOUR).isoformat()


def test_a_surplus_too_big_to_absorb_no_longer_empties_the_battery():
    """The regression this rule exists for.

    22.5 kWh of sun against a 10 kWh battery makes Test 2's ceiling
    unsatisfiable, and it collapses to the reserve -- planning an overnight
    import to make room for surplus that would refill the battery by mid-
    morning regardless. The night comes first.
    """
    decision = _cover_day()
    tests = decision.details["tests"]

    assert tests[TEST_SOLAR_HEADROOM]["soc"] == pytest.approx(0.30)
    assert tests[TEST_NIGHT_COVER]["soc"] == pytest.approx(0.70)
    assert decision.soc == pytest.approx(0.70)


def test_sun_arriving_at_the_pin_needs_no_cover_at_all():
    """The other end of the same rule: at dawn, ending on the reserve is right."""
    prices = [1.0 + i * 0.01 for i in range(48)]
    # Surplus from the first tail hour onwards.
    pv = _series([0.0] * 24 + [5000.0] * 8 + [0.0] * 16)
    decision = _decide(buy_price=_series(prices), pv=pv, load=_series([500.0] * 48))

    assert decision.soc == pytest.approx(0.30)
    assert decision.details["cover_energy_wh"] == 0
    assert "cover_until" not in decision.details


def test_a_pin_inside_a_weak_morning_still_carries_the_night_behind_it():
    """What the backward walk buys over Test 1's bridge.

    The sun is already up at the pin, so Test 1 calls the bridge zero and hands
    back the bare reserve -- selling off the morning's own charge before a night
    the day cannot pay for. The walk credits the day's 2 kWh against the 6 kWh
    night that follows and carries the difference.
    """
    prices = [1.0 + i * 0.01 for i in range(48)]
    # Tail hours 0-3: 1000 W against a 500 W load = 2000 Wh banked. Then 12 h
    # of darkness at 500 W = 6000 Wh, and sunrise again at tail hour 16.
    pv = _series([0.0] * 24 + [1000.0] * 4 + [0.0] * 12 + [4000.0] * 5 + [0.0] * 3)
    decision = _decide(buy_price=_series(prices), pv=pv, load=_series([500.0] * 48))
    tests = decision.details["tests"]

    assert tests[TEST_BRIDGE_ONLY]["soc"] == pytest.approx(0.30)
    # 6000 Wh of night less the 2000 Wh the weak morning banks = 4000 Wh.
    assert decision.details["cover_energy_wh"] == 4000
    assert decision.soc == pytest.approx(0.70)


def test_darkness_past_the_last_sunrise_is_not_a_night_to_cover():
    """The lookahead's own edge must not read as an endless night.

    Every hour after the final surplus block is darkness with no visible end,
    which is a property of a 24 h window rather than of the world. Left in, it
    would propagate back and pin every sunny day at soc_max.
    """
    decision = _cover_day()

    # 11 dark tail hours follow the block; only the 8 before it are covered.
    assert decision.details["cover_energy_wh"] == 4000
    assert decision.soc == pytest.approx(0.70)


def test_a_sunless_lookahead_ends_the_night_at_the_cheapest_hour():
    """December: no sun to make room for, so the grid is allowed to refill.

    Without this the cover would run the whole 24 h and pin at soc_max, which
    is the hoarding the feature exists to end.
    """
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    decision = _decide(buy_price=_series(prices))

    assert decision.details["replenishment_kind"] == REPLENISH_CHEAP_GRID
    # Tail hours 24 and 25 at 1000 W before the trough at hour 26.
    assert decision.details["cover_energy_wh"] == 2000
    assert decision.soc == pytest.approx(0.50)


def test_a_sunless_lookahead_with_no_trough_holds_the_whole_stretch():
    prices = [1.0 + i * 0.01 for i in range(48)]
    decision = _decide(buy_price=_series(prices))

    assert decision.details["replenishment_kind"] == REPLENISH_NONE
    assert decision.soc == 0.90
    assert decision.details["clamped_by"] == "range"


def test_a_guessed_sunrise_may_not_shorten_the_cover_either():
    """Proxied PV is darkness to the walk, as it is to Test 1's bridge."""
    prices = [1.0 + i * 0.01 for i in range(48)]
    # PV known for 30 h only; the tail's sun is the previous day's shape.
    pv = _series([0.0] * 8 + [4000.0] * 6 + [0.0] * 16)
    decision = _decide(buy_price=_series(prices), pv=pv, load=_series([500.0] * 48))

    assert decision.details["pv_tail"] == PV_TAIL_PROXY
    # No forecast block anywhere, and no cheap hour either: the whole stretch.
    assert decision.details["replenishment_kind"] == REPLENISH_NONE
    assert decision.soc == 0.90


# --- test 4: the priced exception --------------------------------------------


def _sale_day(sell: list[float] | None = None, **overrides):
    """The cover day again, with a sell curve and a published overnight trough.

    Buy prices: 1.00 through the horizon, then a 0.20 trough over the night,
    so a sale has something genuinely cheap to buy back at.
    """
    prices = [1.0] * 24 + [0.20] * 8 + [2.0] * 16
    kwargs = dict(
        buy_price=_series(prices),
        sell_price=_series(sell if sell is not None else [3.0] * 48),
        grid=GridConfig(export_max_w=1000.0),
        pv=_series([0.0] * 32 + [5000.0] * 5 + [0.0] * 11),
        load=_series([500.0] * 48),
    )
    kwargs.update(overrides)
    return _decide(**kwargs)


def test_a_peak_worth_more_than_buying_it_back_sells_part_of_the_cover():
    """3.00 to sell against 0.20 to buy back clears any wear: the trade is on.

    Capped by what can physically leave: 1 kW of export over the 24 horizon
    hours = 24 kWh offered, more than the 4 kWh cover, so the cover caps it.
    """
    decision = _sale_day()

    assert decision.details["sale_energy_wh"] == 4000
    assert decision.details["sale_buy_price"] == pytest.approx(0.20)
    assert decision.details["sale_sell_price"] == pytest.approx(3.0)
    assert decision.details["clamped_by"] == "profitable_sale"
    assert decision.soc == pytest.approx(0.30)


def test_a_sale_releases_only_what_fits_in_the_profitable_hours():
    """One hour above the bar at 1 kW releases 1 kWh, not the night."""
    decision = _sale_day(sell=[3.0] + [0.10] * 47)

    assert decision.details["sale_energy_wh"] == 1000
    assert decision.soc == pytest.approx(0.60)  # 0.70 cover less 0.1
    assert decision.details["clamped_by"] == "profitable_sale"


def test_a_spread_that_does_not_clear_the_wear_sells_nothing():
    """The summer case on this house's tariff: the cover is carried whole."""
    decision = _sale_day(
        sell=[0.25] * 48,
        battery=_battery(weight_battery_discharge=0.10, weight_battery_charge=0.0),
    )

    assert decision.details["sale_blocked"] == SALE_NO_MARGIN
    assert decision.soc == pytest.approx(0.70)


def test_a_proxied_overnight_price_may_never_talk_the_cover_down():
    """A spread against a guessed market is the calculation to refuse.

    Prices published for the horizon only, so the whole night is yesterday's
    curve shifted -- and a sale priced against it would empty the battery on
    the strength of a market that has not opened.
    """
    decision = _sale_day(buy_price=_series([1.0] * 12 + [0.20] * 12))

    assert decision.details["price_tail"] == PRICE_TAIL_PROXY
    assert decision.details["sale_blocked"] == SALE_PRICES_UNPUBLISHED
    assert decision.soc == pytest.approx(0.70)


def test_no_sell_price_means_the_cover_is_carried_whole():
    decision = _cover_day()
    assert decision.details["sale_blocked"] == SALE_NO_SELL_PRICE
    assert decision.soc == pytest.approx(0.70)


# --- test 5: the priced bridge and its curtailment ceiling -------------------


def test_the_priced_bridge_lets_a_published_cheap_hour_end_the_night(active):
    """Test 4 carries the night past a trough; Test 5 stops there.

    The difference the shadow column is there to measure.
    """
    # A published trough at tail hours 4 and 5, sunrise at tail hour 8. The
    # in-horizon night sets the cheap threshold at 0.50, so the 2.00 tail
    # hours either side of the trough are not refills themselves.
    prices = [0.5] * 24 + [2.0] * 4 + [0.20] * 2 + [2.0] * 18
    scenario = dict(
        buy_price=_series(prices),
        pv=_series([0.0] * 32 + [5000.0] * 5 + [0.0] * 11),
        load=_series([500.0] * 48),
    )
    tests = _decide(**scenario).details["tests"]

    # Test 4 covers all 8 hours to sunrise; Test 5 stops at the trough four
    # hours in and covers 2000 Wh.
    assert tests[TEST_NIGHT_COVER]["soc"] == pytest.approx(0.70)
    assert tests[TEST_PRICED_BRIDGE]["soc"] == pytest.approx(0.50)

    active(TEST_PRICED_BRIDGE)
    assert _decide(**scenario).details["cheap_refills"] == 2


def test_a_proxied_trough_is_no_refill_for_the_priced_bridge_either(active):
    active(TEST_PRICED_BRIDGE)
    # Published for the horizon only: the tail's troughs are all yesterday's.
    decision = _decide(
        buy_price=_series([1.0] * 12 + [0.20] * 12),
        pv=_series([0.0] * 32 + [5000.0] * 5 + [0.0] * 11),
        load=_series([500.0] * 48),
    )

    assert decision.details["cheap_refills"] == 0
    assert decision.soc == pytest.approx(0.70)


def test_surplus_that_merely_exports_leaves_the_ceiling_inert(active):
    """Sold is not lost. A 5 kW block under a 9 kW export limit costs nothing."""
    active(TEST_PRICED_BRIDGE)
    decision = _decide(
        buy_price=_series([1.0 + i * 0.01 for i in range(48)]),
        sell_price=_series([1.0] * 48),
        grid=GridConfig(export_max_w=9000.0),
        pv=_series([0.0] * 32 + [5000.0] * 5 + [0.0] * 11),
        load=_series([500.0] * 48),
    )

    assert decision.details["lost_surplus_wh"] == 0
    assert "curtailment_cap" not in decision.details
    assert decision.soc == pytest.approx(0.70)


def test_surplus_above_the_export_limit_makes_room_for_itself(active):
    """Clipped power is genuinely lost, so here the ceiling earns its keep."""
    active(TEST_PRICED_BRIDGE)
    decision = _decide(
        buy_price=_series([1.0 + i * 0.01 for i in range(48)]),
        sell_price=_series([1.0] * 48),
        grid=GridConfig(export_max_w=1500.0),
        pv=_series([0.0] * 32 + [5000.0] * 5 + [0.0] * 11),
        load=_series([500.0] * 48),
    )

    # 5 h clipped at 5000 - 500 - 1500 = 3000 W = 15 kWh thrown away, against
    # a 4 kWh drawdown: room for 0.9 - 1.1, under the reserve.
    assert decision.details["lost_surplus_wh"] == 15000
    assert decision.details["clamped_by"] == "reserve"
    assert decision.soc == pytest.approx(0.30)


def test_a_giveaway_hour_counts_as_lost_however_small(active):
    """Nothing to earn from exporting at zero, so all of it wants a home."""
    active(TEST_PRICED_BRIDGE)
    decision = _decide(
        buy_price=_series([1.0 + i * 0.01 for i in range(48)]),
        sell_price=_series([1.0] * 33 + [-0.5] * 2 + [1.0] * 13),
        grid=GridConfig(export_max_w=9000.0),
        pv=_series([0.0] * 32 + [5000.0] * 5 + [0.0] * 11),
        load=_series([500.0] * 48),
    )

    # Two hours at 4500 W of surplus given away.
    assert decision.details["lost_surplus_wh"] == 9000
    assert decision.details["curtailment_cap"] == pytest.approx(0.40)
    assert decision.soc == pytest.approx(0.40)


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


def test_the_candidate_answers_are_recorded_before_hysteresis(active):
    """The pin may be held back; what each idea actually computed is not."""
    active(TEST_BRIDGE_ONLY)
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    first = _decide(buy_price=_series(prices))
    second = _decide(
        buy_price=_series(prices),
        load=_series([1050.0] * 48),
        previous=first,
    )

    assert second.soc == first.soc
    assert second.details["tests"][ACTIVE_CANDIDATE]["soc"] == pytest.approx(0.51)

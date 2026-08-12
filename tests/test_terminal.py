"""Tests for the end-of-horizon SOC heuristic.

Each scenario is a day the pinned soc_final=soc_init behaviour got wrong; the
assertions state what the freed target should be instead. Times are UTC and
hourly to keep the arithmetic legible: capacity 10 kWh, so every 1000 Wh of
cover energy is exactly 0.1 SOC.

Every candidate runs on every call, but only :data:`ACTIVE_CANDIDATE` reaches
``decision.soc`` and ``decision.details``. Scenarios that check one candidate's
own arithmetic say which with the ``active`` fixture, so a scenario reads as a
claim about a named rule rather than about whichever rule happens to ship --
which matters most for the rules that do *not* ship, and matters again every
time the active one changes.

Scenarios about the shared machinery -- the price and PV proxies, the load
source, hysteresis, the unreachable annotation -- pin the night cover as the
active rule too. Any candidate would exercise those paths; naming one keeps the
expected numbers arithmetic rather than whatever the shipping rule happens to
compute this month.
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
    PRICE_MODEL_TARIFF,
    PRICE_MODEL_UNTRUSTED,
    PRICE_TAIL_PROXY,
    PV_TAIL_PROXY,
    PV_TAIL_ZERO,
    REPLENISH_CHEAP_GRID,
    REPLENISH_NONE,
    REPLENISH_SOLAR,
    SALE_NO_MARGIN,
    SALE_NO_SELL_PRICE,
    SALE_PRICES_UNPUBLISHED,
    TEST_DAILY_RATIO,
    TEST_NIGHT_COVER,
    TEST_PRICED_COVER,
    TEST_SHADOW_PLAN,
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


def test_flat_prices_mean_refill_anytime_so_hold_only_the_reserve(active):
    active(TEST_NIGHT_COVER)
    decision = _decide()
    assert decision.details["replenishment_kind"] == REPLENISH_CHEAP_GRID
    assert decision.soc == pytest.approx(0.30)


# --- the previous-day PV proxy -----------------------------------------------


def test_solar_wins_over_an_earlier_cheap_price(active):
    """Free PV always outranks grid price, however soon the price dips."""
    active(TEST_NIGHT_COVER)
    # Same price curve as the cheap-night test: cheap at tail hour 2 (26).
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    # A material PV block starts at tail hour 5 (29) -- later than the price
    # dip, but it should still win outright.
    pv = [0.0] * 29 + [5000.0] + [0.0] * 18
    decision = _decide(buy_price=_series(prices), pv=_series(pv))

    assert decision.details["replenishment_kind"] == REPLENISH_SOLAR
    assert decision.details["cover_until"] == (HORIZON_END + 5 * HOUR).isoformat()
    # Cover: tail hours 24-28 (5 h) at 1000 W = 5000 Wh = 0.5 SOC on the
    # reserve -- the cheap grid hour along the way is not used as a shortcut.
    assert decision.details["cover_energy_wh"] == 5000
    assert decision.soc == pytest.approx(0.80)


def test_a_weak_pv_blip_does_not_preempt_the_cheap_price_fallback(active):
    """Winter: a PV glint too small to matter still falls back to price."""
    active(TEST_NIGHT_COVER)
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    # Surplus of 200 W for one hour = 200 Wh, short of the 1000 Wh (10 % of
    # 10 kWh) material threshold, and it never accumulates further.
    pv = [0.0] * 27 + [1200.0] + [0.0] * 20
    decision = _decide(buy_price=_series(prices), pv=_series(pv))

    assert decision.details["replenishment_kind"] == REPLENISH_CHEAP_GRID
    assert decision.soc == pytest.approx(0.50)
    assert decision.details["cover_energy_wh"] == 2000


def test_a_missing_price_tail_borrows_todays_curve_and_says_so(active):
    """Before ~13:00 tomorrow's Nordpool prices do not exist yet."""
    active(TEST_NIGHT_COVER)
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
    active(TEST_NIGHT_COVER)
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    decision = _decide(
        buy_price=_series(prices),
        soc_init=0.10,
        battery=_battery(charge_power_max_w=100),
    )

    assert decision.soc == pytest.approx(0.50)  # the target itself is untouched
    assert decision.details["unreachable"] is True


def test_hysteresis_holds_the_previous_target_against_jitter(active):
    active(TEST_NIGHT_COVER)
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
    active(TEST_NIGHT_COVER)
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


def test_the_night_cover_carries_what_the_house_needs_until_sunrise(active):
    """8 tail hours at 500 W = 4000 Wh on top of the reserve."""
    active(TEST_NIGHT_COVER)
    decision = _cover_day()

    assert decision.details["active_test"] == TEST_NIGHT_COVER
    assert decision.soc == pytest.approx(0.70)
    assert decision.details["cover_energy_wh"] == 4000
    assert decision.details["replenishment_kind"] == REPLENISH_SOLAR
    assert decision.details["cover_until"] == (HORIZON_END + 8 * HOUR).isoformat()


def test_sun_arriving_at_the_pin_needs_no_cover_at_all(active):
    """The other end of the same rule: at dawn, ending on the reserve is right."""
    active(TEST_NIGHT_COVER)
    prices = [1.0 + i * 0.01 for i in range(48)]
    # Surplus from the first tail hour onwards.
    pv = _series([0.0] * 24 + [5000.0] * 8 + [0.0] * 16)
    decision = _decide(buy_price=_series(prices), pv=pv, load=_series([500.0] * 48))

    assert decision.soc == pytest.approx(0.30)
    assert decision.details["cover_energy_wh"] == 0
    assert "cover_until" not in decision.details


def test_a_pin_inside_a_weak_morning_still_carries_the_night_behind_it(active):
    """What the backward walk buys over Test 1's bridge.

    The sun is already up at the pin, so Test 1 calls the bridge zero and hands
    back the bare reserve -- selling off the morning's own charge before a night
    the day cannot pay for. The walk credits the day's 2 kWh against the 6 kWh
    night that follows and carries the difference.
    """
    active(TEST_NIGHT_COVER)
    prices = [1.0 + i * 0.01 for i in range(48)]
    # Tail hours 0-3: 1000 W against a 500 W load = 2000 Wh banked. Then 12 h
    # of darkness at 500 W = 6000 Wh, and sunrise again at tail hour 16.
    pv = _series([0.0] * 24 + [1000.0] * 4 + [0.0] * 12 + [4000.0] * 5 + [0.0] * 3)
    decision = _decide(buy_price=_series(prices), pv=pv, load=_series([500.0] * 48))
    # 6000 Wh of night less the 2000 Wh the weak morning banks = 4000 Wh.
    assert decision.details["cover_energy_wh"] == 4000
    assert decision.soc == pytest.approx(0.70)


def test_darkness_past_the_last_sunrise_is_not_a_night_to_cover(active):
    """The lookahead's own edge must not read as an endless night.

    Every hour after the final surplus block is darkness with no visible end,
    which is a property of a 24 h window rather than of the world. Left in, it
    would propagate back and pin every sunny day at soc_max.
    """
    active(TEST_NIGHT_COVER)
    decision = _cover_day()

    # 11 dark tail hours follow the block; only the 8 before it are covered.
    assert decision.details["cover_energy_wh"] == 4000
    assert decision.soc == pytest.approx(0.70)


def test_a_sunless_lookahead_ends_the_night_at_the_cheapest_hour(active):
    """December: no sun to make room for, so the grid is allowed to refill.

    Without this the cover would run the whole 24 h and pin at soc_max, which
    is the hoarding the feature exists to end.
    """
    active(TEST_NIGHT_COVER)
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    decision = _decide(buy_price=_series(prices))

    assert decision.details["replenishment_kind"] == REPLENISH_CHEAP_GRID
    # Tail hours 24 and 25 at 1000 W before the trough at hour 26.
    assert decision.details["cover_energy_wh"] == 2000
    assert decision.soc == pytest.approx(0.50)


def test_a_sunless_lookahead_with_no_trough_holds_the_whole_stretch(active):
    active(TEST_NIGHT_COVER)
    prices = [1.0 + i * 0.01 for i in range(48)]
    decision = _decide(buy_price=_series(prices))

    assert decision.details["replenishment_kind"] == REPLENISH_NONE
    assert decision.soc == 0.90
    assert decision.details["clamped_by"] == "range"


def test_a_guessed_sunrise_may_not_shorten_the_cover_either(active):
    """Proxied PV is darkness to the walk, as it is to Test 1's bridge."""
    active(TEST_NIGHT_COVER)
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


def test_a_peak_worth_more_than_buying_it_back_sells_part_of_the_cover(active):
    """3.00 to sell against 0.20 to buy back clears any wear: the trade is on.

    Capped by what can physically leave: 1 kW of export over the 24 horizon
    hours = 24 kWh offered, more than the 4 kWh cover, so the cover caps it.
    """
    active(TEST_NIGHT_COVER)
    decision = _sale_day()

    assert decision.details["sale_energy_wh"] == 4000
    assert decision.details["sale_buy_price"] == pytest.approx(0.20)
    assert decision.details["sale_sell_price"] == pytest.approx(3.0)
    assert decision.details["clamped_by"] == "profitable_sale"
    assert decision.soc == pytest.approx(0.30)


def test_a_sale_releases_only_what_fits_in_the_profitable_hours(active):
    """One hour above the bar at 1 kW releases 1 kWh, not the night."""
    active(TEST_NIGHT_COVER)
    decision = _sale_day(sell=[3.0] + [0.10] * 47)

    assert decision.details["sale_energy_wh"] == 1000
    assert decision.soc == pytest.approx(0.60)  # 0.70 cover less 0.1
    assert decision.details["clamped_by"] == "profitable_sale"


def test_a_spread_that_does_not_clear_the_wear_sells_nothing(active):
    """The summer case on this house's tariff: the cover is carried whole."""
    active(TEST_NIGHT_COVER)
    decision = _sale_day(
        sell=[0.25] * 48,
        battery=_battery(weight_battery_discharge=0.10, weight_battery_charge=0.0),
    )

    assert decision.details["sale_blocked"] == SALE_NO_MARGIN
    assert decision.soc == pytest.approx(0.70)


def test_a_proxied_overnight_price_may_never_talk_the_cover_down(active):
    """A spread against a guessed market is the calculation to refuse.

    Prices published for the horizon only, so the whole night is yesterday's
    curve shifted -- and a sale priced against it would empty the battery on
    the strength of a market that has not opened.
    """
    active(TEST_NIGHT_COVER)
    decision = _sale_day(buy_price=_series([1.0] * 12 + [0.20] * 12))

    assert decision.details["price_tail"] == PRICE_TAIL_PROXY
    assert decision.details["sale_blocked"] == SALE_PRICES_UNPUBLISHED
    assert decision.soc == pytest.approx(0.70)


def test_no_sell_price_means_the_cover_is_carried_whole(active):
    active(TEST_NIGHT_COVER)
    decision = _cover_day()
    assert decision.details["sale_blocked"] == SALE_NO_SELL_PRICE
    assert decision.soc == pytest.approx(0.70)


# --- test 6: the merit order -------------------------------------------------


def _published_horizon(prices: list[float], **overrides):
    """A run before the day-ahead auction: 24 h of prices, no tail.

    The shape the merit order needs, and the shape most runs actually have.
    ``_horizon_view`` recovers the horizon's own buy curve out of the tail's
    proxy lag, so a scenario that publishes all 48 h leaves it nothing to
    recover and no purchase to price -- which is correct, and inert.
    """
    kwargs = dict(buy_price=_series(prices), load=_series([1000.0] * 48))
    kwargs.update(overrides)
    return _decide(**kwargs)


def test_a_cheap_horizon_against_an_expensive_tail_carries_more_than_the_night():
    """The winter trade nothing else here can make.

    Prices published for today only: a 0.20 trough tonight, inside the horizon,
    against a 3.00 evening that the proxy puts just past the pin. Buying the
    trough forward beats importing at 3.00, so the merit order arrives heavier
    than the house physically needs -- which is the one thing Test 4's floor
    can never do.
    """
    prices = [0.20] * 6 + [1.0] * 12 + [3.0] * 6
    tests = _published_horizon(prices).details["tests"]

    assert tests[TEST_PRICED_COVER]["soc"] > tests[TEST_NIGHT_COVER]["soc"]


def test_the_merit_order_never_plans_below_the_night_cover():
    """Its floor is Test 4's answer, and the backtest is why.

    The same ladder free to undercut the physical cover scored 1.70 SEK of mean
    regret against 1.12 floored here. Lifting is where the idea pays.
    """
    for prices in (
        [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21,  # a published trough
        [2.0] * 48,  # flat
        [1.0 + i * 0.01 for i in range(48)],  # nothing cheap anywhere
    ):
        tests = _decide(buy_price=_series(prices)).details["tests"]
        assert tests[TEST_PRICED_COVER]["soc"] >= tests[TEST_NIGHT_COVER]["soc"]


def test_a_tail_that_can_refill_itself_more_cheaply_caps_what_carrying_is_worth(active):
    """The cap that makes a proxied curve safe to read.

    A carried kWh is only worth the import it displaces while the tail could
    not have bought the same kWh for less. Here the tail opens on its own
    trough, so every dear hour behind it is worth the trough's price rather
    than its own -- and making that evening three times dearer again buys
    exactly as much carry, which is the cap doing its job.
    """
    active(TEST_PRICED_COVER)

    def trough_then(evening: float):
        return _decide(
            buy_price=_series([1.0] * 24 + [0.20] * 6 + [evening] * 18),
            load=_series([1000.0] * 48),
        )

    assert (
        trough_then(3.0).details["priced_energy_wh"]
        == (trough_then(9.0).details["priced_energy_wh"])
    )


def test_the_merit_order_says_what_the_last_carried_kwh_cost(active):
    active(TEST_PRICED_COVER)
    decision = _published_horizon([0.20] * 6 + [1.0] * 12 + [3.0] * 6)

    assert decision.details["marginal_cost"] > 0
    assert decision.details["demand_rungs"] > 0
    assert decision.details["supply_rungs"] > 0


# --- test 7: the shadow plan -------------------------------------------------


def test_the_shadow_plan_solves_the_window_and_shows_its_working():
    decision = _decide(buy_price=_series([1.0] * 12 + [2.0] * 36))

    assert decision.details["solver"] == "dynamic program"
    # The two spans the reason sentence has to keep apart: the plan runs over
    # 48 h, the SOC it reports is the one it holds 24 h in, and every cost
    # below is for the whole window.
    assert decision.details["window_hours"] == pytest.approx(48.0)
    assert decision.details["horizon_hours"] == pytest.approx(24.0)
    assert "24 h horizon" in decision.reason or "24 h mark" in decision.reason
    assert 0.10 <= decision.soc <= 0.90
    # The three numbers that let a reader check the answer rather than trust it.
    assert decision.details["plan_cost"] <= decision.details["cost_at_reserve"]
    assert decision.details["plan_cost"] <= decision.details["cost_at_full"]


def test_the_shadow_plan_recovers_the_import_tariff_from_the_sell_curve():
    """_Tail carries no buy series, so the horizon's own prices are inferred.

    Both curves come off one spot price through an affine tariff -- here this
    house's own ``1.25x + 0.80`` -- so a fit over the tail's matched pairs
    recovers it exactly.
    """
    spot = [0.5 + 0.1 * (i % 7) for i in range(48)]
    decision = _decide(
        buy_price=_series([value * 1.25 + 0.80 for value in spot]),
        sell_price=_series(spot),
    )

    assert decision.details["price_model"] == PRICE_MODEL_TARIFF


def test_a_tariff_that_will_not_fit_is_not_believed():
    """A buy curve that is not an affine function of the sell curve."""
    decision = _decide(
        buy_price=_series([1.0 + (i % 5) ** 2 for i in range(48)]),
        sell_price=_series([0.5] * 24 + [1.5] * 24),
    )

    assert decision.details["price_model"] == PRICE_MODEL_UNTRUSTED


def test_a_guessed_price_keeps_its_level_and_loses_its_shape():
    """The discipline the whole design rests on.

    A rule can decline to act on a proxied price; a solver believes whatever it
    is handed. So past the day-ahead close every slot is flattened to one
    level, and a peak invented by yesterday's curve cannot move the pin however
    dramatic it is.
    """
    published = [1.0] * 12 + [2.0] * 12
    mild = _decide(buy_price=_series(published))
    # Same published median, a far more dramatic shape to arbitrage.
    wild = _decide(buy_price=_series([0.1] * 12 + [9.0] * 12))

    assert mild.details["price_tail"] == PRICE_TAIL_PROXY
    assert "guessed_buy" in mild.details
    assert mild.details["guessed_slots"] > 0
    assert wild.details["guessed_buy"] == pytest.approx(wild.details["guessed_buy"])
    # The guessed half of the window is one flat price in both, so neither can
    # trade the tail against itself.
    assert mild.details["guessed_buy"] == pytest.approx(1.5)


def test_a_battery_with_no_room_to_plan_in_degrades_to_the_floor():
    decision = _decide(battery=_battery(soc_min=0.40, soc_max=0.40, soc_target=0.40))

    assert decision.details["solver"] == "degenerate"
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


def test_the_slate_is_the_one_the_backtest_left():
    """Tests 1, 2 and 5 were retired on the numbers; their keys are not reused."""
    keys = [candidate.key for candidate in CANDIDATES]

    assert keys == [TEST_DAILY_RATIO, TEST_NIGHT_COVER, TEST_PRICED_COVER, TEST_SHADOW_PLAN]
    assert ACTIVE_CANDIDATE == TEST_SHADOW_PLAN


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
    active(TEST_NIGHT_COVER)
    prices = [1.0] * 12 + [2.0] * 14 + [1.0] + [2.0] * 21
    first = _decide(buy_price=_series(prices))
    second = _decide(
        buy_price=_series(prices),
        load=_series([1050.0] * 48),
        previous=first,
    )

    assert second.soc == first.soc
    assert second.details["tests"][TEST_NIGHT_COVER]["soc"] == pytest.approx(0.51)

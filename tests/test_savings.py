"""Tests for the cost and savings arithmetic.

Every number here is worked by hand in the test itself, because that is the
only way a savings figure earns any trust: the module's whole job is to answer
"what did this cost me", and an assertion that merely agrees with whatever the
code currently produces would notice nothing at all if the sign of an export
flipped.

The three worlds under test (grid-only, solar-only, actual) are documented in
``docs/savings_plan.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.emhass_companion.configuration import EmhassConfig
from custom_components.emhass_companion.const import (
    CONF_GRID_EXPORT_ENERGY_ENTITY,
    CONF_GRID_IMPORT_ENERGY_ENTITY,
    CONF_METERING,
    CONF_METERING_ENABLED,
    CONF_PV_ENERGY_ENTITY,
)
from custom_components.emhass_companion.metering import build_meters
from custom_components.emhass_companion.models import Plan, PlanRow, Point, Series
from custom_components.emhass_companion.savings import (
    IntervalEnergy,
    Ledger,
    Prices,
    cost_worlds,
    forecast_costs,
    house_load_kwh,
    settle,
)

BUY = 2.0
SELL = 0.5
PRICES = Prices(buy=BUY, sell=SELL)


# --- the primitives -----------------------------------------------------------


def test_settle_prices_each_direction_on_its_own_side():
    # Not one price: the sell price is rarely the buy price and in most markets
    # is nowhere near it, so netting the two would be worth real money.
    assert settle(3.0, PRICES) == pytest.approx(6.0)
    assert settle(-3.0, PRICES) == pytest.approx(-1.5)
    assert settle(0.0, PRICES) == 0.0


def test_house_load_is_derived_from_the_energy_balance():
    # 4 kWh of PV and 1 imported, with 2 going into the battery: the house
    # itself consumed the other 3.
    energy = IntervalEnergy(imported=1.0, pv=4.0, battery_charge=2.0)
    assert house_load_kwh(energy) == pytest.approx(3.0)


def test_measured_house_load_wins_over_the_derivation():
    # The derivation would say 3.0; a measured sensor is trusted instead,
    # because it does not inherit three other meters' errors.
    energy = IntervalEnergy(imported=1.0, pv=4.0, battery_charge=2.0, house_load=2.5)
    assert house_load_kwh(energy) == pytest.approx(2.5)


def test_disagreeing_meters_cannot_produce_negative_load():
    # A DC-side PV meter against an AC-side grid meter, or one sensor updating
    # a moment before the others. Left signed, this would hand the grid-only
    # world a credit for consumption that never happened.
    energy = IntervalEnergy(exported=10.0, pv=1.0)
    assert house_load_kwh(energy) == 0.0


# --- the three worlds ---------------------------------------------------------


def test_solar_covers_the_whole_load():
    # 5 kWh of sun, 3 consumed, 2 exported, no battery. The house buys nothing.
    energy = IntervalEnergy(exported=2.0, pv=5.0, house_load=3.0)
    costs = cost_worlds(energy, PRICES)

    assert costs.actual == pytest.approx(-1.0)  # 2 kWh sold at 0.5
    assert costs.solar_only == pytest.approx(-1.0)  # no battery: identical
    assert costs.grid_only == pytest.approx(6.0)  # 3 kWh bought at 2.0


def test_battery_discharging_beats_the_solar_only_world():
    # Night: 3 kWh of load, all of it from the battery, nothing imported.
    energy = IntervalEnergy(house_load=3.0, battery_discharge=3.0)
    costs = cost_worlds(energy, PRICES)

    assert costs.actual == 0.0
    # Without the battery those 3 kWh come off the grid at the buy price...
    assert costs.solar_only == pytest.approx(6.0)
    # ...and without the solar either, the same, since there is no sun anyway.
    assert costs.grid_only == pytest.approx(6.0)


def test_charging_the_battery_costs_more_than_not_charging_it():
    # Cheap hour: 1 kWh of load plus 4 kWh into the battery, all imported.
    energy = IntervalEnergy(imported=5.0, house_load=1.0, battery_charge=4.0)
    costs = cost_worlds(energy, PRICES)

    assert costs.actual == pytest.approx(10.0)
    assert costs.solar_only == pytest.approx(2.0)
    # The interval on its own looks like an 8.00 loss. It is only a saving once
    # the battery discharges -- which is what the storage carry term exists to
    # stop misreporting at a day boundary. See test_storage_carry_*.


def test_both_directions_in_one_interval_are_priced_separately():
    # A meter can legitimately report both. Netting them to a single direction
    # would silently discard whichever side is smaller.
    energy = IntervalEnergy(imported=2.0, exported=1.0, house_load=1.0)
    costs = cost_worlds(energy, PRICES)
    assert costs.actual == pytest.approx(2.0 * BUY - 1.0 * SELL)


# --- the ledger ---------------------------------------------------------------


def test_savings_components_sum_to_the_total():
    """The decomposition is the feature's central claim, so it is asserted.

    solar + battery must equal total, exactly, on any sequence of intervals --
    otherwise the three sensors published from them disagree with each other on
    the user's dashboard.
    """
    ledger = Ledger(day="2026-08-12")
    ledger.record(IntervalEnergy(exported=2.0, pv=5.0, house_load=3.0), PRICES)
    ledger.record(IntervalEnergy(house_load=3.0, battery_discharge=3.0), PRICES)
    ledger.record(IntervalEnergy(imported=5.0, house_load=1.0, battery_charge=4.0), PRICES)

    assert ledger.solar_savings + ledger.battery_savings == pytest.approx(ledger.total_savings)


def test_a_day_of_pure_solar_saves_the_retail_price_of_what_it_displaced():
    ledger = Ledger(day="2026-08-12")
    ledger.record(IntervalEnergy(pv=4.0, house_load=4.0), PRICES)

    # Four self-consumed kWh at the buy price, and nothing for the battery.
    assert ledger.solar_savings == pytest.approx(8.0)
    assert ledger.battery_savings == pytest.approx(0.0)
    assert ledger.actual_cost == pytest.approx(0.0)


def test_exported_solar_is_credited_at_the_sell_price_not_the_buy_price():
    ledger = Ledger(day="2026-08-12")
    ledger.record(IntervalEnergy(exported=6.0, pv=6.0, house_load=0.0), PRICES)

    # The house consumed nothing, so W0 costs nothing; the saving is purely the
    # export income, which is what W1 earned.
    assert ledger.solar_savings == pytest.approx(3.0)


def test_round_trip_losses_come_out_of_the_battery_saving_on_their_own():
    """No explicit loss term: charge exceeding discharge is the loss.

    The two assertions below are the same day read at two different moments,
    and the difference between them is exactly where the loss lands.
    """
    ledger = Ledger(day="2026-08-12", stored_start_kwh=0.0, stored_now_kwh=0.0)
    # Buy 10 kWh cheap into the battery.
    ledger.record(
        IntervalEnergy(imported=10.0, house_load=0.0, battery_charge=10.0),
        Prices(buy=1.0, sell=0.5),
    )
    # Get 9 back out later, when buying would have cost 3.0.
    ledger.record(IntervalEnergy(house_load=9.0, battery_discharge=9.0), Prices(buy=3.0, sell=1.0))

    assert ledger.actual_cost == pytest.approx(10.0)
    assert ledger.solar_only_cost == pytest.approx(27.0)
    assert ledger.battery_charge_kwh - ledger.battery_discharge_kwh == pytest.approx(1.0)
    # Read here, the account still believes a kWh is in the battery: paid 10,
    # displaced 27, holding 1.
    assert ledger.battery_savings == pytest.approx(18.0)

    # But the SOC sensor says the battery is empty again -- the missing kWh was
    # never stored at all, it was the round trip. Reconciling writes it off,
    # and the saving drops to the 17 actually captured. Nothing anywhere
    # models efficiency; the meters disagreeing *is* the loss.
    ledger.reconcile_storage(0.0)
    assert ledger.battery_savings == pytest.approx(17.0)


def test_storage_carry_credits_a_day_that_ends_with_a_fuller_battery():
    """Otherwise a day that charged overnight reads as a disaster.

    The energy is bought inside the day and spent inside the next one, so
    without this the pair sawtooths between a large loss and a large gain.
    """
    ledger = Ledger(day="2026-08-12", stored_start_kwh=0.0, stored_now_kwh=0.0)
    ledger.record(
        IntervalEnergy(imported=10.0, house_load=0.0, battery_charge=10.0),
        Prices(buy=1.0, sell=0.5),
    )

    # Charged at 1.0/kWh, so the 10 kWh left in the battery is worth 10.
    assert ledger.storage_carry == pytest.approx(10.0)
    assert ledger.savings_excl_carry == pytest.approx(-10.0)
    assert ledger.total_savings == pytest.approx(0.0)


def test_storage_carry_telescopes_across_a_pair_of_days():
    """The property the whole daily figure rests on.

    Buy cheap on Monday, spend it on Tuesday. Each day on its own must read
    sensibly, and the two together must equal the spread actually captured --
    which they only do because the credit Monday takes is the identical debit
    Tuesday pays, out of the same running cost account.
    """
    prices_cheap, prices_dear = Prices(buy=1.0, sell=0.5), Prices(buy=3.0, sell=1.0)

    monday = Ledger(day="2026-08-10", stored_start_kwh=0.0, stored_now_kwh=0.0)
    monday.record(IntervalEnergy(imported=10.0, house_load=0.0, battery_charge=10.0), prices_cheap)

    # The rollover metering.py performs: the account crosses midnight intact.
    tuesday = Ledger(
        day="2026-08-11",
        stored_start_kwh=10.0,
        stored_now_kwh=10.0,
        battery_book_kwh=monday.battery_book_kwh,
        battery_book_value=monday.battery_book_value,
        battery_book_value_start=monday.battery_book_value,
    )
    tuesday.record(IntervalEnergy(house_load=10.0, battery_discharge=10.0), prices_dear)

    # Monday bought 10 kWh for 10 and stored all of it: it broke even.
    assert monday.total_savings == pytest.approx(0.0)
    # Tuesday displaced 30 of retail using 10 of stored energy: it made 20.
    assert tuesday.total_savings == pytest.approx(20.0)

    pair = monday.total_savings + tuesday.total_savings
    raw = monday.savings_excl_carry + tuesday.savings_excl_carry
    assert pair == pytest.approx(raw) == pytest.approx(20.0)


def test_discharging_a_battery_the_account_never_saw_charge_is_not_free():
    """The asymmetry a naive carry price has, asserted so it cannot come back.

    Valuing the day's SOC delta at "the price the battery charged at today"
    silently yields zero on a day that only discharges -- and a day that only
    discharges is the second half of every arbitrage cycle.
    """
    ledger = Ledger(day="2026-08-12", stored_start_kwh=10.0, stored_now_kwh=10.0)
    ledger.record(
        IntervalEnergy(house_load=10.0, battery_discharge=10.0), Prices(buy=3.0, sell=1.0)
    )

    # The account opened at the price in force, so the stock it spends is
    # debited at that same price rather than at nothing.
    assert ledger.storage_carry == pytest.approx(-30.0)
    assert ledger.total_savings == pytest.approx(0.0)


def test_the_cost_account_opens_against_what_is_already_in_the_battery():
    ledger = Ledger(day="2026-08-12", stored_start_kwh=4.0, stored_now_kwh=4.0)
    ledger.record(IntervalEnergy(imported=1.0, house_load=1.0), Prices(buy=2.0, sell=0.5))

    assert ledger.battery_book_kwh == pytest.approx(4.0)
    assert ledger.carry_price == pytest.approx(2.0)
    # Opening the account is not itself a gain or a loss.
    assert ledger.storage_carry == pytest.approx(0.0)


def test_reconciling_the_account_keeps_the_unit_price_and_fixes_the_quantity():
    ledger = Ledger(day="2026-08-12", stored_start_kwh=0.0, stored_now_kwh=0.0)
    ledger.record(
        IntervalEnergy(imported=10.0, house_load=0.0, battery_charge=10.0),
        Prices(buy=1.5, sell=0.5),
    )
    # The SOC sensor says only 9 actually landed -- charging losses the meter
    # on the AC side never saw.
    ledger.reconcile_storage(9.0)

    assert ledger.battery_book_kwh == pytest.approx(9.0)
    assert ledger.carry_price == pytest.approx(1.5)
    assert ledger.battery_book_value == pytest.approx(13.5)


def test_energy_with_no_price_is_counted_but_not_costed():
    """An incomplete day must look incomplete, not cheap."""
    ledger = Ledger(day="2026-08-12")
    ledger.record(IntervalEnergy(imported=4.0, house_load=4.0), None)

    assert ledger.imported_kwh == pytest.approx(4.0)
    assert ledger.house_load_kwh == pytest.approx(4.0)
    assert ledger.actual_cost == 0.0
    assert ledger.unpriced_kwh == pytest.approx(4.0)


def test_average_prices_ignore_the_unpriced_intervals():
    ledger = Ledger(day="2026-08-12")
    ledger.record(IntervalEnergy(imported=1.0, house_load=1.0), None)
    ledger.record(IntervalEnergy(imported=1.0, house_load=1.0), Prices(buy=4.0, sell=0.0))

    # Two kWh in, one of them priced at 4.0. Dividing the spend by *all* the
    # imported energy would report 2.0 -- a price that was never in force.
    assert ledger.average_import_price == pytest.approx(2.0)
    assert ledger.charge_priced_kwh == 0.0


def test_balance_residual_reports_meters_that_disagree():
    # PV says 5, grid says 1 imported, battery took 2 -- implying 4 of load.
    # The house sensor says 3.5. The half kWh gap is the user's own meters.
    ledger = Ledger(day="2026-08-12")
    ledger.record(IntervalEnergy(imported=1.0, pv=5.0, battery_charge=2.0, house_load=3.5), PRICES)
    assert ledger.balance_residual_kwh == pytest.approx(-0.5)


def test_ledger_round_trips_through_storage():
    ledger = Ledger(day="2026-08-12", stored_start_kwh=1.0)
    ledger.record(IntervalEnergy(imported=2.0, house_load=2.0), PRICES)
    restored = Ledger.from_dict(ledger.as_dict())

    assert restored == ledger
    assert restored.total_savings == pytest.approx(ledger.total_savings)


def test_a_ledger_from_a_newer_version_does_not_raise():
    # A restored backup or a downgrade. Losing a field is a bookkeeping
    # nuisance; taking the integration down over one is not acceptable.
    restored = Ledger.from_dict({"day": "2026-08-12", "actual_cost": 3.0, "invented_field": 9})
    assert restored.day == "2026-08-12"
    assert restored.actual_cost == 3.0


# --- the forecast -------------------------------------------------------------

START = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _plan(rows: list[PlanRow]) -> Plan:
    return Plan(generated_at=START, schema_version="1.0", rows=rows)


def _row(offset_hours: float, **kwargs) -> PlanRow:
    defaults = {
        "p_load": 0.0,
        "p_pv": 0.0,
        "p_grid": 0.0,
        "p_batt": 0.0,
        "unit_load_cost": BUY,
        "unit_prod_price": SELL,
    }
    return PlanRow(timestamp=START + timedelta(hours=offset_hours), **{**defaults, **kwargs})


def test_forecast_prices_the_plan_in_all_three_worlds():
    # Two hourly rows: an hour importing 2 kW for a 2 kW load, then an hour of
    # 3 kW sun covering a 1 kW load and exporting the other 2 kW.
    plan = _plan(
        [
            _row(0, p_load=2000, p_grid=2000),
            _row(1, p_load=1000, p_pv=3000, p_grid=-2000),
        ]
    )
    forecast = forecast_costs(
        plan, start=START, window=timedelta(hours=2), buy=Series.empty(), sell=Series.empty()
    )

    assert forecast is not None
    assert forecast.hours == pytest.approx(2.0)
    assert forecast.actual_cost == pytest.approx(2.0 * BUY - 2.0 * SELL)
    assert forecast.grid_only_cost == pytest.approx(3.0 * BUY)
    assert forecast.solar_savings == pytest.approx(
        forecast.grid_only_cost - forecast.solar_only_cost
    )


def test_forecast_counts_deferrable_loads_against_the_baseline():
    """P_Load excludes them in EMHASS's schema, so they have to be added back.

    A baseline that ignored the car charger would credit the plan with nothing
    for having moved it -- and understate what the house avoided buying.
    """
    plan = _plan(
        [
            _row(0, p_load=1000, p_grid=4000, deferrables=(3000.0,)),
            _row(1, p_load=1000, p_grid=4000, deferrables=(3000.0,)),
        ]
    )
    forecast = forecast_costs(
        plan, start=START, window=timedelta(hours=1), buy=Series.empty(), sell=Series.empty()
    )

    assert forecast is not None
    assert forecast.load_kwh == pytest.approx(4.0)
    assert forecast.grid_only_cost == pytest.approx(8.0)


def test_forecast_reports_the_hours_it_covered_rather_than_extrapolating():
    plan = _plan([_row(0, p_load=1000, p_grid=1000), _row(1, p_load=1000, p_grid=1000)])
    forecast = forecast_costs(
        plan, start=START, window=timedelta(hours=24), buy=Series.empty(), sell=Series.empty()
    )

    assert forecast is not None
    assert forecast.hours == pytest.approx(2.0)
    assert forecast.complete is False


def test_forecast_prorates_a_row_the_window_ends_inside():
    plan = _plan([_row(0, p_load=1000, p_grid=1000), _row(1, p_load=1000, p_grid=1000)])
    forecast = forecast_costs(
        plan,
        start=START,
        window=timedelta(minutes=90),
        buy=Series.empty(),
        sell=Series.empty(),
    )

    assert forecast is not None
    # An hour of the first row and half of the second, not two whole rows.
    assert forecast.import_kwh == pytest.approx(1.5)
    assert forecast.actual_cost == pytest.approx(1.5 * BUY)


def test_forecast_starts_from_now_inside_the_row_that_covers_it():
    # EMHASS aligns its horizon to a timestep boundary, so "now" routinely sits
    # inside row zero. Only the part still ahead counts.
    plan = _plan([_row(0, p_load=1000, p_grid=1000), _row(1, p_load=1000, p_grid=1000)])
    forecast = forecast_costs(
        plan,
        start=START + timedelta(minutes=30),
        window=timedelta(hours=1),
        buy=Series.empty(),
        sell=Series.empty(),
    )

    assert forecast is not None
    assert forecast.import_kwh == pytest.approx(1.0)


def test_forecast_falls_back_to_our_own_prices_when_the_plan_omits_them():
    plan = _plan(
        [
            _row(0, p_load=1000, p_grid=1000, unit_load_cost=None, unit_prod_price=None),
            _row(1, p_load=1000, p_grid=1000, unit_load_cost=None, unit_prod_price=None),
        ]
    )
    forecast = forecast_costs(
        plan,
        start=START,
        window=timedelta(hours=1),
        buy=Series([Point(START, 7.0)]),
        sell=Series([Point(START, 1.0)]),
    )

    assert forecast is not None
    assert forecast.actual_cost == pytest.approx(7.0)


def test_forecast_credits_the_charge_the_horizon_ends_holding():
    # An hour spent buying 4 kWh into an empty battery. Priced naively the
    # window is a pure loss, which is how every overnight-charging plan would
    # look without the carry term.
    plan = _plan(
        [
            _row(0, p_load=0, p_grid=4000, p_batt=-4000, soc=0.4),
            _row(1, p_load=0, p_grid=0, p_batt=0, soc=0.4),
        ]
    )
    forecast = forecast_costs(
        plan,
        start=START,
        window=timedelta(hours=1),
        buy=Series.empty(),
        sell=Series.empty(),
        stored_now_kwh=0.0,
        capacity_kwh=10.0,
    )

    assert forecast is not None
    assert forecast.actual_cost == pytest.approx(8.0)
    # 40% of 10 kWh stored, bought at the buy price.
    assert forecast.storage_carry == pytest.approx(8.0)
    assert forecast.total_savings == pytest.approx(0.0)


def test_forecast_is_none_when_nothing_lies_ahead():
    # Unavailable is the honest answer. A zero reads as "this day is free".
    plan = _plan([_row(-5, p_load=1000, p_grid=1000)])
    assert (
        forecast_costs(
            plan, start=START, window=timedelta(hours=24), buy=Series.empty(), sell=Series.empty()
        )
        is None
    )
    assert (
        forecast_costs(
            None, start=START, window=timedelta(hours=24), buy=Series.empty(), sell=Series.empty()
        )
        is None
    )


# --- meter resolution ---------------------------------------------------------
#
# The cost tracking screen is a single switch, so what that switch resolves to
# is not visible anywhere on the form. These pin the resolution down.


def _config(**kwargs) -> EmhassConfig:
    return EmhassConfig(url="http://localhost:5000", **kwargs)


def test_no_meters_are_read_until_the_switch_is_on():
    # Including the fallbacks. A house that has never opened the screen gets the
    # forecast sensors and nothing that touches its meters.
    spec = build_meters(
        _config(pv_live_entity="sensor.pv", house_load_total_entity="sensor.load"), {}
    )
    assert spec.usable is False
    assert spec.entities() == []


def test_the_switch_alone_picks_up_sensors_configured_elsewhere():
    """The fallbacks are what make one switch enough for most houses."""
    spec = build_meters(
        _config(
            pv_live_entity="sensor.pv_power",
            house_load_total_entity="sensor.house_power",
            battery_power_entity="sensor.batt_power",
        ),
        {
            CONF_METERING: {
                CONF_METERING_ENABLED: True,
                CONF_GRID_IMPORT_ENERGY_ENTITY: "sensor.imported",
                CONF_GRID_EXPORT_ENERGY_ENTITY: "sensor.exported",
            }
        },
    )

    assert spec.usable is True
    assert spec.has_solar is True
    assert spec.has_battery is True
    # And it says so, since a power sensor is only as good as its update rate.
    assert spec.describe()["solar"] == "sensor.pv_power (power)"
    assert spec.describe()["grid_import"] == "sensor.imported (energy)"


def test_a_configured_counter_beats_the_power_fallback():
    spec = build_meters(
        _config(pv_live_entity="sensor.pv_power"),
        {
            CONF_METERING: {
                CONF_METERING_ENABLED: True,
                CONF_GRID_IMPORT_ENERGY_ENTITY: "sensor.imported",
                CONF_GRID_EXPORT_ENERGY_ENTITY: "sensor.exported",
                CONF_PV_ENERGY_ENTITY: "sensor.pv_energy",
            }
        },
    )
    assert spec.describe()["solar"] == "sensor.pv_energy (energy)"


def test_half_a_grid_pair_is_not_usable():
    # No power-sensor fallback for the grid on purpose: deriving it from load
    # minus PV minus battery would carry three sensors' error on the one
    # quantity that has to be right.
    spec = build_meters(
        _config(),
        {
            CONF_METERING: {
                CONF_METERING_ENABLED: True,
                CONF_GRID_IMPORT_ENERGY_ENTITY: "sensor.imp",
            }
        },
    )
    assert spec.usable is False

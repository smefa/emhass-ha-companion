"""Tests for the net house load sensor's subtraction.

Two loads sharing one meter is an ordinary setup -- an EV configured as both a
scheduled charge and a surplus-only charge points both at the charger -- and
double-counting it drove the sensor onto its zero clamp for whole afternoons.
Because that sensor's own recorded history is what the load forecast is built
from, the zeros then came back as tomorrow's forecast.
"""

from __future__ import annotations

from homeassistant.core import State

from custom_components.emhass_companion.deferrable import DeferrableRuntime
from custom_components.emhass_companion.savings import Ledger
from custom_components.emhass_companion.sensor import (
    _battery_attributes,
    _deferred_watts,
    _savings_attributes,
    _solar_attributes,
)


def test_no_battery_savings_publish_unreconciled_difference():
    ledger = Ledger(
        battery_enabled=False,
        actual_cost=46.23,
        solar_only_cost=53.55,
        grid_only_cost=63.08,
        balance_residual_kwh=-0.089,
        unpriced_kwh=0.148,
    )
    attributes = _savings_attributes(ledger)
    assert attributes["battery_savings"] == 0.0
    assert attributes["solar_savings"] == 16.85
    assert attributes["baseline_cost_solar_only"] == 46.23
    assert attributes["modeled_cost_solar_only"] == 53.55
    assert attributes["solar_only_baseline_method"] == "measured_grid_without_battery"
    assert attributes["accounting_difference"] == 7.32
    assert attributes["battery_enabled"] is False
    assert attributes["source_intervals_reconciled"] is False
    assert attributes["balance_residual_kwh"] == -0.089
    assert attributes["unpriced_kwh"] == 0.148
    assert _battery_attributes(ledger)["accounting_difference"] == 7.32
    assert _solar_attributes(ledger)["baseline_cost_solar_only"] == 46.23
    assert _solar_attributes(ledger)["modeled_cost_solar_only"] == 53.55
    assert _solar_attributes(ledger)["accounting_difference"] == 7.32


def _load(**overrides) -> DeferrableRuntime:
    defaults = {
        "subentry_id": "abc",
        "name": "Car",
        "nominal_power_w": 8000.0,
        "operating_hours": 3.0,
    }
    return DeferrableRuntime(**{**defaults, **overrides})


def _states(**by_entity: str) -> object:
    lookup = {
        entity_id.replace("__", "."): State(entity_id.replace("__", "."), value)
        for entity_id, value in by_entity.items()
    }
    return lookup.get


def test_distinct_meters_add_up():
    loads = [
        _load(subentry_id="car", power_sensor="sensor.charger_power"),
        _load(subentry_id="pool", name="Pool", power_sensor="sensor.plug_power"),
    ]
    state_of = _states(sensor__charger_power="3555", sensor__plug_power="800")
    assert _deferred_watts(loads, state_of) == 4355.0


def test_a_shared_meter_is_counted_once():
    """The scenario this was added for.

    Both loads read the same charger. Summing per load subtracts 7110 W from a
    4853 W house total, which clamps to zero; counting the meter once leaves
    the real baseline behind it.
    """
    loads = [
        _load(subentry_id="car_scheduled", power_sensor="sensor.charger_power"),
        _load(subentry_id="car_surplus", name="Car solar", power_sensor="sensor.charger_power"),
    ]
    state_of = _states(sensor__charger_power="3555")
    assert _deferred_watts(loads, state_of) == 3555.0


def test_a_shared_on_off_source_takes_the_largest_nominal():
    """An on/off source carries no watts of its own.

    Each load reads it as its own nominal power, so they disagree. One switch
    is one physical thing, and the most any load claims is behind it is the
    honest reading.
    """
    loads = [
        _load(subentry_id="big", nominal_power_w=8000.0, control_entity="switch.charger"),
        _load(subentry_id="small", nominal_power_w=1400.0, control_entity="switch.charger"),
    ]
    assert _deferred_watts(loads, _states(switch__charger="on")) == 8000.0
    assert _deferred_watts(loads, _states(switch__charger="off")) == 0.0


def test_an_unreadable_source_contributes_nothing():
    loads = [
        _load(subentry_id="car", power_sensor="sensor.charger_power"),
        _load(subentry_id="pool", name="Pool", power_sensor="sensor.plug_power"),
    ]
    state_of = _states(sensor__plug_power="800")  # charger missing entirely
    assert _deferred_watts(loads, state_of) == 800.0


def test_a_load_with_no_source_at_all_is_skipped():
    loads = [_load(subentry_id="hot_water", name="Hot water")]
    assert _deferred_watts(loads, _states()) == 0.0

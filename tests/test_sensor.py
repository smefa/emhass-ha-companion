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
from custom_components.emhass_companion.sensor import _deferred_watts


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

"""The heat pump room model and its EMHASS ``thermal_battery`` entry."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from custom_components.emhass_companion.const import ACTION_MPC
from custom_components.emhass_companion.heat_pump_room import (
    EMHASS_THERMAL_LOSS_KW,
    HeatPumpModel,
    HeatPumpRoomConfig,
)
from custom_components.emhass_companion.models import (
    BatteryConfig,
    DeferrableLoad,
    GridConfig,
    HybridInverterConfig,
    Point,
    Series,
)
from custom_components.emhass_companion.payload import (
    PayloadError,
    PayloadInputs,
    build_payload,
    outdoor_per_step,
)

STEP = timedelta(minutes=30)
NOW = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)


def _model(**overrides) -> HeatPumpModel:
    defaults = {"thermal_mass_m3": 10.0, "floor_area_m2": 150.0, "specific_heating_demand": 100.0}
    return HeatPumpModel(**{**defaults, **overrides})


def _room(**overrides) -> HeatPumpRoomConfig:
    defaults = {
        "model": _model(),
        "nominal_power_w": 2000,
        "comfort_temperature": 21.0,
        "setback_temperature": 19.0,
        "max_temperature": 23.0,
        "comfort_start": time(0, 0),
        "comfort_end": time(23, 59),
        "current_temperature": 21.0,
    }
    return HeatPumpRoomConfig(**{**defaults, **overrides})


def test_cop_follows_emhass_carnot_formula_and_clip():
    model = _model(supply_temperature=35.0, carnot_efficiency=0.4)
    assert model.cop(0.0) == pytest.approx(0.4 * 308.15 / 35)
    assert model.cop(34.0) == 8.0
    assert model.cop(40.0) == 1.0
    assert _model(carnot_efficiency=0.05).cop(-30.0) == 1.0


def test_constant_cop_ignores_the_outdoor_air():
    model = _model(constant_cop=4.5, source_temperature=0.0)
    assert model.cop(-20.0) == model.cop(15.0) == 4.5


def test_source_temperature_replaces_the_outdoor_air_in_carnot():
    model = _model(supply_temperature=35.0, carnot_efficiency=0.4, source_temperature=0.0)
    assert model.cop(-20.0) == model.cop(15.0) == pytest.approx(0.4 * 308.15 / 35)


def test_fixed_cop_is_sent_as_efficiency_and_demand_still_follows_outdoor():
    room = _room(model=_model(constant_cop=4.2)).with_outdoor_forecast([-5.0] * 48)
    entry = room.to_emhass(NOW, STEP, 48)
    assert entry["efficiency"] == 4.2
    assert entry["u_value"] == _model().demand_keys()["u_value"]
    assert "efficiency" not in _room().with_outdoor_forecast([0.0] * 48).to_emhass(NOW, STEP, 48)


@pytest.mark.parametrize(
    "overrides",
    [
        {"constant_cop": 0.5},
        {"constant_cop": 9},
        {"constant_cop": float("nan")},
        {"source_temperature": 35.0},
        {"source_temperature": float("inf")},
    ],
)
def test_invalid_fixed_cop_inputs_are_rejected(overrides):
    with pytest.raises(ValueError):
        _model(**overrides)


def test_from_data_reads_the_optional_cop_keys_and_blank_means_unset():
    base = {"thermal_mass_m3": 10, "floor_area_m2": 150, "specific_heating_demand": 100}
    model = HeatPumpModel.from_data({**base, "constant_cop": 4, "source_temperature": 1})
    assert (model.constant_cop, model.source_temperature) == (4.0, 1.0)
    blank = HeatPumpModel.from_data({**base, "constant_cop": None, "source_temperature": ""})
    assert blank.fixed_cop() is None


def test_no_window_area_sends_no_solar_keys():
    for model in (_model(), _model(window_area_m2=0.0, shgc=0.5)):
        entry = _room(model=model).with_outdoor_forecast([0.0] * 48).to_emhass(NOW, STEP, 48)
        assert "window_area" not in entry
        assert "shgc" not in entry


def test_window_area_sends_solar_gain_keys():
    model = _model(window_area_m2=18.5, shgc=0.45)
    entry = _room(model=model).with_outdoor_forecast([0.0] * 48).to_emhass(NOW, STEP, 48)
    assert (entry["window_area"], entry["shgc"]) == (18.5, 0.45)
    # The physics demand keys EMHASS needs for solar gain are already there.
    assert {"u_value", "envelope_area", "ventilation_rate", "heated_volume"} <= entry.keys()


@pytest.mark.parametrize(
    "overrides",
    [
        {"window_area_m2": -1.0},
        {"window_area_m2": float("nan")},
        {"shgc": 1.5},
        {"shgc": -0.1},
        {"shgc": float("inf")},
    ],
)
def test_invalid_solar_inputs_are_rejected(overrides):
    with pytest.raises(ValueError):
        _model(**overrides)


def test_from_data_reads_solar_keys_and_blank_means_default():
    base = {"thermal_mass_m3": 10, "floor_area_m2": 150, "specific_heating_demand": 100}
    model = HeatPumpModel.from_data({**base, "window_area": 12, "shgc": 0})
    assert (model.window_area_m2, model.shgc) == (12.0, 0.0)  # 0 is a real SHGC
    blank = HeatPumpModel.from_data({**base, "window_area": "", "shgc": None})
    assert (blank.window_area_m2, blank.shgc) == (None, 0.6)
    assert HeatPumpModel.from_data(base).window_area_m2 is None


def test_demand_is_degree_days_and_heating_only():
    model = _model()
    assert model.demand_kwh(8.0, 0.5) == pytest.approx(100 * 150 * 10 * (0.5 / 24) / 3000)
    assert model.demand_kwh(25.0, 0.5) == 0.0


def test_capacity_is_concrete_mass():
    assert _model(thermal_mass_m3=3.6).capacity_kwh_per_k == pytest.approx(2400 * 0.88 / 1000)


@pytest.mark.parametrize(
    "overrides",
    [
        {"thermal_mass_m3": 0},
        {"floor_area_m2": -1},
        {"annual_reference_hdd": 0},
        {"specific_heating_demand": -1},
        {"carnot_efficiency": 0},
        {"carnot_efficiency": 1.1},
        {"inertia_hours": 25},
        {"supply_temperature": float("nan")},
        {"thermal_mass_m3": True},
    ],
)
def test_invalid_model_is_rejected(overrides):
    with pytest.raises(ValueError):
        _model(**overrides)


def test_from_data_reads_stored_keys_with_emhass_defaults():
    model = HeatPumpModel.from_data(
        {"thermal_mass_m3": 8, "floor_area_m2": 120, "specific_heating_demand": 90}
    )
    assert (model.supply_temperature, model.carnot_efficiency) == (35.0, 0.4)
    assert (model.base_temperature, model.annual_reference_hdd, model.inertia_hours) == (
        18.0,
        3000.0,
        0.0,
    )
    with pytest.raises(ValueError, match="incomplete"):
        HeatPumpModel.from_data({"thermal_mass_m3": 8})


def test_band_order_is_validated():
    with pytest.raises(ValueError):
        _room(setback_temperature=22.0)
    with pytest.raises(ValueError):
        _room(nominal_power_w=0)


def test_to_emhass_sends_the_thermal_battery_model():
    room = _room(model=_model(inertia_hours=2.0)).with_outdoor_forecast([0.0] * 48)
    entry = room.to_emhass(NOW, STEP, 48)
    assert entry["volume"] == 10.0
    assert (entry["density"], entry["heat_capacity"]) == (2400.0, 0.88)
    assert entry["supply_temperature"] == 35.0
    assert entry["carnot_efficiency"] == 0.4
    assert entry["sense"] == "heat"
    assert entry["thermal_inertia_time_constant"] == 2.0
    assert entry["start_temperature"] == 21.0
    assert len(entry["min_temperatures"]) == len(entry["max_temperatures"]) == 48
    assert "thermal_loss" not in entry
    assert "heating_rate" not in entry


@pytest.mark.parametrize("outdoor", [-2.5, 0.0, 17.9, 18.0, 25.0])
def test_demand_keys_match_the_degree_day_demand(outdoor):
    """Temporary EMHASS 0.18.8 workaround: the physics keys stand in for the
    degree-day ones, so EMHASS's cache-hit refresh does not zero the demand."""
    model = _model(base_temperature=18.0, annual_reference_hdd=3300.0, specific_heating_demand=57.9)
    keys = model.demand_keys()
    assert "specific_heating_demand" not in keys
    assert "area" not in keys
    # EMHASS calculate_thermal_balance_physics, heating, one 60-minute step.
    kw = (
        keys["u_value"] * keys["envelope_area"] / 1000
        + keys["ventilation_rate"] * keys["heated_volume"] * 1.2 * 1.005 / 3600
    ) * max(keys["indoor_target_temperature"] - outdoor, 0.0)
    assert kw == pytest.approx(model.demand_kwh(outdoor, 1.0))


def test_to_emhass_carries_the_demand_keys():
    entry = _room().with_outdoor_forecast([0.0] * 48).to_emhass(NOW, STEP, 48)
    assert {k: entry[k] for k in _model().demand_keys()} == _model().demand_keys()


def test_no_inertia_and_no_reading_leave_the_keys_out():
    entry = (
        _room(current_temperature=None).with_outdoor_forecast([0.0] * 48).to_emhass(NOW, STEP, 48)
    )
    assert "thermal_inertia_time_constant" not in entry
    assert "start_temperature" not in entry


def test_best_case_replays_emhass_dynamics():
    room = _room(model=_model(specific_heating_demand=0.0), current_temperature=15.0)
    room = room.with_outdoor_forecast([0.0] * 4)
    best = room.best_case(STEP, 4, [23.0] * 4)
    model = room.model
    rise = (model.cop(0.0) * 2.0 * 0.5 - EMHASS_THERMAL_LOSS_KW * 0.5) / model.capacity_kwh_per_k
    assert best[1] == pytest.approx(15.0 + rise)
    assert best[2] == pytest.approx(15.0 + 2 * rise)


def test_inertia_delays_the_first_step_of_heat():
    room = _room(
        model=_model(inertia_hours=1.0, specific_heating_demand=0.0), current_temperature=15.0
    )
    best = room.with_outdoor_forecast([0.0] * 4).best_case(STEP, 4, [23.0] * 4)
    assert best[1] < 15.0
    assert best[2] > best[1]


def test_unreachable_floors_are_lowered_to_the_best_case():
    """A cold room on a cold night: hard floors it cannot reach would be infeasible."""
    room = _room(nominal_power_w=500, current_temperature=17.0).with_outdoor_forecast([-15.0] * 48)
    best = room.best_case(STEP, 48, [23.0] * 48)
    entry = room.to_emhass(NOW, STEP, 48)
    assert entry["min_temperatures"][1] == pytest.approx(best[1] - 0.02)
    assert all(
        floor <= reach for floor, reach in zip(entry["min_temperatures"][1:], best[1:], strict=True)
    )
    assert room.first_unreachable_step(NOW, STEP, 48) is not None


def test_reachable_floors_are_untouched():
    room = _room(nominal_power_w=8000).with_outdoor_forecast([5.0] * 48)
    minimums, _ = room.comfort_band(NOW, STEP, 48)
    assert room.to_emhass(NOW, STEP, 48)["min_temperatures"] == minimums
    assert room.first_unreachable_step(NOW, STEP, 48) is None


def test_outdoor_per_step_holds_the_first_value_before_the_series():
    series = Series(Point(NOW + STEP * (index + 1), float(index)) for index in range(4))
    assert outdoor_per_step(series, NOW, STEP, 3) == [0.0, 0.0, 1.0]


def _inputs(**overrides) -> PayloadInputs:
    defaults = {
        "action": ACTION_MPC,
        "now": NOW,
        "time_step_minutes": 30,
        "horizon_steps": 48,
        "battery": BatteryConfig(),
        "grid": GridConfig(),
        "hybrid_inverter": HybridInverterConfig(),
        "backend_version": "0.18.8",
    }
    return PayloadInputs(**{**defaults, **overrides})


def _load(room: HeatPumpRoomConfig) -> DeferrableLoad:
    return DeferrableLoad(
        subentry_id="hp", name="House", nominal_power_w=2000, operating_hours=0, thermal=room
    )


def _outdoor(value: float) -> Series:
    return Series(Point(NOW + STEP * index, value) for index in range(48))


@pytest.mark.usefixtures("stockholm_timezone")
def test_payload_sends_a_thermal_battery_entry():
    payload = build_payload(
        _inputs(loads=[_load(_room())], outdoor_temperature=_outdoor(5.0))
    ).payload
    entry = payload["def_load_config"][0]
    assert set(entry) == {"thermal_battery"}
    assert entry["thermal_battery"]["volume"] == 10.0
    assert "outdoor_temperature_forecast" in payload


@pytest.mark.usefixtures("stockholm_timezone")
def test_payload_without_a_readable_forecast_fails():
    with pytest.raises(PayloadError, match="heat pump model needs an outdoor temperature"):
        build_payload(_inputs(loads=[_load(_room())]))


@pytest.mark.usefixtures("stockholm_timezone")
def test_payload_warns_when_floors_are_lowered():
    room = _room(nominal_power_w=500, current_temperature=17.0)
    result = build_payload(_inputs(loads=[_load(room)], outdoor_temperature=_outdoor(-15.0)))
    assert any("warmest the heat pump can reach" in warning for warning in result.warnings)


def test_outdoor_per_step_averages_sub_step_points_like_emhass():
    quarter = timedelta(minutes=15)
    series = Series(
        Point(NOW + quarter * index, value) for index, value in enumerate((0.0, -10.0, -4.0))
    )
    assert outdoor_per_step(series, NOW, STEP, 3) == [-5.0, -4.0, -4.0]


def test_without_a_reading_floors_are_held_to_emhass_default_start():
    """EMHASS starts at 20 C; a cold night must still leave it a feasible plan."""
    room = _room(nominal_power_w=500, current_temperature=None).with_outdoor_forecast([-15.0] * 48)
    best = room.best_case(STEP, 48, [23.0] * 48)
    assert best[0] == 20.0
    floors = room.to_emhass(NOW, STEP, 48)["min_temperatures"]
    assert all(floor <= reach for floor, reach in zip(floors[1:], best[1:], strict=True))

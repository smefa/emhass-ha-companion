"""Tank payloads preserve global load indices and optimizer row alignment."""

from datetime import UTC, datetime, time, timedelta

import pytest

from custom_components.emhass_companion.const import ACTION_DAYAHEAD, ACTION_MPC
from custom_components.emhass_companion.models import (
    BatteryConfig,
    DeferrableLoad,
    GridConfig,
    HybridInverterConfig,
)
from custom_components.emhass_companion.payload import PayloadInputs, build_payload
from custom_components.emhass_companion.thermal import ThermalConfig
from custom_components.emhass_companion.water_tank import WaterTankConfig


def _tank(**overrides) -> WaterTankConfig:
    return WaterTankConfig(
        **{
            "current_temperature": 45,
            "volume_litres": 200,
            "nominal_power_w": 2000,
            "standby_loss_w": 30,
            "comfort_temperature": 45,
            "setback_temperature": 37,
            "max_temperature": 52,
            "comfort_start": time(6),
            "comfort_end": time(22),
            "draw_start": time(7),
            "draw_end": time(8),
            "draw_energy_kwh": 2,
            **overrides,
        }
    )


@pytest.mark.usefixtures("stockholm_timezone")
@pytest.mark.parametrize("action", [ACTION_MPC, ACTION_DAYAHEAD])
@pytest.mark.parametrize("minutes", [15, 30, 60])
@pytest.mark.parametrize("mode", ["first", "nearest"])
def test_tank_payload_keeps_mixed_indices_units_and_aligned_consumption(action, minutes, mode):
    now = datetime(2026, 11, 3, 5, 52, tzinfo=UTC)
    step = timedelta(minutes=minutes)
    tank = _tank()
    inputs = PayloadInputs(
        action=action,
        now=now,
        time_step_minutes=minutes,
        horizon_steps=24 * 60 // minutes,
        row_zero_mode=mode,
        battery=BatteryConfig(enabled=False),
        grid=GridConfig(),
        hybrid_inverter=HybridInverterConfig(),
        loads=[
            DeferrableLoad(
                subentry_id="ordinary", name="Ordinary", nominal_power_w=1000, operating_hours=1
            ),
            DeferrableLoad(
                subentry_id="room",
                name="Room",
                nominal_power_w=2000,
                operating_hours=0,
                thermal=ThermalConfig(),
            ),
            DeferrableLoad(
                subentry_id="tank",
                name="Tank",
                nominal_power_w=2000,
                operating_hours=0,
                thermal=tank,
            ),
            DeferrableLoad(
                subentry_id="disabled",
                name="Disabled",
                nominal_power_w=2000,
                operating_hours=0,
                wants_to_run=False,
                thermal=_tank(),
            ),
        ],
    )
    payload = build_payload(inputs).payload
    entries = payload["def_load_config"]
    assert entries[0] == entries[3] == {}
    assert "thermal_config" in entries[1]
    config = entries[2]["thermal_battery"]
    assert config["volume"] == 0.2
    assert config["thermal_loss"] == 0.03
    assert config["efficiency"] == 1
    assert config["draw_off_demand"] == tank.draw_off_demand(
        inputs.row_zero(step), step, inputs.horizon_steps
    )
    assert sum(config["draw_off_demand"]) == pytest.approx(2)
    assert payload["nominal_power_of_deferrable_loads"][2] == 2000
    assert payload["operating_hours_of_each_deferrable_load"][2:] == [0, 0]
    assert payload["number_of_deferrable_loads"] == 4


def test_standby_only_explicitly_selects_tank_mode():
    tank = _tank(draw_energy_kwh=0)
    payload = tank.to_emhass(datetime(2026, 11, 3, tzinfo=UTC), timedelta(minutes=30), 48)
    assert payload["draw_off_demand"] == [0.0] * 48


def _cold_tank_inputs(*, parked: bool = False) -> PayloadInputs:
    """A 200 l tank at 30 C whose 45 C comfort window starts at 06:00."""
    return PayloadInputs(
        action=ACTION_DAYAHEAD,
        now=datetime(2026, 11, 3, 5, 0, tzinfo=UTC),
        time_step_minutes=30,
        horizon_steps=48,
        row_zero_mode="first",
        battery=BatteryConfig(enabled=False),
        grid=GridConfig(),
        hybrid_inverter=HybridInverterConfig(),
        loads=[
            DeferrableLoad(
                subentry_id="tank",
                name="Tank",
                nominal_power_w=2000,
                operating_hours=0,
                wants_to_run=not parked,
                thermal=_tank(current_temperature=30, draw_energy_kwh=0),
            ),
        ],
    )


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_floor_the_heater_cannot_reach_is_lowered_and_reported():
    built = build_payload(_cold_tank_inputs())

    config = built.payload["def_load_config"][0]["thermal_battery"]
    shortfall = built.tank_shortfalls["tank"]
    assert shortfall.current == 30
    assert shortfall.required > shortfall.achievable
    # The request carries what the heater can reach, so EMHASS can still solve.
    lowered = [
        v for v in config["min_temperatures"] if v == pytest.approx(shortfall.achievable - 0.02)
    ]
    assert lowered
    assert any("Tank" in warning and "cannot be reached" in warning for warning in built.warnings)


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_parked_tank_reports_nothing():
    built = build_payload(_cold_tank_inputs(parked=True))

    assert built.tank_shortfalls == {}
    assert not any("cannot be reached" in warning for warning in built.warnings)


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_heat_pump_tank_reaches_a_floor_an_immersion_element_cannot():
    inputs = _cold_tank_inputs()
    inputs.loads[0].thermal = _tank(current_temperature=35, draw_energy_kwh=0)
    assert build_payload(inputs).tank_shortfalls  # the premise: 2 kW of resistance falls short
    inputs.loads[0].thermal = _tank(current_temperature=35, draw_energy_kwh=0, heater_cop=3.0)

    built = build_payload(inputs)

    config = built.payload["def_load_config"][0]["thermal_battery"]
    assert config["efficiency"] == 3.0
    assert built.tank_shortfalls == {}


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_comfort_penalty_sends_a_priced_target_and_no_shortfall_for_it():
    inputs = _cold_tank_inputs()
    inputs.loads[0].thermal = _tank(current_temperature=35, draw_energy_kwh=0, comfort_penalty=10)

    built = build_payload(inputs)

    config = built.payload["def_load_config"][0]["thermal_battery"]
    assert config["penalty_factor"] == 10
    assert config["overshoot_temperature"] == 52
    assert max(config["desired_temperatures"]) == 45
    assert set(config["min_temperatures"]) == {37}
    assert built.tank_shortfalls == {}

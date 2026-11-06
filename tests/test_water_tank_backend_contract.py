"""Offline contract checks against captured real EMHASS v0.18.8 solves.

These tests do not install or execute EMHASS. The fixture records isolated
day-ahead/MPC executions and mocked publication, not a live HA installation.
"""

from datetime import UTC, datetime, time, timedelta
import json
from pathlib import Path

from homeassistant.util import dt as dt_util
import pytest

from custom_components.emhass_companion.water_tank import WaterTankConfig

FIXTURE = Path(__file__).parent / "fixtures" / "water_tank_backend_0188.json"
MODES = ("dayahead", "naive_mpc")
SCENARIOS = ("ideal_heat", "standby", "withdrawal", "mixed", "infeasible_demand")
CAPACITY_KWH_PER_C = 0.2 * 997.0 * 4.184 / 3600


@pytest.fixture
def backend_capture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _case(capture, mode, scenario):
    return next(
        case for case in capture["cases"] if case["mode"] == mode and case["name"] == scenario
    )


def test_capture_provenance_and_coverage(backend_capture):
    backend = backend_capture["backend"]
    assert backend["version"] == "0.18.8"
    assert backend["commit"] == "2ff50ae2d297871d226c69106d2399cdd2e628c4"
    assert backend_capture["step_hours"] == 1.0
    assert backend_capture["input"]["weather"] is None
    assert backend_capture["input"]["pv_w"] == [0.0] * 24
    assert backend_capture["input"]["load_w"] == [300.0] * 24
    assert backend_capture["capacity_kwh_per_c"] == pytest.approx(CAPACITY_KWH_PER_C)
    assert {(case["mode"], case["name"]) for case in backend_capture["cases"]} == {
        (mode, scenario) for mode in MODES for scenario in SCENARIOS
    }
    assert len(backend_capture["cases"]) == len(MODES) * len(SCENARIOS)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_companion_tank_payload_matches_executed_contract(
    backend_capture, monkeypatch, mode, scenario
):
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", UTC)
    heating = scenario in ("ideal_heat", "mixed")
    impossible = scenario == "infeasible_demand"
    config = WaterTankConfig(
        current_temperature=40.0,
        volume_litres=200.0,
        nominal_power_w=2000.0,
        standby_loss_w=50.0 if scenario in ("standby", "withdrawal") else 0.0,
        comfort_temperature=40.0 + 2 / CAPACITY_KWH_PER_C
        if heating
        else 40.0
        if impossible
        else 5.0,
        setback_temperature=40.0 if heating or impossible else 5.0,
        max_temperature=95.0,
        comfort_start=time(1) if heating else time(0),
        comfort_end=time(0),
        draw_start=time(0),
        draw_end=time(0) if impossible else time(1),
        draw_energy_kwh=2400.0 if impossible else 1.0 if scenario == "withdrawal" else 0.0,
    )
    case = _case(backend_capture, mode, scenario)
    executed = case["def_load_config"][case["tank_index"]]["thermal_battery"]
    payload = config.to_emhass(datetime(2026, 11, 3, tzinfo=UTC), timedelta(hours=1), 24)
    assert payload.keys() == executed.keys()
    if impossible:
        # The captured request asked for a floor the heater cannot reach; the
        # backend answered infeasible. Companion now lowers such floors, so
        # that scenario is the only place the two requests differ.
        assert all(
            a <= b
            for a, b in zip(payload["min_temperatures"], executed["min_temperatures"], strict=True)
        )
        assert payload["min_temperatures"] != executed["min_temperatures"]
        payload["min_temperatures"] = executed["min_temperatures"]
    for key in payload:
        if isinstance(payload[key], list):
            assert payload[key] == pytest.approx(executed[key]), key
        else:
            assert payload[key] == executed[key], key
    assert case["nominal_power_w"][case["tank_index"]] == config.nominal_power_w


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("scenario", SCENARIOS[:-1])
def test_solved_temperature_energy_balance(backend_capture, mode, scenario):
    case = _case(backend_capture, mode, scenario)
    assert case["status"] == "Optimal"
    tank_index = case["tank_index"]
    tank = case["def_load_config"][tank_index]["thermal_battery"]
    result = case["result"]
    temperatures = result[f"predicted_temp_heater{tank_index}"]
    raw = case["raw_temperature_c"]
    power = result[f"P_deferrable{tank_index}"]
    demand = result[f"heating_demand_heater{tank_index}"]
    assert len(raw) == len(temperatures) == len(case["timestamps"]) == 24
    assert raw[0] == pytest.approx(40.0)
    assert temperatures == pytest.approx([round(value, 2) for value in raw], abs=1e-8)
    assert demand == pytest.approx(tank["draw_off_demand"])
    for step in range(23):
        expected_delta = (
            power[step] / 1000 - tank["thermal_loss"] - demand[step]
        ) / CAPACITY_KWH_PER_C
        assert raw[step + 1] - raw[step] == pytest.approx(expected_delta, abs=1e-7)
    if scenario in ("ideal_heat", "mixed"):
        assert power[0] == pytest.approx(2000.0, abs=1e-4)
        assert raw[1] - raw[0] == pytest.approx(8.630096791329965, abs=1e-7)
        assert temperatures[1] == 48.63
    else:
        assert power == pytest.approx([0.0] * 24, abs=1e-4)
        first_loss = 0.05 + (1.0 if scenario == "withdrawal" else 0.0)
        assert raw[1] == pytest.approx(40.0 - first_loss / CAPACITY_KWH_PER_C)


@pytest.mark.parametrize("mode", MODES)
def test_global_indexes_and_grid_balance(backend_capture, mode):
    case = _case(backend_capture, mode, "mixed")
    result = case["result"]
    assert case["tank_index"] == 2
    assert all(f"P_deferrable{index}" in case["columns"] for index in range(3))
    assert "predicted_temp_heater1" in case["columns"]
    assert "predicted_temp_heater2" in case["columns"]
    assert "predicted_temp_heater0" not in case["columns"]
    assert "heating_demand_heater2" in case["columns"]
    assert "heating_demand_heater1" not in case["columns"]
    assert sum(result["P_deferrable0"]) / 1000 == pytest.approx(0.5)
    for step, house_load in enumerate(backend_capture["input"]["load_w"]):
        demand = house_load + sum(result[f"P_deferrable{index}"][step] for index in range(3))
        assert result["P_grid_pos"][step] + result["P_grid_neg"][step] == pytest.approx(demand)


@pytest.mark.parametrize("mode", MODES)
def test_impossible_demand_returns_no_schedule(backend_capture, mode):
    case = _case(backend_capture, mode, "infeasible_demand")
    assert case["status"] == "Infeasible"
    assert case["columns"] == ["optim_status"]
    assert case["result"] == {"optim_status": ["Infeasible"] * 24}
    assert case["raw_temperature_c"] is None


def test_captured_publish_helper_preserves_global_metadata(backend_capture):
    publication = backend_capture["publish_mock"]
    assert publication["columns"] == [
        "predicted_temp_heater1",
        "predicted_temp_heater2",
        "heating_demand_heater2",
    ]
    assert len(publication["calls"]) == 3
    for call in publication["calls"]:
        temperature = call["column"].startswith("predicted_temp")
        index = int(call["column"][-1])
        metadata_key = (
            "custom_predicted_temperature_id" if temperature else "custom_heating_demand_id"
        )
        expected = publication["metadata"][metadata_key][index]
        assert {key: call[key] for key in expected} == expected
        assert call["unit_type"] == call["type_var"] == ("temperature" if temperature else "energy")

"""Tests for infeasibility.py's window-energy check and forecast alignment.

The 2026-10-05 failure: the car owed 30 kWh within 3.25 h while the live grid
limit had dropped to 4.2 kW, the battery sat at 11 % and there was no PV.
Every timestep balanced on its own; the window as a whole could not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

from custom_components.emhass_companion.infeasibility import (
    Severity,
    _aligned,
    diagnose,
    headline,
    main,
)

NOW = datetime(2026, 10, 5, 21, 7, tzinfo=UTC)


def _car_payload(**overrides):
    payload = {
        "optimization_time_step": 15,
        "prediction_horizon": 96,
        "pv_power_forecast": [0] * 96,
        "load_power_forecast": [700] * 96,
        "maximum_power_from_grid": 4217,
        "set_use_battery": True,
        "soc_init": 0.115,
        "battery_minimum_state_of_charge": 0.0,
        "battery_nominal_energy_capacity": 23500,
        "battery_discharge_power_max": 10000,
        "battery_discharge_efficiency": 0.95,
        "inverter_is_hybrid": True,
        "inverter_ac_output_max": 10000,
        "number_of_deferrable_loads": 2,
        "nominal_power_of_deferrable_loads": [10000, 1101],
        "operating_hours_of_each_deferrable_load": [3.0, 0.0],
        "operating_timesteps_of_each_deferrable_load": [12, 0],
        "start_timesteps_of_each_deferrable_load": [0, 0],
        "end_timesteps_of_each_deferrable_load": [13, 0],
        "def_current_operating_timesteps": [0, 0],
        "def_current_power": [10000.0, 0.0],
    }
    payload.update(overrides)
    return payload


def test_long_run_under_a_low_grid_limit_is_critical():
    findings = diagnose(_car_payload(), load_names=["Car", "Dishwasher"])

    top = headline(findings)
    assert top is not None and top.code == "window_energy"
    assert top.placeholders["needed"] == "32.3 kWh"  # car 30 + house 13 x 175 Wh
    assert top.placeholders["hours"] == "3.25"
    assert "Car 30.0 kWh" in top.placeholders["loads"]


def test_the_same_run_under_the_full_grid_limit_is_clean():
    findings = diagnose(_car_payload(maximum_power_from_grid=10284))

    assert [f for f in findings if f.code == "window_energy"] == []


def test_completed_timesteps_are_credited():
    # 9 of 12 timesteps already run: 7.5 kWh owed, which 4.2 kW covers.
    findings = diagnose(_car_payload(def_current_operating_timesteps=[9, 0]))

    assert [f for f in findings if f.code == "window_energy"] == []


def test_timestamped_forecasts_are_read_from_now():
    """The integration sends ISO-timestamp mappings, not lists."""
    start = datetime(2026, 10, 5, 21, 0, tzinfo=UTC)
    payload = _car_payload(
        load_power_forecast={
            (start - timedelta(days=1)).isoformat(): 99999,
            start.isoformat(): 700,
            (start + timedelta(hours=1)).isoformat(): 800,
        },
        pv_power_forecast={start.isoformat(): 0},
    )

    aligned = _aligned(payload, NOW)

    assert aligned["load_power_forecast"][0] == 700  # 21:07 floors to 21:00
    assert aligned["load_power_forecast"][4] == 800
    assert len(aligned["pv_power_forecast"]) == 96
    findings = diagnose(payload, now=NOW)
    energy = [f for f in findings if f.code == "window_energy"]
    assert energy and energy[0].severity is Severity.CRITICAL
    assert energy[0].placeholders["house"] != "?"


def test_without_now_mappings_are_left_alone():
    payload = _car_payload(load_power_forecast={NOW.isoformat(): 700})

    assert _aligned(payload, None) is payload
    # Still caught, with the house load unknown.
    energy = [f for f in diagnose(payload) if f.code == "window_energy"]
    assert energy and energy[0].placeholders["house"] == "?"


def test_pv_emhass_forecasts_itself_is_not_counted_as_zero():
    """Without the PV forecast there is no upper bound on supply to prove against."""
    payload = _car_payload()
    del payload["pv_power_forecast"]

    assert [f for f in diagnose(payload) if f.code == "window_energy"] == []


def test_pv_switched_off_counts_as_zero():
    payload = _car_payload(set_use_pv=False)
    del payload["pv_power_forecast"]

    assert [f for f in diagnose(payload) if f.code == "window_energy"]


def test_cli_aligns_a_diagnostics_download_to_its_last_run(tmp_path, capsys):
    start = datetime(2026, 10, 5, 21, 0, tzinfo=UTC)
    payload = _car_payload(
        load_power_forecast={start.isoformat(): 700},
        pv_power_forecast={start.isoformat(): 0},
    )
    bundle = {
        "last_run": {"status": "ok", "infeasible": True, "timestamp": NOW.isoformat()},
        "last_payload": payload,
    }
    source = tmp_path / "diagnostics.json"
    source.write_text(json.dumps(bundle), encoding="utf-8")

    assert main(["check_infeasibility", str(source)]) == 1
    assert "house 2.3 kWh" in capsys.readouterr().out  # 13 x 175 Wh, not "?"

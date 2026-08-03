"""Tests for scripts/check_infeasibility.py.

Imported by path rather than as a package: the script is meant to be a
standalone, copy-pasteable file with zero dependencies, including on this
integration's own package layout.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "check_infeasibility.py"
_spec = importlib.util.spec_from_file_location("check_infeasibility", _SCRIPT_PATH)
check_infeasibility = importlib.util.module_from_spec(_spec)
sys.modules["check_infeasibility"] = check_infeasibility
_spec.loader.exec_module(check_infeasibility)

Severity = check_infeasibility.Severity
load_payload = check_infeasibility.load_payload
run_checks = check_infeasibility.run_checks


def _base_payload(**overrides):
    payload = {
        "optimization_time_step": 15,
        "pv_power_forecast": [0, 1000, 3000, 1000, 0],
        "load_power_forecast": [500, 500, 500, 500, 500],
        "load_cost_forecast": [1, 1, 1, 1, 1],
        "prod_price_forecast": [0.5, 0.5, 0.5, 0.5, 0.5],
        "maximum_power_from_grid": 9000,
        "maximum_power_to_grid": 9000,
        "set_use_battery": True,
        "soc_init": 0.5,
        "soc_final": 0.5,
        "battery_minimum_state_of_charge": 0.1,
        "battery_maximum_state_of_charge": 1.0,
        "battery_charge_power_max": 5000,
        "battery_discharge_power_max": 5000,
        "inverter_is_hybrid": False,
        "number_of_deferrable_loads": 0,
    }
    payload.update(overrides)
    return payload


def _findings(report, severity):
    return [f for f in report.findings if f.severity is severity]


def test_clean_payload_has_no_critical_findings():
    report = run_checks(_base_payload(), [])
    assert _findings(report, Severity.CRITICAL) == []


def test_deferrable_window_shorter_than_run_time_is_critical():
    payload = _base_payload(
        number_of_deferrable_loads=1,
        nominal_power_of_deferrable_loads=[2000],
        minimum_power_of_deferrable_loads=[0],
        operating_hours_of_each_deferrable_load=[2.0],  # needs 8 timesteps @15min
        start_timesteps_of_each_deferrable_load=[0],
        end_timesteps_of_each_deferrable_load=[4],  # only 4 available
        def_current_operating_timesteps=[0],
    )
    report = run_checks(payload, [])
    titles = [f.title for f in _findings(report, Severity.CRITICAL)]
    assert any("doesn't fit its window" in t for t in titles)


def test_deferrable_window_that_fits_is_not_flagged():
    payload = _base_payload(
        number_of_deferrable_loads=1,
        nominal_power_of_deferrable_loads=[2000],
        minimum_power_of_deferrable_loads=[0],
        operating_hours_of_each_deferrable_load=[1.0],  # needs 4 timesteps @15min
        start_timesteps_of_each_deferrable_load=[0],
        end_timesteps_of_each_deferrable_load=[4],
        def_current_operating_timesteps=[0],
    )
    report = run_checks(payload, [])
    assert _findings(report, Severity.CRITICAL) == []


def test_mismatched_forecast_array_lengths_is_critical():
    payload = _base_payload(load_power_forecast=[500, 500, 500])
    report = run_checks(payload, [])
    assert any("disagree on length" in f.title for f in _findings(report, Severity.CRITICAL))


def test_nan_in_forecast_is_critical():
    payload = _base_payload(pv_power_forecast=[0, float("nan"), 3000, 1000, 0])
    report = run_checks(payload, [])
    assert any("null/NaN" in f.title for f in _findings(report, Severity.CRITICAL))


def test_short_per_load_array_is_critical():
    payload = _base_payload(
        number_of_deferrable_loads=2,
        nominal_power_of_deferrable_loads=[2000],  # only 1 entry for 2 loads
        minimum_power_of_deferrable_loads=[0, 0],
        operating_hours_of_each_deferrable_load=[0, 0],
        start_timesteps_of_each_deferrable_load=[0, 0],
        end_timesteps_of_each_deferrable_load=[0, 0],
        deferrable_load_max_cost=[0, 0],
    )
    report = run_checks(payload, [])
    assert any("expected 2" in f.title for f in _findings(report, Severity.CRITICAL))


def test_soc_init_outside_band_is_a_warning_not_critical():
    payload = _base_payload(soc_init=0.02, soc_final=0.02)
    report = run_checks(payload, [])
    assert _findings(report, Severity.CRITICAL) == []
    assert any("outside" in f.title for f in _findings(report, Severity.WARNING))


def test_blocked_ac_charging_is_informational():
    payload = _base_payload(
        inverter_is_hybrid=True,
        inverter_ac_input_max=0,
        inverter_ac_output_max=10000,
    )
    report = run_checks(payload, [])
    assert any("only be charged by PV" in f.title for f in _findings(report, Severity.INFO))


def test_load_exceeding_every_supply_is_a_critical_deficit():
    payload = _base_payload(
        load_power_forecast=[20000, 500, 500, 500, 500],
        maximum_power_from_grid=9000,
        battery_discharge_power_max=5000,
    )
    report = run_checks(payload, [])
    assert any("power deficit" in f.title for f in _findings(report, Severity.CRITICAL))


def test_pv_exceeding_every_sink_is_a_critical_surplus():
    payload = _base_payload(
        pv_power_forecast=[0, 1000, 30000, 1000, 0],
        maximum_power_to_grid=9000,
        battery_charge_power_max=5000,
    )
    report = run_checks(payload, [])
    assert any("PV surplus" in f.title for f in _findings(report, Severity.CRITICAL))


def test_companion_warnings_are_passed_through_as_warnings():
    report = run_checks(_base_payload(), ["Buy price only covers until ..."])
    assert any("Buy price only covers" in f.detail for f in _findings(report, Severity.WARNING))


def test_load_payload_accepts_a_diagnostics_download():
    raw = {
        "last_payload": _base_payload(),
        "warnings": ["something"],
        "last_run": {"status": "infeasible", "infeasible": True},
    }
    payload, warnings, last_run = load_payload(raw)
    assert payload["optimization_time_step"] == 15
    assert warnings == ["something"]
    assert last_run["infeasible"] is True


def test_load_payload_accepts_a_bare_sensor_payload_attribute():
    raw = {"payload": _base_payload(), "warnings": []}
    payload, _warnings, last_run = load_payload(raw)
    assert payload["optimization_time_step"] == 15
    assert last_run == {}


def test_load_payload_accepts_a_raw_payload_with_no_wrapper():
    payload, warnings, _last_run = load_payload(_base_payload())
    assert payload["optimization_time_step"] == 15
    assert warnings == []

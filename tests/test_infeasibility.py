"""Tests for custom_components/emhass_companion/infeasibility.py.

The CLI behaviour is covered, unchanged, by test_check_infeasibility.py through
the script shim. These cover the checks the integration relies on to name a
cause in the repair issue, and the contract between finding codes and the
issue's translations.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re

import pytest

from custom_components.emhass_companion.const import (
    INFEASIBLE_CAUSE_CODES,
    ISSUE_OPTIMIZATION_INFEASIBLE,
)
from custom_components.emhass_companion.infeasibility import (
    HEADLINE_ORDER,
    Finding,
    Severity,
    diagnose,
    format_power,
    headline,
    load_names_from,
    load_payload,
    main,
)

FIXTURE = Path(__file__).parent / "fixtures" / "infeasible_pinned_car.json"
COMPONENT = Path(__file__).parent.parent / "custom_components" / "emhass_companion"


def _payload(**overrides):
    """A small, feasible MPC request: 5 timesteps, battery half full."""
    payload = {
        "optimization_time_step": 15,
        "prediction_horizon": 5,
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
        "battery_nominal_energy_capacity": 10000,
        "battery_discharge_efficiency": 0.95,
        "battery_charge_power_max": 5000,
        "battery_discharge_power_max": 5000,
        "inverter_is_hybrid": False,
        "number_of_deferrable_loads": 0,
    }
    payload.update(overrides)
    return payload


def _pinned(**overrides):
    """Two loads, the first pinned at timestep 0."""
    loads = {
        "number_of_deferrable_loads": 2,
        "nominal_power_of_deferrable_loads": [10000, 1101],
        "minimum_power_of_deferrable_loads": [1400, 0],
        "operating_hours_of_each_deferrable_load": [1.0, 1.25],
        "operating_timesteps_of_each_deferrable_load": [4, 5],
        "start_timesteps_of_each_deferrable_load": [0, 0],
        "end_timesteps_of_each_deferrable_load": [5, 5],
        "def_current_operating_timesteps": [0, 0],
        "def_current_power": [3000.0, 0.0],
    }
    return _payload(**{**loads, **overrides})


def _codes(findings, severity=Severity.CRITICAL):
    return [f.code for f in findings if f.severity is severity]


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# --- the clean case ----------------------------------------------------------


def test_a_feasible_request_has_no_critical_finding():
    assert _codes(diagnose(_payload())) == []
    assert _codes(diagnose(_pinned())) == []
    assert headline(diagnose(_pinned())) is None


# --- timestep0_deficit -------------------------------------------------------


def test_pins_beyond_what_timestep_0_can_supply_are_critical():
    payload = _pinned(
        def_current_power=[10000.0, 1101.0],
        maximum_power_from_grid=7102,
        soc_init=0.11,  # 1 % above the minimum: ~950 W over 15 min
    )
    findings = diagnose(payload, load_names=["Car", "Dishwasher"])
    top = headline(findings)
    assert top is not None
    assert top.code == "timestep0_deficit"
    assert top.placeholders["pins"] == "Car 10.0 kW, Dishwasher 1.1 kW"
    assert top.placeholders["grid"] == "7.1 kW"
    assert top.placeholders["house"] == "500 W"
    assert top.placeholders["needed"] == "11.6 kW"
    # Stored energy, not the 5 kW rating: 0.01 * 10 kWh * 0.95 / 0.25 h.
    assert top.placeholders["battery"] == "380 W (SOC 11.0 %)"


def test_pins_that_fit_are_not_flagged():
    payload = _pinned(def_current_power=[1400.0, 1101.0], maximum_power_from_grid=7102)
    assert "timestep0_deficit" not in _codes(diagnose(payload))


def test_the_deficit_check_counts_the_battery_power_rating_when_it_is_full():
    # 9 kW grid + 5 kW battery covers 13 kW of pins + 500 W house.
    payload = _pinned(def_current_power=[12000.0, 1000.0], soc_init=1.0)
    assert "timestep0_deficit" not in _codes(diagnose(payload))


def test_an_unknown_house_load_still_finds_a_pin_deficit():
    payload = _pinned(def_current_power=[10000.0, 0.0], maximum_power_from_grid=7102, soc_init=0.1)
    del payload["load_power_forecast"]
    top = headline(diagnose(payload))
    assert top is not None
    assert top.code == "timestep0_deficit"
    assert top.placeholders["house"] == "?"


def test_a_per_timestep_grid_limit_is_read_at_timestep_0():
    payload = _pinned(
        def_current_power=[10000.0, 0.0],
        maximum_power_from_grid=[3000, 20000, 20000, 20000, 20000],
        soc_init=0.1,
    )
    assert headline(diagnose(payload)).code == "timestep0_deficit"


def test_a_hybrid_inverter_caps_pv_and_battery_together():
    payload = _pinned(
        def_current_power=[9000.0, 0.0],
        maximum_power_from_grid=4000,
        pv_power_forecast=[6000, 0, 0, 0, 0],
        soc_init=1.0,
        inverter_is_hybrid=True,
        inverter_ac_output_max=5000,
    )
    # 4 kW grid + min(6 kW PV + 5 kW battery, 5 kW inverter) < 9.5 kW.
    assert headline(diagnose(payload)).code == "timestep0_deficit"


def test_day_ahead_skips_the_timestep_0_checks():
    payload = _pinned(def_current_power=[10000.0, 1101.0], maximum_power_from_grid=7102)
    del payload["prediction_horizon"]
    assert "timestep0_deficit" not in _codes(diagnose(payload))


# --- battery_energy ----------------------------------------------------------


def test_an_empty_battery_that_decides_timestep_0_is_critical():
    payload = _payload(
        load_power_forecast=[3000, 500, 500, 500, 500],
        maximum_power_from_grid=2500,
        soc_init=0.11,
    )
    findings = diagnose(payload)
    top = headline(findings)
    assert top is not None
    assert top.code == "battery_energy"
    assert top.placeholders["battery"] == "380 W"
    assert top.placeholders["rated"] == "5.0 kW"
    # The plain deficit check now sees it too, with the cap applied at t0.
    assert "power_deficit" in _codes(findings)


def test_a_battery_with_enough_stored_energy_is_not_flagged():
    payload = _payload(load_power_forecast=[3000, 500, 500, 500, 500], maximum_power_from_grid=2500)
    findings = diagnose(payload)
    assert "battery_energy" not in _codes(findings)
    assert "power_deficit" not in _codes(findings)


def test_a_deficit_the_power_rating_cannot_cover_either_is_not_blamed_on_energy():
    payload = _payload(
        load_power_forecast=[20000, 500, 500, 500, 500],
        maximum_power_from_grid=9000,
        soc_init=0.11,
    )
    codes = _codes(diagnose(payload))
    assert "battery_energy" not in codes
    assert "power_deficit" in codes


def test_day_ahead_keeps_the_power_rating_at_timestep_0():
    payload = _payload(
        load_power_forecast=[3000, 500, 500, 500, 500],
        maximum_power_from_grid=2500,
        soc_init=0.11,
    )
    del payload["prediction_horizon"]
    assert _codes(diagnose(payload)) == []


# --- pin_vs_window -----------------------------------------------------------


def test_a_pin_on_a_load_asked_for_zero_hours_is_critical():
    payload = _pinned(
        operating_hours_of_each_deferrable_load=[0.0, 1.25],
        operating_timesteps_of_each_deferrable_load=[0, 5],
    )
    findings = diagnose(payload, load_names=["Car", "Dishwasher"])
    top = headline(findings)
    assert top is not None
    assert top.code == "pin_vs_window"
    assert top.placeholders["name"] == "Car"
    assert top.placeholders["power"] == "3.0 kW"


def test_a_pin_on_a_load_whose_window_starts_later_is_a_warning():
    payload = _pinned(start_timesteps_of_each_deferrable_load=[1, 0])
    findings = diagnose(payload)
    assert "pin_vs_window" in _codes(findings, Severity.WARNING)
    assert "pin_vs_window" not in _codes(findings)


def test_a_pin_inside_its_window_is_not_flagged():
    assert "pin_vs_window" not in _codes(diagnose(_pinned()), Severity.WARNING)


# --- window_too_short --------------------------------------------------------


def test_a_window_shorter_than_the_run_time_names_the_load():
    payload = _pinned(
        def_current_power=[0.0, 0.0],
        operating_hours_of_each_deferrable_load=[1.0, 2.0],
        end_timesteps_of_each_deferrable_load=[5, 4],
    )
    top = headline(diagnose(payload, load_names=["Car", "Dishwasher"]))
    assert top is not None
    assert top.code == "window_too_short"
    assert top.placeholders == {
        "name": "Dishwasher",
        "hours": "2",
        "needed": "8",
        "start": "0",
        "end": "4",
        "available": "4",
    }


def test_an_end_timestep_of_zero_means_the_end_of_the_horizon():
    """EMHASS's sentinel for "no end constraint", not a zero-width window."""
    payload = _pinned(def_current_power=[0.0, 0.0], end_timesteps_of_each_deferrable_load=[0, 0])
    assert "window_too_short" not in _codes(diagnose(payload))


# --- power_deficit / pv_surplus ----------------------------------------------


def test_power_deficit_placeholders_are_formatted():
    payload = _payload(load_power_forecast=[500, 500, 20000, 500, 500])
    top = headline(diagnose(payload))
    assert top.code == "power_deficit"
    assert top.placeholders["timestep"] == "2"
    assert top.placeholders["house"] == "20.0 kW"


def test_pv_surplus_is_still_found():
    payload = _payload(pv_power_forecast=[0, 1000, 30000, 1000, 0])
    assert headline(diagnose(payload)).code == "pv_surplus"


# --- headline ----------------------------------------------------------------


def test_headline_prefers_the_most_specific_cause():
    def finding(code, severity=Severity.CRITICAL):
        return Finding(severity, code, code, "")

    findings = [
        finding("power_deficit"),
        finding("soc_band", Severity.WARNING),
        finding("timestep0_deficit"),
        finding("window_too_short"),
    ]
    assert headline(findings).code == "timestep0_deficit"
    assert headline([finding("soc_band", Severity.WARNING)]) is None
    assert headline([finding("something_new"), finding("pv_surplus")]).code == "pv_surplus"


# --- the 2026-10-01 22:00 incident -------------------------------------------


def test_the_pinned_car_incident_is_named():
    raw = _fixture()
    payload, warnings, _last_run = load_payload(raw)
    findings = diagnose(payload, load_names=load_names_from(raw), warnings=warnings)

    top = headline(findings)
    assert top is not None
    assert top.code == "timestep0_deficit"
    assert top.placeholders["pins"] == "Car 10.0 kW, Dishwasher 1.1 kW"
    assert top.placeholders["grid"] == format_power(7102)
    assert payload["def_current_power"][:2] == [10000.0, 1101.0]


def test_the_same_system_at_21_45_has_no_critical_finding():
    """The run before the incident solved; the diff recorded what changed."""
    raw = _fixture()
    payload = copy.deepcopy(raw["last_payload"])
    payload["def_current_state"] = [False, False, False, False]
    del payload["def_current_power"]
    payload["maximum_power_from_grid"] = 8758
    payload["soc_init"] = 0.018
    payload["end_timesteps_of_each_deferrable_load"] = [19, 30, 0, 0]
    assert _codes(diagnose(payload, load_names=load_names_from(raw))) == []


def test_the_cli_names_the_cause_and_fails(capsys):
    assert main(["infeasibility.py", str(FIXTURE)]) == 1
    out = capsys.readouterr().out
    assert "Most likely cause: Timestep 0 cannot supply the loads pinned to it" in out
    assert "Car 10.0 kW" in out


# --- translations ------------------------------------------------------------


def _strings():
    return json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))


def test_every_cause_code_can_be_a_headline():
    assert set(HEADLINE_ORDER) == set(INFEASIBLE_CAUSE_CODES)


def test_every_cause_code_has_its_own_issue_translation():
    issues = _strings()["issues"]
    for code in INFEASIBLE_CAUSE_CODES:
        key = f"{ISSUE_OPTIMIZATION_INFEASIBLE}_{code}"
        assert issues[key]["title"]
        assert issues[key]["description"]
    extra = {
        key.removeprefix(f"{ISSUE_OPTIMIZATION_INFEASIBLE}_")
        for key in issues
        if key.startswith(f"{ISSUE_OPTIMIZATION_INFEASIBLE}_")
    }
    assert extra == set(INFEASIBLE_CAUSE_CODES)


# One request per cause code, each producing that code as a CRITICAL.
_CAUSES = {
    "timestep0_deficit": _pinned(
        def_current_power=[10000.0, 1101.0], maximum_power_from_grid=7102, soc_init=0.11
    ),
    "battery_energy": _payload(
        load_power_forecast=[3000, 500, 500, 500, 500], maximum_power_from_grid=2500, soc_init=0.11
    ),
    "pin_vs_window": _pinned(
        operating_hours_of_each_deferrable_load=[0.0, 1.25],
        operating_timesteps_of_each_deferrable_load=[0, 5],
    ),
    "window_too_short": _pinned(
        def_current_power=[0.0, 0.0], operating_hours_of_each_deferrable_load=[1.0, 2.0]
    ),
    "power_deficit": _payload(load_power_forecast=[500, 500, 20000, 500, 500]),
    "pv_surplus": _payload(pv_power_forecast=[0, 1000, 30000, 1000, 0]),
    "array_length": _payload(load_power_forecast=[500, 500, 500]),
    "forecast_nan": _payload(pv_power_forecast=[0, float("nan"), 3000, 1000, 0]),
}


@pytest.mark.parametrize("code", sorted(INFEASIBLE_CAUSE_CODES))
def test_a_cause_fills_every_placeholder_its_translation_uses(code):
    """A placeholder the finding does not supply renders as a literal brace."""
    findings = [f for f in diagnose(_CAUSES[code]) if f.code == code]
    assert findings, f"no {code} finding from its sample request"
    issue = _strings()["issues"][f"{ISSUE_OPTIMIZATION_INFEASIBLE}_{code}"]
    used = set(re.findall(r"\{(\w+)\}", issue["title"] + issue["description"]))
    # The coordinator adds these two to every finding's own placeholders.
    supplied = {"action", "total"}
    for finding in findings:
        if finding.severity is Severity.CRITICAL:
            assert used <= set(finding.placeholders) | supplied, (code, used)

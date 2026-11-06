"""Tests for infeasibility.py's hot-water tank reachability check.

The 2026-11-04 failure: a 200 L tank with a 2 kW element sat at 48 C (its
sensor was a heat pump's, so the element never warmed it) while the request
held a 60 C floor for the next six timesteps. Even flat out the tank reaches
only 49.9 C after one step, so EMHASS finds no plan and the old plan -- which
never heated the tank -- is kept, run after run.
"""

from __future__ import annotations

import copy

import pytest

from custom_components.emhass_companion.infeasibility import Severity, diagnose, headline

HORIZON = 12


def _request(**tank_overrides):
    tank = {
        "volume": 0.2,
        "density": 997.0,
        "heat_capacity": 4.184,
        "thermal_loss": 0.06,
        "efficiency": 1.0,
        "sense": "heat",
        "start_temperature": 48.0,
        "min_temperatures": [60.0] * 6 + [50.0] * (HORIZON - 6),
        "max_temperatures": [65.0] * HORIZON,
        "draw_off_demand": [0.0375] * 6 + [0.0] * (HORIZON - 6),
    }
    tank.update(tank_overrides)
    return {
        "optimization_time_step": 15,
        "prediction_horizon": HORIZON,
        "pv_power_forecast": [0] * HORIZON,
        "load_power_forecast": [300] * HORIZON,
        "maximum_power_from_grid": 9000,
        "maximum_power_to_grid": 9000,
        "set_use_battery": False,
        "number_of_deferrable_loads": 2,
        "nominal_power_of_deferrable_loads": [1000, 2000],
        "def_load_config": [{}, {"thermal_battery": tank}],
    }


def _thermal(findings):
    return [f for f in findings if f.code == "thermal_unreachable"]


def test_a_floor_the_heater_cannot_reach_is_critical():
    findings = diagnose(_request(), load_names=["Washer", "test1"])

    (finding,) = _thermal(findings)
    assert finding.severity is Severity.CRITICAL
    assert finding.placeholders == {
        "name": "test1",
        "required": "60.0",
        "achievable": "49.9",
        "current": "48.0",
        "power": "2.0 kW",
        "timestep": "1",
        "hours": "0.25",
    }
    top = headline(findings)
    assert top is not None and top.code == "thermal_unreachable"


def test_the_first_unreachable_timestep_is_reported():
    """A floor that only rises later is flagged where it first cannot be met."""
    request = _request(
        start_temperature=50.0,
        min_temperatures=[50.0] * 4 + [70.0] * (HORIZON - 4),
        max_temperatures=[95.0] * HORIZON,
    )

    (finding,) = _thermal(diagnose(request))
    assert finding.placeholders["timestep"] == "4"
    assert finding.placeholders["required"] == "70.0"


def test_a_reachable_floor_is_not_flagged():
    request = _request(start_temperature=59.0)  # +1.9 K per step with this draw-off and loss

    assert _thermal(diagnose(request)) == []


def test_a_tank_exactly_at_its_floor_is_reachable():
    request = _request(
        start_temperature=50.0,
        min_temperatures=[50.0] * HORIZON,
    )

    assert _thermal(diagnose(request)) == []


def test_standby_loss_and_draw_off_count_against_the_heater():
    """Flat out, 2 kW lifts 48 C to 50.16 C in one step; a 0.1 kWh draw-off stops it."""
    floors = [48.0, 50.1] + [48.0] * (HORIZON - 2)
    free = _request(min_temperatures=floors, draw_off_demand=[0.0], thermal_loss=0.0)
    drained = _request(min_temperatures=floors, draw_off_demand=[0.1], thermal_loss=0.0)

    assert _thermal(diagnose(free)) == []
    assert len(_thermal(diagnose(drained))) == 1


def test_a_tank_starting_above_its_ceiling_is_not_clamped_to_it():
    """EMHASS relaxes a maximum the tank starts past; its heat still counts."""
    request = _request(
        start_temperature=80.0,
        min_temperatures=[60.0] * HORIZON,
        max_temperatures=[65.0] * HORIZON,
        draw_off_demand=[0.0, 2.0] + [0.0] * (HORIZON - 2),
    )

    assert _thermal(diagnose(request)) == []


def test_heat_above_the_ceiling_does_not_count_towards_later_floors():
    """The heater is throttled at the ceiling, so it cannot bank the overshoot."""
    floors = [55.0] * 8 + [63.0] * (HORIZON - 8)
    draw = [0.0] * 5 + [2.0] + [0.0] * (HORIZON - 6)
    request = _request(
        start_temperature=55.0,
        thermal_loss=0.0,
        min_temperatures=floors,
        max_temperatures=[65.0] * HORIZON,
        draw_off_demand=draw,
    )
    uncapped = _request(
        start_temperature=55.0,
        thermal_loss=0.0,
        min_temperatures=floors,
        max_temperatures=[95.0] * HORIZON,
        draw_off_demand=draw,
    )

    (finding,) = _thermal(diagnose(request))
    assert finding.placeholders["timestep"] == "8"
    assert _thermal(diagnose(uncapped)) == []


def test_a_day_ahead_request_is_checked_too():
    """Day-ahead sends timestamped forecasts and no prediction_horizon."""
    request = _request()
    del request["prediction_horizon"]
    request["pv_power_forecast"] = {"2026-11-04T18:00:00+00:00": 0.0}
    request["load_power_forecast"] = {"2026-11-04T18:00:00+00:00": 300.0}

    (finding,) = _thermal(diagnose(request))
    assert finding.placeholders["timestep"] == "1"


def test_a_short_draw_off_profile_is_tiled_like_emhass_does():
    """A one-value profile applies at every timestep, as EMHASS tiles it."""
    request = _request(
        start_temperature=50.0,
        min_temperatures=[50.0] * HORIZON,
        draw_off_demand=[0.5],  # exactly the heater's 0.5 kWh per step
        thermal_loss=0.0,
    )
    assert _thermal(diagnose(request)) == []

    request["def_load_config"][1]["thermal_battery"]["draw_off_demand"] = [0.6]
    (finding,) = _thermal(diagnose(request))
    assert finding.placeholders["timestep"] == "1"


@pytest.mark.parametrize(
    "overrides",
    [
        {"efficiency": None},  # heat-pump COP: no constant factor to bound with
        {"thermal_inertia_time_constant": 0.5},
        {"solar_absorption_area": 2.0},
        {"sense": "cool"},
        {"draw_off_demand": []},
        {"min_temperatures": None},
        {"start_temperature": "warm"},
    ],
)
def test_tanks_it_cannot_bound_are_skipped(overrides):
    assert _thermal(diagnose(_request(**overrides))) == []


@pytest.mark.parametrize("power", [0, None])
def test_a_load_without_power_is_skipped(power):
    request = _request()
    request["nominal_power_of_deferrable_loads"][1] = power

    assert _thermal(diagnose(request)) == []


def test_requests_without_a_thermal_battery_are_untouched():
    request = _request()
    request["def_load_config"] = [{}, {}]
    assert _thermal(diagnose(request)) == []
    del request["def_load_config"]
    assert _thermal(diagnose(request)) == []


def test_the_request_is_not_modified():
    request = _request()
    before = copy.deepcopy(request)

    diagnose(request)

    assert request == before


# --- heating window and start cap on a tank ----------------------------------


def _tank_request(**overrides):
    """A 200 L tank with a 2 kW element, 1 kW minimum and little demand."""
    tank = {
        "start_temperature": 55.0,
        "min_temperatures": [50.0] * HORIZON,
        "max_temperatures": [57.0] * HORIZON,
        "draw_off_demand": [0.1] * HORIZON,
    }
    payload_keys = {
        k: overrides.pop(k)
        for k in list(overrides)
        if k.endswith("_deferrable_load")
        or k.endswith("_deferrable_loads")
        or k == "def_current_state"
    }
    tank.update(overrides)
    payload = _request(**tank)
    payload["minimum_power_of_deferrable_loads"] = [0, 1000]
    payload["set_deferrable_max_startups"] = [0, 1]
    payload.update(payload_keys)
    return payload


def _codes(payload, code):
    return [f for f in diagnose(payload, load_names=["Washer", "Tank"]) if f.code == code]


def test_a_window_that_closes_before_a_later_floor_is_critical():
    payload = _tank_request(
        start_temperature=40.0,
        min_temperatures=[30.0] * 9 + [52.0] * 3,
        max_temperatures=[65.0] * HORIZON,
        draw_off_demand=[0.0] * HORIZON,
        start_timesteps_of_each_deferrable_load=[0, 0],
        end_timesteps_of_each_deferrable_load=[0, 2],
    )

    (finding,) = _codes(payload, "thermal_window")
    assert finding.severity is Severity.CRITICAL
    assert finding.placeholders["name"] == "Tank"
    assert finding.placeholders["required"] == "52.0"
    assert (finding.placeholders["start"], finding.placeholders["end"]) == ("0", "2")
    assert not _codes(payload, "thermal_unreachable")


def test_a_window_wide_enough_for_the_floor_is_not_flagged():
    payload = _tank_request(
        start_temperature=40.0,
        min_temperatures=[30.0] * 9 + [52.0] * 3,
        max_temperatures=[65.0] * HORIZON,
        draw_off_demand=[0.0] * HORIZON,
        start_timesteps_of_each_deferrable_load=[0, 0],
        end_timesteps_of_each_deferrable_load=[0, 9],
    )

    assert not _codes(payload, "thermal_window")


def test_a_floor_unreachable_even_without_a_window_is_left_to_the_other_check():
    payload = _tank_request(
        start_temperature=40.0,
        min_temperatures=[60.0] * HORIZON,
        start_timesteps_of_each_deferrable_load=[0, 0],
        end_timesteps_of_each_deferrable_load=[0, 2],
    )

    assert not _codes(payload, "thermal_window")
    assert _codes(payload, "thermal_unreachable")


def test_a_load_already_running_is_not_held_to_its_window():
    payload = _tank_request(
        start_temperature=40.0,
        min_temperatures=[30.0] * 9 + [52.0] * 3,
        max_temperatures=[65.0] * HORIZON,
        draw_off_demand=[0.0] * HORIZON,
        start_timesteps_of_each_deferrable_load=[0, 0],
        end_timesteps_of_each_deferrable_load=[0, 2],
        def_current_state=[False, True],
    )

    assert not _codes(payload, "thermal_window")

"""Ownership, compatibility and projection contracts for effective settings."""

from datetime import UTC, datetime

import pytest

from custom_components.emhass_companion.const import ACTION_DAYAHEAD, ACTION_MPC
from custom_components.emhass_companion.effective_settings import (
    SETTINGS,
    resolve_effective_settings,
)
from custom_components.emhass_companion.models import (
    BatteryConfig,
    DeferrableLoad,
    GridConfig,
    HybridInverterConfig,
)
from custom_components.emhass_companion.payload import PayloadInputs, build_payload
from custom_components.emhass_companion.profiles.schema import ProfileError


def _resolve(**overrides):
    arguments = {
        "battery": BatteryConfig(enabled=True, capacity_wh=12000),
        "grid": GridConfig(),
        "hybrid": HybridInverterConfig(),
        "time_step_minutes": 15,
        "cost_fun": "profit",
        "row_zero_mode": "first",
        "num_lags": 96,
        "profile_settings": {},
        "backend_version": "0.18.8",
    }
    return resolve_effective_settings(**(arguments | overrides))


@pytest.mark.parametrize("history_days", [2, 9, 30])
def test_ml_model_defaults_and_history_are_explicit(history_days):
    settings = _resolve(
        profile_settings={
            "load_forecast_method": "mlforecaster",
            "sensor_power_load_no_var_loads": "sensor.house_load",
            "historic_days_to_retrieve": history_days,
        }
    )
    runtime = settings.runtime()
    assert runtime["optimization_time_step"] == 15
    assert runtime["num_lags"] == 96
    assert runtime["var_model"] == "sensor.house_load"
    assert runtime["model_type"] == "long_train_data"
    assert runtime["sklearn_model"] == "KNeighborsRegressor"
    assert runtime["split_date_delta"] == "48h"
    assert runtime["perform_backtest"] is False
    assert runtime["mlforecaster_weather_features"] == []
    assert runtime["historic_days_to_retrieve"] == max(9, history_days)
    assert settings.persistent()["historic_days_to_retrieve"] == max(9, history_days)


def test_profiles_override_supported_defaults_but_not_invariants():
    settings = _resolve(
        profile_settings={
            "lp_solver_timeout": 90,
            "set_use_pv": False,
            "battery_nominal_energy_capacity": 14000,
            "optimization_time_step": 60,
            "method_ts_round": "last",
            "heat_topology": {"sources": ["inherited"]},
            "number_of_batteries": 3,
            "maximum_power_from_grid": 999999,
        }
    )
    runtime = settings.runtime()
    assert runtime["lp_solver_timeout"] == 90
    assert runtime["battery_nominal_energy_capacity"] == 14000
    assert runtime["set_use_pv"] is False
    assert runtime["optimization_time_step"] == 15
    assert runtime["method_ts_round"] == "first"
    assert runtime["heat_topology"] == {}
    assert runtime["number_of_batteries"] == 1
    assert runtime["maximum_power_from_grid"] == GridConfig().import_max_w
    assert len(settings.warnings) == 5


@pytest.mark.parametrize("version", [None, "unknown", "development", "0.17.9"])
def test_unknown_and_minimum_versions_do_not_receive_newer_settings(version):
    runtime = _resolve(backend_version=version).runtime()
    assert runtime["heat_topology"] == {}
    assert runtime["lp_solver_timeout"] == 45
    for name in (
        "number_of_batteries",
        "battery_charge_power_derating",
        "battery_soc_final_reward_factor",
        "inverter_power_curve_dc_ac",
        "shared_thermal_tanks",
        "tariff_schedule_time_zone",
        "cop_solver",
    ):
        assert name not in runtime


@pytest.mark.parametrize(
    ("version", "name"),
    [
        ("0.18.0", "number_of_batteries"),
        ("0.18.2", "capacity_charge_interval_timesteps"),
        ("0.18.3", "battery_charge_power_derating"),
        ("0.18.5", "battery_soc_final_reward_factor"),
        ("0.18.5", "inverter_power_curve_dc_ac"),
        ("0.18.5", "tariff_schedule_time_zone"),
        ("0.18.6", "shared_thermal_tanks"),
        ("0.18.6", "cop_solver"),
    ],
)
def test_capabilities_start_at_the_verified_version(version, name):
    assert name in _resolve(backend_version=version).runtime()
    assert SETTINGS[name].minimum_version == version


def test_unsupported_profile_feature_cannot_bypass_version_gate():
    settings = _resolve(
        backend_version="0.18.4",
        profile_settings={
            "inverter_power_curve_dc_ac": [[0, 0], [1000, 900]],
        },
    )
    assert "inverter_power_curve_dc_ac" not in settings.runtime()
    assert "inverter_power_curve_dc_ac" not in settings.profile_overrides
    assert "requires EMHASS 0.18.5" in settings.warnings[0]


@pytest.mark.parametrize("action", [ACTION_MPC, ACTION_DAYAHEAD])
@pytest.mark.parametrize("interval", [1, 4])
def test_capacity_interval_does_not_inherit_backend_configuration(action, interval):
    inputs = PayloadInputs(
        action=action,
        now=datetime(2026, 11, 4, 11, 8, tzinfo=UTC),
        time_step_minutes=15,
        horizon_steps=96,
        battery=BatteryConfig(),
        grid=GridConfig(),
        hybrid_inverter=HybridInverterConfig(),
        backend_version="0.18.8",
        network_demand_charge_configured=True,
        demand_charge_rate_per_kw=45.0,
        capacity_interval_timesteps=interval,
    )
    payload = build_payload(inputs).payload
    assert payload["capacity_charge_interval_timesteps"] == (
        interval if action == ACTION_MPC else 1
    )
    assert "capacity_charge_interval_timesteps" not in _resolve().persistent()


@pytest.mark.parametrize("version", [None, "invalid", "0.18.1"])
def test_capacity_interval_default_requires_runtime_support(version):
    assert "capacity_charge_interval_timesteps" not in _resolve(backend_version=version).runtime()


def test_explicit_false_zero_and_empty_are_not_replaced_by_backend_values():
    runtime = _resolve(profile_settings={"inverter_stress_cost": 0, "set_use_pv": False}).runtime()
    assert runtime["heat_topology"] == {}
    assert runtime["shared_thermal_tanks"] == []
    assert runtime["inverter_power_curve_dc_ac"] == []
    assert runtime["battery_charge_power_derating"] == []
    assert runtime["compute_curtailment"] is False
    assert runtime["set_use_pv"] is False
    assert runtime["inverter_stress_cost"] == 0


def test_none_does_not_clear_and_projection_copies_do_not_share_lists():
    settings = _resolve(profile_settings={"inverter_stress_cost": None})
    runtime = settings.runtime()
    runtime["inverter_power_curve_dc_ac"].append([1, 1])
    runtime["soc_init"] = 0.7
    assert settings.persistent()["inverter_power_curve_dc_ac"] == []
    assert settings.runtime()["inverter_stress_cost"] == 0
    assert "soc_init" not in settings.persistent()
    assert "shared_thermal_tanks" not in settings.persistent()


def test_empty_peak_periods_clear_in_requests_but_save_a_renderable_placeholder():
    """EMHASS's config page cannot render ``{}`` here and stops building sections."""
    settings = _resolve()
    assert settings.runtime()["load_peak_hour_periods"] == {}
    saved = settings.persistent()["load_peak_hour_periods"]
    assert saved
    assert all(isinstance(value, list) for value in saved.values())
    profile = {"load_peak_hour_periods": {"p": [{"start": "06:00"}, {"end": "09:00"}]}}
    kept = _resolve(profile_settings=profile).persistent()["load_peak_hour_periods"]
    assert kept == profile["load_peak_hour_periods"]


def test_credentials_live_values_and_unknown_keys_are_not_action_settings():
    settings = _resolve(
        profile_settings={
            "long_lived_token": "secret-value",
            "hass_url": "private-host",
            "influxdb_password": "secret-value",
            "soc_init": 0.7,
            "load_power_forecast": [123],
            "data_path": "/private",
            "logging_level": "DEBUG",
            "unrecognised_key": 42,
        }
    )
    assert not any(
        name in settings.runtime()
        for name in (
            "long_lived_token",
            "hass_url",
            "influxdb_password",
            "soc_init",
            "load_power_forecast",
            "data_path",
            "logging_level",
            "unrecognised_key",
        )
    )
    assert "secret-value" not in str(settings)
    assert "private-host" not in str(settings)


def test_forecast_sensor_model_and_lags_share_the_effective_configuration():
    settings = _resolve(
        profile_settings={
            "sensor_power_load_no_var_loads": "sensor.house",
            "load_forecast_method": "mlforecaster",
        }
    )
    assert settings.runtime()["var_model"] == "sensor.house"
    assert settings.runtime()["num_lags"] == 96
    assert settings.persistent()["sensor_power_load_no_var_loads"] == "sensor.house"


def test_var_model_follows_the_load_sensor_and_a_different_override_is_ignored():
    """Prediction indexes the retrieved history by `var_model`; it must be the same series."""
    settings = _resolve(
        profile_settings={
            "sensor_power_load_no_var_loads": "sensor.house",
            "var_model": "sensor.other",
        }
    )
    assert settings.runtime()["var_model"] == "sensor.house"
    assert settings.persistent()["var_model"] == "sensor.house"
    assert "var_model" not in settings.profile_overrides
    assert any("'var_model'" in warning for warning in settings.warnings)


def test_a_var_model_override_equal_to_the_sensor_is_accepted_silently():
    settings = _resolve(
        profile_settings={
            "sensor_power_load_no_var_loads": "sensor.house",
            "var_model": "sensor.house",
        }
    )
    assert settings.runtime()["var_model"] == "sensor.house"
    assert not any("'var_model'" in warning for warning in settings.warnings)


@pytest.mark.parametrize("value", ["unavailable", "", [30]])
def test_unusable_ml_history_days_is_a_profile_error(value):
    with pytest.raises(ProfileError, match="historic_days_to_retrieve"):
        _resolve(
            profile_settings={
                "load_forecast_method": "mlforecaster",
                "sensor_power_load_no_var_loads": "sensor.house",
                "historic_days_to_retrieve": value,
            }
        )


def test_non_ml_forecasts_do_not_add_unused_model_defaults():
    names = (
        "model_type",
        "sklearn_model",
        "split_date_delta",
        "perform_backtest",
        "mlforecaster_weather_features",
    )
    settings = _resolve()
    assert all(name not in settings.runtime() for name in names)
    assert all(name not in settings.persistent() for name in names)
    assert (
        _resolve(profile_settings={"sklearn_model": "Ridge"}).runtime()["sklearn_model"] == "Ridge"
    )


def test_profile_curve_is_cleared_when_hybrid_is_disabled():
    settings = _resolve(
        hybrid=HybridInverterConfig(enabled=False),
        profile_settings={"inverter_power_curve_dc_ac": [[0, 0], [1000, 900]]},
    )
    assert settings.runtime()["inverter_power_curve_dc_ac"] == []
    assert "inverter_power_curve_dc_ac" not in settings.profile_overrides
    assert "requires a hybrid inverter" in settings.warnings[0]


@pytest.mark.parametrize("action", [ACTION_MPC, ACTION_DAYAHEAD])
def test_conflicting_backend_values_are_overridden_for_both_actions(action):
    inputs = PayloadInputs(
        action=action,
        now=datetime(2026, 11, 4, 11, 8, tzinfo=UTC),
        time_step_minutes=15,
        horizon_steps=96,
        battery=BatteryConfig(enabled=True),
        grid=GridConfig(),
        hybrid_inverter=HybridInverterConfig(),
        backend_version="0.18.8",
        loads=[
            DeferrableLoad(
                subentry_id="active", name="Active", nominal_power_w=1000, operating_hours=1
            ),
            DeferrableLoad(
                subentry_id="parked",
                name="Parked",
                nominal_power_w=500,
                wants_to_run=False,
                operating_hours=1,
            ),
        ],
    )
    payload = build_payload(inputs).payload
    conflicting = {
        "heat_topology": {"sources": ["backend"]},
        "shared_thermal_tanks": [{"id": "old"}],
        "def_load_config": [{"thermal_config": {}}],
        "is_electric_load": [False],
        "number_of_batteries": 3,
        "compute_curtailment": True,
        "battery_charge_power_derating": [[0.5, 0.1]],
        "inverter_power_curve_dc_ac": [[1000, 800]],
        "inverter_stress_cost": 99,
        "deferrable_load_groups": [{"names": ["old"]}],
        "cost_forecast_per_deferrable_load": [[999]],
        "lp_solver_timeout": 1,
        "method_ts_round": "last",
    }
    merged = conflicting | payload
    for name in conflicting:
        assert merged[name] == payload[name]
        assert merged[name] != conflicting[name]
    assert payload["method_ts_round"] == inputs.row_zero_mode
    assert payload["is_electric_load"] == [True, True]
    assert payload["def_load_config"] == [{}, {}]
    assert payload["cost_forecast_per_deferrable_load"] == [None, None]
    assert payload["operating_hours_of_each_deferrable_load"] == [1, 0]
    assert "soc_init" not in _resolve().persistent()


def test_the_pv_sensor_reaches_the_request():
    """EMHASS's history fetch for a series-less MPC run reads it."""
    settings = _resolve(profile_settings={"sensor_power_photovoltaics": "sensor.pv_now"})
    assert settings.runtime()["sensor_power_photovoltaics"] == "sensor.pv_now"


def test_solver_limits_default_and_follow_the_advanced_setting():
    default = _resolve().runtime()
    assert default["lp_solver_timeout"] == 45
    assert default["lp_solver_mip_rel_gap"] == 0.01

    chosen = _resolve(
        solver_settings={"lp_solver_timeout": 120, "lp_solver_mip_rel_gap": 0.02, "other": 1}
    )
    assert chosen.runtime()["lp_solver_timeout"] == 120
    assert chosen.runtime()["lp_solver_mip_rel_gap"] == 0.02
    assert chosen.persistent()["lp_solver_timeout"] == 120
    assert "other" not in chosen.runtime()


def test_solver_advanced_setting_outranks_a_profile():
    settings = _resolve(
        profile_settings={"lp_solver_timeout": 10},
        solver_settings={"lp_solver_timeout": 90},
    )
    assert settings.runtime()["lp_solver_timeout"] == 90

"""Resolve Companion-owned settings independently of action-specific live inputs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from packaging.version import InvalidVersion, Version

from .models import BatteryConfig, GridConfig, HybridInverterConfig
from .profiles.schema import ProfileError


@dataclass(frozen=True, slots=True)
class Setting:
    """An allowlisted action setting and its persistent configuration destination."""

    default: Any = None
    minimum_version: str = "0.17.9"
    persistent_name: str | None = None
    profile_override: bool = True


_DEFAULTS: dict[str, Any] = {
    "lp_solver_timeout": 45,
    "lp_solver_mip_rel_gap": 0.01,
    "num_threads": 0,
    "historic_days_to_retrieve": 2,
    "load_negative": False,
    "set_zero_min": True,
    "sensor_replace_zero": [],
    "sensor_linear_interp": [],
    "load_forecast_method": "typical",
    "weather_forecast_method": "open-meteo",
    "weather_forecast_pv_quantile_bias": 0.0,
    "open_meteo_cache_max_age": 30,
    "set_use_adjusted_pv": False,
    "adjusted_pv_regression_model": "LassoRegression",
    "adjusted_pv_solar_elevation_threshold": 10,
    "adjusted_pv_model_max_age": 24,
    "set_use_battery_identification": False,
    "battery_identification_trust_tier": "observe",
    "battery_identification_model_max_age": 24,
    "load_cost_forecast_method": "hp_hc_periods",
    "production_price_forecast_method": "constant",
    "load_peak_hour_periods": {},
    "load_peak_hours_cost": 0.1907,
    "load_offpeak_hours_cost": 0.1419,
    "photovoltaic_production_sell_price": 0.1419,
    "tariff_schedule_time_zone": "",
    "modules_per_string": [1],
    "strings_per_inverter": [1],
    "inverter_stress_cost": 0.0,
    "inverter_stress_segments": 10,
    "set_total_pv_sell": False,
    "cop_solver": "static",
    "cop_solver_tolerance": 0.5,
    "cop_hx_approach": 5.0,
}

# An empty value clears a feature in a request, but EMHASS's configuration page
# cannot render some of them: an empty ``load_peak_hour_periods`` stops it
# building every section after Tariff (Battery included). The saved copy gets
# EMHASS's own placeholder instead; it is inert while the request sends ``{}``.
_PERSISTENT_PLACEHOLDERS: dict[str, Any] = {
    "load_peak_hour_periods": {"period_hp_template": [{"start": "02:54"}, {"end": "15:24"}]},
}

SETTINGS = {name: Setting(default, persistent_name=name) for name, default in _DEFAULTS.items()}
for _name in (
    "optimization_time_step",
    "costfun",
    "set_use_pv",
    "num_lags",
    "model_type",
    "sklearn_model",
    "split_date_delta",
    "perform_backtest",
    "mlforecaster_weather_features",
    "var_model",
    "sensor_power_load_no_var_loads",
    "sensor_power_photovoltaics",
    "pv_module_model",
    "pv_inverter_model",
    "surface_tilt",
    "surface_azimuth",
    "set_use_battery",
    "battery_nominal_energy_capacity",
    "battery_charge_power_max",
    "battery_discharge_power_max",
    "battery_minimum_state_of_charge",
    "battery_maximum_state_of_charge",
    "battery_target_state_of_charge",
    "battery_charge_efficiency",
    "battery_discharge_efficiency",
    "set_nodischarge_to_grid",
    "set_nocharge_from_grid",
    "set_battery_first_priority",
    "set_battery_dynamic",
    "battery_dynamic_max",
    "battery_dynamic_min",
    "weight_battery_discharge",
    "weight_battery_charge",
    "battery_soc_deficit_threshold",
    "battery_soc_deficit_cost",
    "battery_soc_surplus_threshold",
    "battery_soc_surplus_cost",
    "battery_stress_cost",
    "battery_stress_segments",
    "inverter_is_hybrid",
    "inverter_ac_output_max",
    "inverter_ac_input_max",
    "inverter_efficiency_dc_ac",
    "inverter_efficiency_ac_dc",
    "compute_curtailment",
    "maximum_power_from_grid",
    "maximum_power_to_grid",
):
    SETTINGS[_name] = Setting(persistent_name=_name)
SETTINGS.update(
    {
        "optimization_time_step": Setting(
            persistent_name="optimization_time_step", profile_override=False
        ),
        "method_ts_round": Setting(persistent_name="method_ts_round", profile_override=False),
        "heat_topology": Setting({}, persistent_name="heat_topology", profile_override=False),
        "number_of_batteries": Setting(1, "0.18.0", "number_of_batteries", False),
        "battery_charge_power_derating": Setting([], "0.18.3", "battery_charge_power_derating"),
        "battery_soc_final_reward_factor": Setting(
            0.0, "0.18.5", "battery_soc_final_reward_factor"
        ),
        "inverter_power_curve_dc_ac": Setting([], "0.18.5", "inverter_power_curve_dc_ac"),
        "inverter_power_curve_ac_dc": Setting([], "0.18.5", "inverter_power_curve_ac_dc"),
        "shared_thermal_tanks": Setting([], "0.18.6", profile_override=False),
        "capacity_charge_interval_timesteps": Setting(1, "0.18.2", profile_override=False),
        "tariff_schedule_time_zone": Setting("", "0.18.5", "tariff_schedule_time_zone"),
        "cop_solver": Setting("static", "0.18.6", "cop_solver"),
        "cop_solver_tolerance": Setting(0.5, "0.18.6", "cop_solver_tolerance"),
        "cop_hx_approach": Setting(5.0, "0.18.6", "cop_hx_approach"),
        "maximum_power_from_grid": Setting(
            persistent_name="maximum_power_from_grid", profile_override=False
        ),
        "maximum_power_to_grid": Setting(
            persistent_name="maximum_power_to_grid", profile_override=False
        ),
    }
)


ML_SETTING_NAMES = frozenset(
    {
        "optimization_time_step",
        "method_ts_round",
        "load_forecast_method",
        "sensor_power_load_no_var_loads",
        "var_model",
        "num_lags",
        "model_type",
        "sklearn_model",
        "split_date_delta",
        "perform_backtest",
        "mlforecaster_weather_features",
        "historic_days_to_retrieve",
        "load_negative",
        "set_zero_min",
        "sensor_replace_zero",
        "sensor_linear_interp",
        "weather_forecast_method",
        "open_meteo_cache_max_age",
    }
)

_ML_DEFAULTS = {
    "model_type": "long_train_data",
    "sklearn_model": "KNeighborsRegressor",
    "split_date_delta": "48h",
    "perform_backtest": False,
    "mlforecaster_weather_features": [],
}

ML_MODEL_SETTING_NAMES = ML_SETTING_NAMES - {
    "method_ts_round",
    "open_meteo_cache_max_age",
    "weather_forecast_method",
}


@dataclass(frozen=True, slots=True)
class EffectiveSettings:
    """Separate projections; callers receive copies so live updates cannot leak back."""

    values: dict[str, Any]
    profile_overrides: dict[str, Any]
    warnings: tuple[str, ...]

    def runtime(self) -> dict[str, Any]:
        """Project using public runtime names, not EMHASS's internal legacy names."""
        return deepcopy(self.values)

    def persistent(self) -> dict[str, Any]:
        """Project only allowlisted static values, never the completed action payload."""
        return {
            SETTINGS[name].persistent_name: deepcopy(
                _PERSISTENT_PLACEHOLDERS[name]
                if not value and name in _PERSISTENT_PLACEHOLDERS
                else value
            )
            for name, value in self.values.items()
            if SETTINGS[name].persistent_name is not None
        }


def resolve_effective_settings(
    *,
    battery: BatteryConfig,
    grid: GridConfig,
    hybrid: HybridInverterConfig,
    time_step_minutes: int,
    cost_fun: str,
    row_zero_mode: str,
    num_lags: int,
    profile_settings: dict[str, Any],
    backend_version: str | None,
    send_derating: bool = True,
    send_soc_final_reward: bool = True,
    use_pv: bool = False,
) -> EffectiveSettings:
    """Apply defaults, Companion settings, supported profiles, then invariants.

    Unknown versions get the integration's minimum-supported capability set.
    Backend connections, credentials and unrecognised keys never enter either
    projection, including when contributed by a custom profile.
    """
    values = {
        name: deepcopy(rule.default) for name, rule in SETTINGS.items() if rule.default is not None
    }
    values.update(_battery_settings(battery))
    values.update(_hybrid_inverter_settings(hybrid))
    values.update(
        {
            "optimization_time_step": time_step_minutes,
            "costfun": cost_fun,
            "method_ts_round": row_zero_mode,
            "num_lags": num_lags,
            "set_use_pv": use_pv,
            "compute_curtailment": bool(grid.compute_curtailment),
            "maximum_power_from_grid": grid.import_max_w,
            "maximum_power_to_grid": grid.export_max_w,
        }
    )
    overrides: dict[str, Any] = {}
    warnings: list[str] = []
    for name, value in profile_settings.items():
        rule = SETTINGS.get(name)
        if rule is None or not rule.profile_override or value is None:
            warnings.append(f"Ignored unsupported profile setting {name!r}.")
            continue
        overrides[name] = deepcopy(value)
    values.update(overrides)
    if values["load_forecast_method"] == "mlforecaster":
        for name, default in _ML_DEFAULTS.items():
            values.setdefault(name, deepcopy(default))
        try:
            history_days = int(values["historic_days_to_retrieve"])
        except (TypeError, ValueError):
            raise ProfileError(
                "historic_days_to_retrieve must be a whole number of days, got "
                f"{values['historic_days_to_retrieve']!r}."
            ) from None
        values["historic_days_to_retrieve"] = max(9, history_days)
    if not values["inverter_is_hybrid"]:
        for name in ("inverter_power_curve_dc_ac", "inverter_power_curve_ac_dc"):
            if overrides.get(name):
                overrides.pop(name)
                values[name] = []
                warnings.append(f"Ignored profile setting {name!r}: requires a hybrid inverter.")
    # Optimisation retrieves the load history under the load sensor's name and a
    # saved model indexes it by its own `var_model`, so the two must be the same
    # series: a model trained on another sensor would raise a KeyError at
    # prediction. The sensor is the single source; a differing override is dropped.
    sensor = values.get("sensor_power_load_no_var_loads")
    if sensor:
        if values.get("var_model", sensor) != sensor:
            overrides.pop("var_model", None)
            warnings.append(
                "Ignored profile setting 'var_model': it must match "
                "'sensor_power_load_no_var_loads'."
            )
        values["var_model"] = sensor
    try:
        version = Version(backend_version or "0.17.9")
    except InvalidVersion:
        version = Version("0.17.9")
    for name in list(values):
        rule = SETTINGS[name]
        supported = version >= Version(rule.minimum_version)
        if name == "battery_charge_power_derating":
            supported = supported and send_derating
        elif name == "battery_soc_final_reward_factor":
            supported = supported and send_soc_final_reward
        if not supported:
            if name in overrides:
                warnings.append(
                    f"Ignored profile setting {name!r}: requires EMHASS {rule.minimum_version}+."
                )
            values.pop(name)
            overrides.pop(name, None)
    return EffectiveSettings(values, overrides, tuple(warnings))


def _battery_settings(battery: BatteryConfig) -> dict[str, Any]:
    return {
        "set_use_battery": battery.enabled,
        "battery_nominal_energy_capacity": battery.capacity_wh,
        "battery_charge_power_max": battery.charge_power_max_w,
        "battery_discharge_power_max": battery.discharge_power_max_w,
        "battery_minimum_state_of_charge": battery.soc_min,
        "battery_maximum_state_of_charge": battery.soc_max,
        "battery_target_state_of_charge": battery.soc_target,
        "battery_charge_efficiency": battery.charge_efficiency,
        "battery_discharge_efficiency": battery.discharge_efficiency,
        "set_nodischarge_to_grid": battery.no_discharge_to_grid,
        "set_nocharge_from_grid": battery.no_charge_from_grid,
        "set_battery_first_priority": battery.battery_first_priority,
        "set_battery_dynamic": battery.dynamic_enabled,
        "battery_dynamic_max": battery.dynamic_max,
        "battery_dynamic_min": battery.dynamic_min,
        "weight_battery_discharge": battery.weight_battery_discharge,
        "weight_battery_charge": battery.weight_battery_charge,
        "battery_soc_deficit_threshold": battery.soc_deficit_threshold,
        "battery_soc_deficit_cost": battery.soc_deficit_cost,
        "battery_soc_surplus_threshold": battery.soc_surplus_threshold,
        "battery_soc_surplus_cost": battery.soc_surplus_cost,
        "battery_stress_cost": battery.stress_cost,
        "battery_stress_segments": battery.stress_segments,
        "battery_charge_power_derating": [
            [round(soc, 4), round(fraction, 4)] for soc, fraction in battery.charge_power_derating
        ],
        "battery_soc_final_reward_factor": round(battery.soc_final_reward_factor, 4),
    }


def _hybrid_inverter_settings(hybrid: HybridInverterConfig) -> dict[str, Any]:
    return {
        "inverter_is_hybrid": hybrid.enabled,
        "inverter_ac_output_max": hybrid.ac_output_max_w,
        "inverter_ac_input_max": hybrid.ac_input_max_w,
        "inverter_efficiency_dc_ac": hybrid.efficiency_dc_ac,
        "inverter_efficiency_ac_dc": hybrid.efficiency_ac_dc,
    }

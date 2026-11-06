"""Falling back to real sensor data instead of a guaranteed-failing mlforecaster run.

Enabling "Machine-learning forecaster" (or pointing it at a different sensor,
most commonly right after "Create a house load sensor" builds a brand new
one) otherwise means the very next optimisation predicts against a stale or
entirely missing EMHASS model file and crashes outright -- see
test_config_sync.py's LOAD_SENSOR, which is the exact sensor name a real
mismatched-model crash was reproduced against. The coordinator now confirms a
fit actually succeeded for the *current* sensor before ever asking EMHASS to
use mlforecaster, auto-triggering that fit itself once enough recorder
history exists rather than only ever reacting to the "Train load forecaster"
button.

In the meantime it no longer falls back to EMHASS's own "typical" method,
which -- for a sensor mlforecaster has never been trained against -- reads
whatever a stale or mismatched reference pickle happens to hold, not this
household's own consumption (see docs/on_demand_loads.md-adjacent design
notes for the investigation). Instead it builds a series itself out of
whatever real readings already exist, which is what EMHASS's own "naive"
would have computed from the same recorder -- see
test_load_forecast_fallback.py for that piece in isolation.

The same fallback also covers a trained mlforecaster whose runs have started
failing: a request carrying no load series is the one shape that sends EMHASS
back to Home Assistant for history of its own, so when such a run fails the
forecast comes back in-house for a cooldown rather than being retried every
cycle. This file covers only the orchestration in
_resolve_load_forecast_method: whether and when it asks EMHASS for
mlforecaster versus deferring to _load_forecast_fallback.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient, EmhassConnectionError, EmhassError
from custom_components.emhass_companion.const import (
    CONF_LOAD,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    DOMAIN,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.models import Series
from custom_components.emhass_companion.profiles import async_load_profiles
from custom_components.emhass_companion.services import (
    SERVICE_TRAIN_LOAD_FORECASTER,
    async_register_services,
)

LOAD_SENSOR = "sensor.house_load_without_emhass_consumers"

_NOW = dt_util.utcnow()
_HORIZON_END = _NOW + timedelta(hours=24)
_FALLBACK_SENTINEL = ("naive", None)


async def _coordinator(hass: HomeAssistant, *, method: str = "mlforecaster") -> EmhassCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        options={
            CONF_LOAD: {
                CONF_PROFILE: "load/sensor",
                CONF_PROFILE_OPTIONS: {"entity": LOAD_SENSOR, "method": method},
            },
        },
    )
    entry.add_to_hass(hass)

    loads = DeferrableRegistry(hass, entry)
    loads.sync()

    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)
    coordinator.profiles = (await async_load_profiles(hass)).profiles
    return coordinator


async def _resolve(coordinator: EmhassCoordinator) -> tuple[str, Series | None]:
    return await coordinator._resolve_load_forecast_method(
        coordinator.load_forecast_settings(), _NOW, _HORIZON_END
    )


async def test_falls_back_when_never_trained(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    with (
        patch.object(coordinator, "_has_enough_history", AsyncMock(return_value=False)),
        patch.object(
            coordinator, "_load_forecast_fallback", AsyncMock(return_value=_FALLBACK_SENTINEL)
        ) as fallback,
    ):
        result = await _resolve(coordinator)

    assert result == _FALLBACK_SENTINEL
    fallback.assert_awaited_once_with(LOAD_SENSOR, _NOW, _HORIZON_END)
    coordinator.client.async_run_action.assert_not_awaited()


async def test_auto_trains_once_enough_history_exists(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    with patch.object(coordinator, "_has_enough_history", AsyncMock(return_value=True)):
        method, bootstrap = await _resolve(coordinator)

    assert method == "mlforecaster"
    assert bootstrap is None
    coordinator.client.async_run_action.assert_awaited_once()
    assert coordinator._ml_trained_settings["sensor_power_load_no_var_loads"] == LOAD_SENSOR


async def test_does_not_refit_once_trained_for_the_current_sensor(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    coordinator._ml_trained_settings = coordinator._effective_ml_settings(
        coordinator.load_forecast_settings()
    )

    with patch.object(coordinator, "_has_enough_history", AsyncMock()) as history_check:
        method, bootstrap = await _resolve(coordinator)

    assert method == "mlforecaster"
    assert bootstrap is None
    history_check.assert_not_awaited()
    coordinator.client.async_run_action.assert_not_awaited()


async def test_a_sensor_change_invalidates_the_previous_training(hass: HomeAssistant) -> None:
    """Switching sensors must not keep trusting a fit trained against the old one."""
    coordinator = await _coordinator(hass)
    coordinator._ml_trained_settings = coordinator._effective_ml_settings(
        coordinator.load_forecast_settings()
        | {
            "sensor_power_load_no_var_loads": "sensor.some_other_load",
            "var_model": "sensor.some_other_load",
        }
    )

    with (
        patch.object(coordinator, "_has_enough_history", AsyncMock(return_value=False)),
        patch.object(
            coordinator, "_load_forecast_fallback", AsyncMock(return_value=_FALLBACK_SENTINEL)
        ),
    ):
        result = await _resolve(coordinator)

    assert result == _FALLBACK_SENTINEL


async def test_falls_back_and_cools_down_when_the_fit_itself_fails(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    coordinator.client.async_run_action = AsyncMock(side_effect=EmhassConnectionError("boom"))

    with (
        patch.object(
            coordinator, "_has_enough_history", AsyncMock(return_value=True)
        ) as history_check,
        patch.object(
            coordinator, "_load_forecast_fallback", AsyncMock(return_value=_FALLBACK_SENTINEL)
        ) as fallback,
    ):
        first = await _resolve(coordinator)
        second = await _resolve(coordinator)

    assert first == second == _FALLBACK_SENTINEL
    # The cooldown must skip the second attempt entirely, not just fail again.
    history_check.assert_awaited_once()
    coordinator.client.async_run_action.assert_awaited_once()
    assert fallback.await_count == 2


async def test_no_sensor_configured_defers_to_fallback(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    coordinator.load_forecast_settings = lambda: {"load_forecast_method": "mlforecaster"}

    with patch.object(
        coordinator, "_load_forecast_fallback", AsyncMock(return_value=_FALLBACK_SENTINEL)
    ) as fallback:
        result = await _resolve(coordinator)

    assert result == _FALLBACK_SENTINEL
    fallback.assert_awaited_once_with(None, _NOW, _HORIZON_END)


@pytest.mark.parametrize("method", ["typical", "naive"])
async def test_no_fallback_logic_for_non_mlforecaster_methods(
    hass: HomeAssistant, method: str
) -> None:
    coordinator = await _coordinator(hass, method=method)
    with patch.object(coordinator, "_has_enough_history", AsyncMock()) as history_check:
        result, bootstrap = await _resolve(coordinator)

    assert result == method
    assert bootstrap is None
    history_check.assert_not_awaited()


async def test_only_a_run_that_sent_no_series_arms_the_cooldown(hass: HomeAssistant) -> None:
    """A run carrying its own load series had no EMHASS-side fetch to blame.

    Arming on it would quietly disable EMHASS's forecaster over an infeasible
    solve, an unreachable add-on, or anything else that has nothing to do with
    how the load forecast was obtained.
    """
    coordinator = await _coordinator(hass)

    coordinator._sent_load_list = True
    coordinator._note_failed_run()
    assert coordinator._no_list_run_failed_at is None

    coordinator._sent_load_list = False
    coordinator._note_failed_run()
    assert coordinator._no_list_run_failed_at is not None


async def test_a_failed_no_series_run_brings_the_forecast_back_in_house(
    hass: HomeAssistant,
) -> None:
    """Even for a sensor mlforecaster is confirmed trained against.

    Being trained is what makes this reachable at all: without the cooldown a
    trained model whose runs have started failing is retried every MPC cycle
    forever, each one failing the same way, with a perfectly good local series
    available the whole time.
    """
    coordinator = await _coordinator(hass)
    coordinator._ml_trained_settings = coordinator._effective_ml_settings(
        coordinator.load_forecast_settings()
    )
    coordinator._no_list_run_failed_at = _NOW

    with patch.object(
        coordinator, "_load_forecast_fallback", AsyncMock(return_value=_FALLBACK_SENTINEL)
    ) as fallback:
        result = await _resolve(coordinator)

    assert result == _FALLBACK_SENTINEL
    fallback.assert_awaited_once_with(LOAD_SENSOR, _NOW, _HORIZON_END)


async def test_the_cooldown_lapses_back_to_mlforecaster(hass: HomeAssistant) -> None:
    """Each retry costs one failed run, which is what stops this hiding a real
    misconfiguration for good."""
    coordinator = await _coordinator(hass)
    coordinator._ml_trained_settings = coordinator._effective_ml_settings(
        coordinator.load_forecast_settings()
    )
    coordinator._no_list_run_failed_at = _NOW - timedelta(hours=1, minutes=1)

    result, bootstrap = await _resolve(coordinator)

    assert result == "mlforecaster"
    assert bootstrap is None


async def test_the_cooldown_covers_methods_other_than_mlforecaster(hass: HomeAssistant) -> None:
    """ "typical" sends no load series either, and fails the same way for it."""
    coordinator = await _coordinator(hass, method="typical")
    coordinator._no_list_run_failed_at = _NOW

    with patch.object(
        coordinator, "_load_forecast_fallback", AsyncMock(return_value=_FALLBACK_SENTINEL)
    ) as fallback:
        result = await _resolve(coordinator)

    assert result == _FALLBACK_SENTINEL
    fallback.assert_awaited_once_with(LOAD_SENSOR, _NOW, _HORIZON_END)


async def test_ml_state_round_trips_through_storage(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    coordinator._ml_trained_settings = coordinator._effective_ml_settings(
        coordinator.load_forecast_settings()
    )
    await coordinator._async_save_ml_state()

    restored = EmhassCoordinator(
        hass, coordinator.config_entry, AsyncMock(spec=EmhassClient), coordinator.loads
    )
    await restored.async_load_ml_state()

    assert restored._ml_trained_settings == coordinator._ml_trained_settings


@pytest.mark.parametrize(
    ("name", "changed"),
    [
        ("optimization_time_step", 30),
        ("num_lags", 12),
        ("var_model", "sensor.different"),
        ("model_type", "other_model"),
        ("sklearn_model", "RandomForestRegressor"),
        ("mlforecaster_weather_features", ["temperature_2m"]),
        ("historic_days_to_retrieve", 30),
        ("load_negative", True),
        ("sensor_linear_interp", [LOAD_SENSOR]),
    ],
)
async def test_incompatible_saved_settings_require_refit(
    hass: HomeAssistant, name: str, changed: object
) -> None:
    coordinator = await _coordinator(hass)
    await coordinator.async_run_forecast_fit()
    coordinator._ml_trained_settings[name] = changed
    coordinator.client.async_run_action.reset_mock()
    with (
        patch.object(coordinator, "_has_enough_history", AsyncMock(return_value=False)),
        patch.object(
            coordinator, "_load_forecast_fallback", AsyncMock(return_value=_FALLBACK_SENTINEL)
        ),
    ):
        assert await _resolve(coordinator) == _FALLBACK_SENTINEL
    coordinator.client.async_run_action.assert_not_awaited()


async def test_legacy_sensor_only_state_is_not_compatible(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    await coordinator._ml_store.async_save({"trained_sensor": LOAD_SENSOR})
    await coordinator.async_load_ml_state()
    with patch.object(coordinator, "_has_enough_history", AsyncMock(return_value=True)):
        assert await _resolve(coordinator) == ("mlforecaster", None)
    coordinator.client.async_run_action.assert_awaited_once()
    assert coordinator._ml_trained_settings is not None


async def test_failed_refit_does_not_certify_existing_model(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    await coordinator.async_run_forecast_fit()
    coordinator.client.async_run_action.side_effect = EmhassConnectionError("offline")
    with pytest.raises(EmhassConnectionError):
        await coordinator.async_run_forecast_fit()
    assert coordinator._ml_trained_settings is None
    assert (await coordinator._ml_store.async_load())["trained_settings"] is None


@pytest.mark.parametrize("action", ["dayahead-optim", "naive-mpc-optim"])
@pytest.mark.parametrize("time_step", [15, 30])
async def test_fit_and_optimisation_agree_without_config_sync(
    hass: HomeAssistant, action: str, time_step: int
) -> None:
    coordinator = await _coordinator(hass)
    coordinator.config.time_step_minutes = time_step
    coordinator.backend_version = "0.18.8"
    coordinator.client.async_set_config_merged.side_effect = EmhassConnectionError("save failed")
    await coordinator.async_ensure_emhass_baseline()
    await coordinator.async_run_forecast_fit()
    fit = coordinator.client.async_run_action.await_args.args[1]
    with patch.object(coordinator, "_series", AsyncMock(return_value=Series.empty())):
        _inputs, built = await coordinator._build(action)
    assert {name: built.payload[name] for name in fit} == fit
    assert fit["optimization_time_step"] == time_step
    assert fit["num_lags"] == coordinator.config.dayahead_num_lags
    assert fit["var_model"] == LOAD_SENSOR
    assert fit["historic_days_to_retrieve"] == 9
    assert fit["model_type"] == "long_train_data"
    assert fit["mlforecaster_weather_features"] == []
    assert "prediction_horizon" not in fit
    assert "soc_init" not in fit


async def test_custom_model_settings_match_fit_and_prediction(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    settings = coordinator.load_forecast_settings() | {
        "num_lags": 72,
        "sensor_power_load_no_var_loads": "sensor.custom_training",
        "var_model": "sensor.custom_training",
        "model_type": "custom_load_model",
        "sklearn_model": "RandomForestRegressor",
        "split_date_delta": "72h",
        "perform_backtest": True,
        "historic_days_to_retrieve": 30,
        "mlforecaster_weather_features": ["temperature_2m"],
        "load_negative": True,
        "sensor_replace_zero": ["sensor.custom_training"],
        "sensor_linear_interp": ["sensor.custom_training"],
    }
    with (
        patch.object(coordinator, "_forecast_profile_settings", return_value=settings),
        patch.object(coordinator, "_series", AsyncMock(return_value=Series.empty())),
    ):
        await coordinator.async_run_forecast_fit()
        fit = coordinator.client.async_run_action.await_args.args[1]
        _inputs, built = await coordinator._build("naive-mpc-optim")
    for name, value in settings.items():
        assert fit[name] == built.payload[name] == value


async def test_manual_fit_reports_an_unusable_setting_as_a_user_error(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(hass)
    settings = coordinator.load_forecast_settings() | {"historic_days_to_retrieve": "unavailable"}
    with (
        patch.object(coordinator, "_forecast_profile_settings", return_value=settings),
        pytest.raises(HomeAssistantError, match="historic_days_to_retrieve"),
    ):
        await coordinator.async_run_forecast_fit()
    coordinator.client.async_run_action.assert_not_awaited()


async def test_auto_fit_probes_effective_training_sensor(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    settings = coordinator.load_forecast_settings() | {
        "sensor_power_load_no_var_loads": "sensor.custom_training"
    }
    with patch.object(
        coordinator, "_has_enough_history", AsyncMock(return_value=True)
    ) as history_check:
        assert await coordinator._resolve_load_forecast_method(settings, _NOW, _HORIZON_END) == (
            "mlforecaster",
            None,
        )
    history_check.assert_awaited_once_with("sensor.custom_training")


async def test_auto_fit_falls_back_if_config_changes_during_history_check(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(hass)

    async def change_config(_sensor: str) -> bool:
        coordinator.config.time_step_minutes = 30
        return True

    with (
        patch.object(coordinator, "_has_enough_history", side_effect=change_config),
        patch.object(
            coordinator, "_load_forecast_fallback", AsyncMock(return_value=_FALLBACK_SENTINEL)
        ),
    ):
        assert await _resolve(coordinator) == _FALLBACK_SENTINEL


async def test_timestep_change_retrains_with_current_settings(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    await coordinator.async_run_forecast_fit()
    coordinator.config.time_step_minutes = 30
    coordinator.client.async_run_action.reset_mock()
    with patch.object(coordinator, "_has_enough_history", AsyncMock(return_value=True)):
        assert await _resolve(coordinator) == ("mlforecaster", None)
    coordinator.client.async_run_action.assert_awaited_once()
    fit = coordinator.client.async_run_action.await_args.args[1]
    assert fit["optimization_time_step"] == 30
    assert fit["num_lags"] == coordinator.config.dayahead_num_lags
    assert coordinator._ml_trained_settings == fit


async def test_manual_fit_requires_load_sensor(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    with (
        patch.object(coordinator, "_forecast_profile_settings", return_value={}),
        pytest.raises(EmhassError, match="Select a load sensor"),
    ):
        await coordinator.async_run_forecast_fit()
    coordinator.client.async_run_action.assert_not_awaited()


async def test_prediction_falls_back_while_manual_fit_holds_lock(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    async with coordinator._ml_fit_lock:
        with (
            patch.object(coordinator, "_has_enough_history", AsyncMock()) as history_check,
            patch.object(
                coordinator, "_load_forecast_fallback", AsyncMock(return_value=_FALLBACK_SENTINEL)
            ),
        ):
            assert await _resolve(coordinator) == _FALLBACK_SENTINEL
    history_check.assert_not_awaited()
    coordinator.client.async_run_action.assert_not_awaited()


async def test_rounding_changes_and_restart_do_not_require_refitting(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    await coordinator.async_run_forecast_fit()
    coordinator.client.async_run_action.reset_mock()
    coordinator._row_zero_mode = "first"
    assert await _resolve(coordinator) == ("mlforecaster", None)
    with patch.object(coordinator, "_series", AsyncMock(return_value=Series.empty())):
        _inputs, built = await coordinator._build("naive-mpc-optim")
    assert built.payload["method_ts_round"] == "first"
    coordinator.client.async_run_action.assert_not_awaited()
    await coordinator.async_run_forecast_fit()
    restored = EmhassCoordinator(
        hass, coordinator.config_entry, AsyncMock(spec=EmhassClient), coordinator.loads
    )
    restored.profiles = coordinator.profiles
    await restored.async_load_ml_state()
    assert await _resolve(restored) == ("mlforecaster", None)
    restored.client.async_run_action.assert_not_awaited()


@pytest.mark.parametrize(
    "overrides",
    [{"open_meteo_cache_max_age": 15}, {"weather_forecast_method": "list"}],
)
async def test_weather_transport_settings_do_not_invalidate_model(
    hass: HomeAssistant, overrides: dict
) -> None:
    coordinator = await _coordinator(hass)
    await coordinator.async_run_forecast_fit()
    coordinator.client.async_run_action.reset_mock()
    settings = coordinator.load_forecast_settings() | overrides
    assert await coordinator._resolve_load_forecast_method(settings, _NOW, _HORIZON_END) == (
        "mlforecaster",
        None,
    )
    coordinator.client.async_run_action.assert_not_awaited()


async def test_failed_auto_refit_invalidates_saved_snapshot(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    await coordinator.async_run_forecast_fit()
    coordinator.config.time_step_minutes = 30
    coordinator.client.async_run_action.side_effect = EmhassConnectionError("offline")
    with (
        patch.object(coordinator, "_has_enough_history", AsyncMock(return_value=True)),
        patch.object(
            coordinator, "_load_forecast_fallback", AsyncMock(return_value=_FALLBACK_SENTINEL)
        ),
    ):
        assert await _resolve(coordinator) == _FALLBACK_SENTINEL
    assert coordinator._ml_trained_settings is None
    assert (await coordinator._ml_store.async_load())["trained_settings"] is None


# --- weekly refit ---------------------------------------------------------------


def _trained(coordinator: EmhassCoordinator, *, age: timedelta | None) -> None:
    coordinator._ml_trained_settings = coordinator._effective_ml_settings(
        coordinator.load_forecast_settings()
    )
    coordinator._ml_trained_at = None if age is None else dt_util.utcnow() - age


async def test_a_model_younger_than_a_week_is_not_refitted(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    _trained(coordinator, age=timedelta(days=6, hours=23))

    assert await _resolve(coordinator) == ("mlforecaster", None)
    coordinator.client.async_run_action.assert_not_awaited()


async def test_a_week_old_model_is_refitted_and_restamped(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    _trained(coordinator, age=timedelta(days=7, minutes=1))

    with patch.object(coordinator, "_has_enough_history", AsyncMock(return_value=True)):
        assert await _resolve(coordinator) == ("mlforecaster", None)

        coordinator.client.async_run_action.assert_awaited_once()
        assert dt_util.utcnow() - coordinator._ml_trained_at < timedelta(minutes=1)
        assert await _resolve(coordinator) == ("mlforecaster", None)
    coordinator.client.async_run_action.assert_awaited_once()


async def test_a_failed_weekly_refit_falls_back_then_retries_after_the_cooldown(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(hass)
    _trained(coordinator, age=timedelta(days=8))
    coordinator.client.async_run_action.side_effect = EmhassConnectionError("offline")

    with (
        patch.object(coordinator, "_has_enough_history", AsyncMock(return_value=True)),
        patch.object(
            coordinator, "_load_forecast_fallback", AsyncMock(return_value=_FALLBACK_SENTINEL)
        ),
    ):
        assert await _resolve(coordinator) == _FALLBACK_SENTINEL
        assert await _resolve(coordinator) == _FALLBACK_SENTINEL  # cooling down: no new attempt
    assert coordinator.client.async_run_action.await_count == 1

    coordinator.client.async_run_action.side_effect = None
    with patch.object(coordinator, "_has_enough_history", AsyncMock(return_value=True)):
        later = _NOW + timedelta(hours=1, minutes=1)
        assert await coordinator._resolve_load_forecast_method(
            coordinator.load_forecast_settings(), later, later + timedelta(hours=24)
        ) == ("mlforecaster", None)


async def test_a_weekly_refit_without_enough_history_keeps_the_working_model(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(hass)
    _trained(coordinator, age=timedelta(days=8))

    with patch.object(coordinator, "_has_enough_history", AsyncMock(return_value=False)):
        assert await _resolve(coordinator) == ("mlforecaster", None)
        assert await _resolve(coordinator) == ("mlforecaster", None)

    coordinator.client.async_run_action.assert_not_awaited()
    assert coordinator._ml_trained_settings is not None


async def test_state_without_a_fit_time_starts_its_clock_instead_of_refitting(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(hass)
    _trained(coordinator, age=None)

    assert await _resolve(coordinator) == ("mlforecaster", None)

    coordinator.client.async_run_action.assert_not_awaited()
    assert coordinator._ml_trained_at is not None
    assert (await coordinator._ml_store.async_load())["trained_at"] is not None


async def test_the_fit_time_round_trips_through_storage(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    await coordinator.async_run_forecast_fit()

    restored = EmhassCoordinator(
        hass, coordinator.config_entry, AsyncMock(spec=EmhassClient), coordinator.loads
    )
    await restored.async_load_ml_state()

    assert restored._ml_trained_at == coordinator._ml_trained_at


# --- the train_load_forecaster action ---------------------------------------------


class _RuntimeData:
    def __init__(self, coordinator: EmhassCoordinator) -> None:
        self.coordinator = coordinator


async def _registered(hass: HomeAssistant, *, method: str) -> EmhassCoordinator:
    coordinator = await _coordinator(hass, method=method)
    coordinator.config_entry.runtime_data = _RuntimeData(coordinator)
    coordinator.config_entry.mock_state(hass, ConfigEntryState.LOADED)
    async_register_services(hass)
    return coordinator


async def test_the_action_trains_the_forecaster(hass: HomeAssistant) -> None:
    coordinator = await _registered(hass, method="mlforecaster")

    await hass.services.async_call(DOMAIN, SERVICE_TRAIN_LOAD_FORECASTER, blocking=True)

    coordinator.client.async_run_action.assert_awaited_once()
    assert coordinator._ml_trained_settings is not None


@pytest.mark.parametrize("method", ["typical", "naive"])
async def test_the_action_refuses_other_forecast_methods(hass: HomeAssistant, method: str) -> None:
    coordinator = await _registered(hass, method=method)

    with pytest.raises(ServiceValidationError, match="machine-learning forecaster"):
        await hass.services.async_call(DOMAIN, SERVICE_TRAIN_LOAD_FORECASTER, blocking=True)

    coordinator.client.async_run_action.assert_not_awaited()


# -- the history fetch of a run that carries no load series ----------------------


async def test_a_series_less_run_names_the_live_pv_sensor(hass: HomeAssistant) -> None:
    """EMHASS fetches PV history too; its saved sensor name is a stock default."""
    import dataclasses

    coordinator = await _coordinator(hass)
    coordinator.config = dataclasses.replace(coordinator.config, pv_live_entity="sensor.pv_now")
    settings = {"sensor_power_load_no_var_loads": LOAD_SENSOR}

    series = await coordinator._ensure_history_fetchable(settings, _NOW, _HORIZON_END)

    assert series is None
    assert settings["sensor_power_photovoltaics"] == "sensor.pv_now"


async def test_a_profile_supplied_pv_sensor_is_kept(hass: HomeAssistant) -> None:
    import dataclasses

    coordinator = await _coordinator(hass)
    coordinator.config = dataclasses.replace(coordinator.config, pv_live_entity="sensor.pv_now")
    settings = {"sensor_power_photovoltaics": "sensor.profile_pv"}

    await coordinator._ensure_history_fetchable(settings, _NOW, _HORIZON_END)

    assert settings["sensor_power_photovoltaics"] == "sensor.profile_pv"


async def test_without_any_pv_sensor_the_run_sends_its_own_series(hass: HomeAssistant) -> None:
    """Nothing can satisfy EMHASS's fetch, so it is avoided rather than left to fail."""
    coordinator = await _coordinator(hass)
    settings = {
        "sensor_power_load_no_var_loads": LOAD_SENSOR,
        "load_forecast_method": "mlforecaster",
    }
    own = Series.empty()

    with patch.object(
        coordinator, "_load_forecast_fallback", AsyncMock(return_value=("list", own))
    ):
        series = await coordinator._ensure_history_fetchable(settings, _NOW, _HORIZON_END)

    assert series is own
    assert settings["load_forecast_method"] == "list"
    assert "sensor_power_photovoltaics" not in settings


async def test_a_load_series_does_not_replace_the_pv_sensor_with_native_pv(
    hass: HomeAssistant,
) -> None:
    """Without a PV series the weather forecast is EMHASS's, so it fetches anyway."""
    import dataclasses

    coordinator = await _coordinator(hass)
    settings = {"sensor_power_load_no_var_loads": LOAD_SENSOR}
    with patch.object(coordinator, "_load_forecast_fallback", AsyncMock()) as fallback:
        series = await coordinator._ensure_history_fetchable(
            settings, _NOW, _HORIZON_END, has_load_series=True
        )
    assert series is None
    fallback.assert_not_called()

    coordinator.config = dataclasses.replace(coordinator.config, pv_live_entity="sensor.pv_now")
    await coordinator._ensure_history_fetchable(settings, _NOW, _HORIZON_END, has_load_series=True)
    assert settings["sensor_power_photovoltaics"] == "sensor.pv_now"


async def test_with_pv_off_a_trained_model_keeps_serving_mpc(hass: HomeAssistant) -> None:
    """No PV means EMHASS fetches no PV history, so there is nothing to work around."""
    from custom_components.emhass_companion.const import ACTION_MPC, CONF_PV

    coordinator = await _coordinator(hass)
    hass.config_entries.async_update_entry(
        coordinator.config_entry,
        options={**coordinator.config_entry.options, CONF_PV: {CONF_PROFILE: "pv/none"}},
    )
    coordinator.config = type(coordinator.config).from_entry(hass, coordinator.config_entry)
    settings = coordinator.load_forecast_settings()
    coordinator._ml_trained_settings = coordinator._effective_ml_settings(
        coordinator._forecast_profile_settings()
    )
    coordinator._ml_trained_at = dt_util.utcnow()
    hass.states.async_set(LOAD_SENSOR, "500", {"unit_of_measurement": "W"})
    assert settings

    _inputs, built = await coordinator._build(ACTION_MPC)

    assert built.payload["load_forecast_method"] == "mlforecaster"
    assert "load_power_forecast" not in built.payload
    assert "sensor_power_photovoltaics" not in built.payload

"""The manual ``emhass_companion.export_settings`` action.

``/set-config`` rebuilds EMHASS's configuration from its defaults, so the
export must fetch the active configuration, overlay only Companion's allowlisted
static settings and post the whole result. It must never carry live inputs, never
report success after a failed save, and never put the fetched configuration
(which holds the backend's credentials) in a log line or an error.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, Unauthorized
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.emhass_companion.api import (
    EmhassApiError,
    EmhassClient,
    EmhassConnectionError,
)
from custom_components.emhass_companion.const import (
    CONF_LOAD,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    DOMAIN,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.effective_settings import SETTINGS
from custom_components.emhass_companion.profiles import async_load_profiles
from custom_components.emhass_companion.services import (
    SERVICE_EXPORT_SETTINGS,
    async_register_services,
    async_unregister_services,
)

LOAD_SENSOR = "sensor.house_load_without_emhass_consumers"
SECRET = "hunter2-do-not-log"

# What a long-lived EMHASS install holds that Companion has no business touching.
BACKEND_CONFIG = {
    "hass_url": "http://supervisor/core",
    "long_lived_token": SECRET,
    "solcast_api_key": SECRET,
    "costfun": "cost",
    "data_path": "/data",
    "lp_solver": "COIN_CMD",
    "custom_something_new": [1, 2, 3],
}


async def _coordinator(
    hass: HomeAssistant,
    *,
    method: str = "typical",
    version: str | None = "0.18.8",
    config: dict | None = None,
) -> EmhassCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        options={
            CONF_LOAD: {
                CONF_PROFILE: "load/sensor",
                CONF_PROFILE_OPTIONS: {"entity": LOAD_SENSOR, "method": method},
            }
        },
    )
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()

    client = AsyncMock(spec=EmhassClient)
    client.async_get_config.return_value = dict(BACKEND_CONFIG if config is None else config)
    coordinator = EmhassCoordinator(hass, entry, client, loads)
    coordinator.profiles = (await async_load_profiles(hass)).profiles
    coordinator.backend_version = version
    return coordinator


def _posted(coordinator: EmhassCoordinator) -> dict:
    coordinator.client.async_set_config.assert_awaited_once()
    (posted,), _kwargs = coordinator.client.async_set_config.await_args
    return posted


# --- what is merged -----------------------------------------------------------


async def test_unrelated_backend_settings_are_preserved(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)

    await coordinator.async_export_settings()

    posted = _posted(coordinator)
    for key, value in BACKEND_CONFIG.items():
        if key != "costfun":  # the one overlap; Companion owns it
            assert posted[key] == value


async def test_fetches_before_it_saves_and_never_uses_the_runtime_path(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(hass)
    order: list[str] = []
    coordinator.client.async_get_config.side_effect = lambda: (
        order.append("get") or dict(BACKEND_CONFIG)
    )
    coordinator.client.async_set_config.side_effect = lambda _config: order.append("set")

    await coordinator.async_export_settings()

    assert order == ["get", "set"]
    coordinator.client.async_run_action.assert_not_awaited()


async def test_static_settings_come_from_the_shared_resolver(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    battery = coordinator.config.battery
    grid = coordinator.config.grid

    count = await coordinator.async_export_settings()

    posted = _posted(coordinator)
    assert posted["optimization_time_step"] == coordinator.config.time_step_minutes
    assert posted["method_ts_round"] == coordinator._row_zero_mode
    assert posted["costfun"] == coordinator.cost_fun
    assert posted["battery_nominal_energy_capacity"] == battery.capacity_wh
    assert posted["battery_charge_power_max"] == battery.charge_power_max_w
    assert posted["maximum_power_from_grid"] == grid.import_max_w
    assert posted["maximum_power_to_grid"] == grid.export_max_w
    assert posted["num_lags"] == coordinator.config.dayahead_num_lags
    assert count == len(coordinator._persistent_baseline()[0])


async def test_live_and_runtime_only_settings_are_never_written(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)

    await coordinator.async_export_settings()

    exportable = {rule.persistent_name for rule in SETTINGS.values() if rule.persistent_name}
    written = set(_posted(coordinator)) - set(BACKEND_CONFIG)
    assert written <= exportable
    assert not written & {
        "soc_init",
        "soc_final",
        "load_power_forecast",
        "pv_power_forecast",
        "load_cost_forecast",
        "prod_price_forecast",
        "outdoor_temperature_forecast",
        "prediction_horizon",
        "alpha",
        "beta",
        "delta_forecast_daily",
        "def_load_config",
        "is_electric_load",
        "shared_thermal_tanks",
        "number_of_deferrable_loads",
        "nominal_power_of_deferrable_loads",
        "operating_hours_of_each_deferrable_load",
        "capacity_cost_per_kw",
        "current_period_peak",
        "capacity_charge_interval_timesteps",
        "def_current_state",
    }


async def test_a_temporary_grid_limit_does_not_reach_the_saved_baseline(
    hass: HomeAssistant,
) -> None:
    """The request narrows the limit by phase and window; the baseline must not."""
    coordinator = await _coordinator(hass)
    coordinator._grid_import_limit_w = lambda _config: 1234.0

    await coordinator.async_export_settings()

    assert _posted(coordinator)["maximum_power_from_grid"] == coordinator.config.grid.import_max_w


async def test_the_load_profile_supplies_its_forecast_and_model_settings(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(hass, method="mlforecaster")

    await coordinator.async_export_settings()

    posted = _posted(coordinator)
    assert posted["load_forecast_method"] == "mlforecaster"
    assert posted["sensor_power_load_no_var_loads"] == LOAD_SENSOR
    assert posted["var_model"] == LOAD_SENSOR
    assert posted["model_type"] == "long_train_data"
    assert posted["historic_days_to_retrieve"] >= 9


# --- clearing and versions ----------------------------------------------------


async def test_inherited_features_are_overwritten_with_explicit_clearing_values(
    hass: HomeAssistant,
) -> None:
    conflicting = {
        **BACKEND_CONFIG,
        "heat_topology": {"heat_pump": {"enabled": True}},
        "battery_charge_power_derating": [[0.9, 0.5]],
        "inverter_power_curve_dc_ac": [[0, 0], [1000, 900]],
        "number_of_batteries": 3,
        "battery_soc_final_reward_factor": 4.0,
        "tariff_schedule_time_zone": "Mars/Olympus",
        "compute_curtailment": True,
    }
    coordinator = await _coordinator(hass, config=conflicting)

    await coordinator.async_export_settings()

    posted = _posted(coordinator)
    assert posted["heat_topology"] == {}
    assert posted["battery_charge_power_derating"] == []
    assert posted["inverter_power_curve_dc_ac"] == []
    assert posted["number_of_batteries"] == 1
    assert posted["battery_soc_final_reward_factor"] == 0.0
    assert posted["tariff_schedule_time_zone"] == ""
    assert posted["compute_curtailment"] is False


@pytest.mark.parametrize("version", ["0.17.9", None, "not-a-version"])
async def test_newer_settings_are_left_alone_on_old_or_unknown_backends(
    hass: HomeAssistant, version: str | None
) -> None:
    coordinator = await _coordinator(
        hass,
        version=version,
        config={**BACKEND_CONFIG, "number_of_batteries": 2, "cop_solver": "iterative"},
    )

    await coordinator.async_export_settings()

    posted = _posted(coordinator)
    # Unsupported fields keep whatever the backend already had, rather than
    # being sent to a release that does not know them.
    assert posted["number_of_batteries"] == 2
    assert posted["cop_solver"] == "iterative"
    assert "battery_soc_final_reward_factor" not in posted
    assert "battery_charge_power_derating" not in posted
    assert posted["optimization_time_step"] == coordinator.config.time_step_minutes


async def test_a_current_backend_receives_the_versioned_fields(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass, version="0.18.8")

    await coordinator.async_export_settings()

    posted = _posted(coordinator)
    assert posted["number_of_batteries"] == 1
    assert posted["cop_solver"] == "static"


# --- failures -----------------------------------------------------------------


async def test_an_unreachable_backend_fails_without_writing(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    coordinator.client.async_get_config.side_effect = EmhassConnectionError("boom")

    with pytest.raises(HomeAssistantError, match="could not reach EMHASS"):
        await coordinator.async_export_settings()

    coordinator.client.async_set_config.assert_not_awaited()


@pytest.mark.parametrize("answer", [{}, None, ["not", "a", "mapping"]])
async def test_an_empty_active_configuration_is_never_overwritten(
    hass: HomeAssistant, answer: object
) -> None:
    """Posting only Companion's keys would reset everything else to defaults."""
    coordinator = await _coordinator(hass)
    coordinator.client.async_get_config.return_value = answer

    with pytest.raises(HomeAssistantError, match="no active configuration"):
        await coordinator.async_export_settings()

    coordinator.client.async_set_config.assert_not_awaited()


async def test_a_failed_save_is_reported_not_swallowed(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = await _coordinator(hass)
    coordinator.client.async_set_config.side_effect = EmhassApiError(
        f"POST /set-config returned 500: {SECRET}", status=500, body=SECRET
    )

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(HomeAssistantError, match=r"refused to save.*HTTP 500") as raised,
    ):
        await coordinator.async_export_settings()

    assert SECRET not in str(raised.value)
    assert "Wrote" not in caplog.text


async def test_an_unreadable_configuration_does_not_leak_the_response(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(hass)
    coordinator.client.async_get_config.side_effect = EmhassApiError(
        f"GET /get-config returned invalid JSON: {SECRET}"
    )

    with pytest.raises(HomeAssistantError, match="refused to read") as raised:
        await coordinator.async_export_settings()

    assert SECRET not in str(raised.value)
    coordinator.client.async_set_config.assert_not_awaited()


async def test_a_missing_profile_stops_the_export_before_contacting_emhass(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(hass)
    coordinator.profiles = {}

    with pytest.raises(HomeAssistantError, match="not written"):
        await coordinator.async_export_settings()

    coordinator.client.async_get_config.assert_not_awaited()
    coordinator.client.async_set_config.assert_not_awaited()


async def test_the_fetched_configuration_is_never_logged(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = await _coordinator(hass)

    with caplog.at_level(logging.DEBUG):
        await coordinator.async_export_settings()

    assert SECRET not in caplog.text
    assert "Wrote" in caplog.text


# --- entry points -------------------------------------------------------------


class _RuntimeData:
    def __init__(self, coordinator: EmhassCoordinator) -> None:
        self.coordinator = coordinator


async def _register_service(hass: HomeAssistant) -> EmhassCoordinator:
    from homeassistant.config_entries import ConfigEntryState

    coordinator = await _coordinator(hass)
    coordinator.config_entry.runtime_data = _RuntimeData(coordinator)
    coordinator.config_entry.mock_state(hass, ConfigEntryState.LOADED)
    async_register_services(hass)
    return coordinator


async def test_unloading_removes_every_registered_service(hass: HomeAssistant) -> None:
    async_register_services(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_EXPORT_SETTINGS)

    async_unregister_services(hass)

    assert not hass.services.async_services().get(DOMAIN)


async def test_the_service_is_admin_only(
    hass: HomeAssistant, hass_read_only_user: MockUser
) -> None:
    coordinator = await _register_service(hass)

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_EXPORT_SETTINGS,
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
        )

    coordinator.client.async_set_config.assert_not_awaited()


async def test_the_service_runs_for_an_admin_and_for_automations(
    hass: HomeAssistant, hass_admin_user: MockUser
) -> None:
    coordinator = await _register_service(hass)

    await hass.services.async_call(
        DOMAIN, SERVICE_EXPORT_SETTINGS, blocking=True, context=Context(user_id=hass_admin_user.id)
    )
    await hass.services.async_call(DOMAIN, SERVICE_EXPORT_SETTINGS, blocking=True)

    assert coordinator.client.async_set_config.await_count == 2

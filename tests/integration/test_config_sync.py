"""Setup initialises EMHASS's baseline without persisting Companion settings.

Every ``/action/*`` request fails until EMHASS has a saved configuration, and
``/set-config`` is not additive (see ``test_api.py``), so setup still makes sure
one exists. It must carry no Companion setting, though: timestep, load sensor,
``var_model``, lags and the rest travel with each request, and only the manual
export writes them to the saved configuration.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient, EmhassConnectionError
from custom_components.emhass_companion.const import (
    ACTION_FORECAST_FIT,
    CONF_LOAD,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    DOMAIN,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.profiles import async_load_profiles

LOAD_SENSOR = "sensor.house_load_without_emhass_consumers"


async def _coordinator(
    hass: HomeAssistant, *, method: str = "mlforecaster", time_step: int = 15
) -> EmhassCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        options={
            "optimization_time_step": time_step,
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


# --- detecting mlforecaster ---------------------------------------------------


async def test_uses_mlforecaster_when_the_sensor_profile_picks_it(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass, method="mlforecaster")
    assert coordinator.uses_mlforecaster is True


@pytest.mark.parametrize("method", ["typical", "naive"])
async def test_does_not_use_mlforecaster_for_other_methods(
    hass: HomeAssistant, method: str
) -> None:
    coordinator = await _coordinator(hass, method=method)
    assert coordinator.uses_mlforecaster is False


async def test_does_not_use_mlforecaster_with_no_load_profile_chosen(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"})
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)
    coordinator.profiles = (await async_load_profiles(hass)).profiles

    assert coordinator.uses_mlforecaster is False


# --- what setup does to the saved configuration ---------------------------------


@pytest.mark.parametrize("method", ["mlforecaster", "typical"])
async def test_bootstrap_carries_no_companion_setting(hass: HomeAssistant, method: str) -> None:
    coordinator = await _coordinator(hass, method=method, time_step=15)

    await coordinator.async_ensure_emhass_baseline()

    coordinator.client.async_set_config_merged.assert_awaited_once_with({})


async def test_bootstrap_saves_the_active_configuration_back_unchanged(
    hass: HomeAssistant,
) -> None:
    """A timestep, load sensor or ML choice made in EMHASS survives a reload."""
    coordinator = await _coordinator(hass, method="mlforecaster", time_step=15)
    saved = {
        "optimization_time_step": 30,
        "sensor_power_load_no_var_loads": "sensor.chosen_in_emhass",
        "var_model": "sensor.chosen_in_emhass",
        "num_lags": 48,
        "solcast_api_key": "unrelated-secret",
    }
    client = EmhassClient(AsyncMock(), "http://localhost:5000")
    client.async_get_config = AsyncMock(return_value=dict(saved))
    client.async_set_config = AsyncMock()
    coordinator.client = client

    await coordinator.async_ensure_emhass_baseline()

    client.async_set_config.assert_awaited_once_with(saved)


async def test_bootstrap_creates_a_baseline_when_emhass_has_none(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    client = EmhassClient(AsyncMock(), "http://localhost:5000")
    client.async_get_config = AsyncMock(return_value={})
    client.async_set_config = AsyncMock()
    coordinator.client = client

    await coordinator.async_ensure_emhass_baseline()

    client.async_set_config.assert_awaited_once_with({})


async def test_bootstrap_is_non_fatal_when_emhass_is_unreachable(hass: HomeAssistant) -> None:
    """EMHASS being briefly down at setup must not abort the whole integration."""
    coordinator = await _coordinator(hass)
    coordinator.client.async_set_config_merged = AsyncMock(
        side_effect=EmhassConnectionError("boom")
    )

    await coordinator.async_ensure_emhass_baseline()  # must not raise


# --- the fit button's own action ------------------------------------------------


async def test_run_forecast_fit_sends_the_sensor_and_method(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass, method="mlforecaster")

    await coordinator.async_run_forecast_fit()

    coordinator.client.async_run_action.assert_awaited_once()
    (action, params), _kwargs = coordinator.client.async_run_action.await_args
    assert action == ACTION_FORECAST_FIT
    assert params["load_forecast_method"] == "mlforecaster"
    assert params["sensor_power_load_no_var_loads"] == LOAD_SENSOR

"""Pushing EMHASS's own configuration into sync at setup.

EMHASS persists its configuration independently of what a run sends it, and
its own ``/set-config`` is not additive (see ``test_api.py``). Two things
specifically must never be left to drift, because each one was the actual
root cause of a real outage before this existed:

- ``optimization_time_step`` must match what every run sends, or a trained
  mlforecaster model crashes the next optimisation outright.
- ``sensor_power_load_no_var_loads`` and ``var_model`` are two independently
  persisted copies of the same load sensor; EMHASS falls back to whichever
  ``var_model`` is on disk regardless of a per-request override, so a fit
  trained against the wrong (stale) sensor silently "succeeds" while
  training on nothing useful.

This exercises the coordinator method that pushes both, unconditionally, on
every setup -- it must never try to detect drift, only assert the truth.
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


# --- what gets pushed ----------------------------------------------------------


async def test_sync_pushes_time_step_sensor_var_model_and_num_lags(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(hass, method="mlforecaster", time_step=15)

    await coordinator.async_sync_emhass_config()

    coordinator.client.async_set_config_merged.assert_awaited_once()
    (patch,), _kwargs = coordinator.client.async_set_config_merged.await_args
    assert patch["optimization_time_step"] == 15
    assert patch["sensor_power_load_no_var_loads"] == LOAD_SENSOR
    assert patch["var_model"] == LOAD_SENSOR
    # DEFAULT_HORIZON_HOURS=24 at 15-minute steps -> 96.
    assert patch["num_lags"] == 96


async def test_sync_only_pushes_time_step_when_not_mlforecaster(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(hass, method="typical", time_step=30)

    await coordinator.async_sync_emhass_config()

    (patch,), _kwargs = coordinator.client.async_set_config_merged.await_args
    assert patch == {"optimization_time_step": 30}


async def test_sync_is_non_fatal_when_emhass_is_unreachable(hass: HomeAssistant) -> None:
    """EMHASS being briefly down at setup must not abort the whole integration."""
    coordinator = await _coordinator(hass)
    coordinator.client.async_set_config_merged = AsyncMock(
        side_effect=EmhassConnectionError("boom")
    )

    await coordinator.async_sync_emhass_config()  # must not raise


# --- the fit button's own action ------------------------------------------------


async def test_run_forecast_fit_sends_the_sensor_and_method(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass, method="mlforecaster")

    await coordinator.async_run_forecast_fit()

    coordinator.client.async_run_action.assert_awaited_once()
    (action, params), _kwargs = coordinator.client.async_run_action.await_args
    assert action == ACTION_FORECAST_FIT
    assert params["load_forecast_method"] == "mlforecaster"
    assert params["sensor_power_load_no_var_loads"] == LOAD_SENSOR

"""Falling back to "typical" instead of a guaranteed-failing mlforecaster run.

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
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient, EmhassConnectionError
from custom_components.emhass_companion.const import (
    CONF_LOAD,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    DOMAIN,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.profiles import async_load_profiles

LOAD_SENSOR = "sensor.house_load_without_emhass_consumers"


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


async def test_falls_back_to_typical_when_never_trained(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    with patch.object(coordinator, "_has_enough_history", AsyncMock(return_value=False)):
        method = await coordinator._resolve_load_forecast_method(coordinator.load_forecast_settings())

    assert method == "typical"
    coordinator.client.async_run_action.assert_not_awaited()


async def test_auto_trains_once_enough_history_exists(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    with patch.object(coordinator, "_has_enough_history", AsyncMock(return_value=True)):
        method = await coordinator._resolve_load_forecast_method(coordinator.load_forecast_settings())

    assert method == "mlforecaster"
    coordinator.client.async_run_action.assert_awaited_once()
    assert coordinator._ml_trained_sensor == LOAD_SENSOR


async def test_does_not_refit_once_trained_for_the_current_sensor(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    coordinator._ml_trained_sensor = LOAD_SENSOR

    with patch.object(coordinator, "_has_enough_history", AsyncMock()) as history_check:
        method = await coordinator._resolve_load_forecast_method(coordinator.load_forecast_settings())

    assert method == "mlforecaster"
    history_check.assert_not_awaited()
    coordinator.client.async_run_action.assert_not_awaited()


async def test_a_sensor_change_invalidates_the_previous_training(hass: HomeAssistant) -> None:
    """Switching sensors must not keep trusting a fit trained against the old one."""
    coordinator = await _coordinator(hass)
    coordinator._ml_trained_sensor = "sensor.some_other_load"

    with patch.object(coordinator, "_has_enough_history", AsyncMock(return_value=False)):
        method = await coordinator._resolve_load_forecast_method(coordinator.load_forecast_settings())

    assert method == "typical"


async def test_falls_back_and_cools_down_when_the_fit_itself_fails(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    coordinator.client.async_run_action = AsyncMock(side_effect=EmhassConnectionError("boom"))

    with patch.object(coordinator, "_has_enough_history", AsyncMock(return_value=True)) as history_check:
        first = await coordinator._resolve_load_forecast_method(coordinator.load_forecast_settings())
        second = await coordinator._resolve_load_forecast_method(coordinator.load_forecast_settings())

    assert first == second == "typical"
    # The cooldown must skip the second attempt entirely, not just fail again.
    history_check.assert_awaited_once()
    coordinator.client.async_run_action.assert_awaited_once()


@pytest.mark.parametrize("method", ["typical", "naive"])
async def test_no_fallback_logic_for_non_mlforecaster_methods(
    hass: HomeAssistant, method: str
) -> None:
    coordinator = await _coordinator(hass, method=method)
    with patch.object(coordinator, "_has_enough_history", AsyncMock()) as history_check:
        result = await coordinator._resolve_load_forecast_method(coordinator.load_forecast_settings())

    assert result == method
    history_check.assert_not_awaited()


async def test_ml_state_round_trips_through_storage(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    coordinator._ml_trained_sensor = LOAD_SENSOR
    await coordinator._async_save_ml_state()

    restored = EmhassCoordinator(
        hass, coordinator.config_entry, AsyncMock(spec=EmhassClient), coordinator.loads
    )
    await restored.async_load_ml_state()

    assert restored._ml_trained_sensor == LOAD_SENSOR

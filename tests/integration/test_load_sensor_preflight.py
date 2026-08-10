"""Stopping an MPC run whose load sensor EMHASS cannot read.

EMHASS fetches ``sensor_power_load_no_var_loads`` itself for every load
method but ``list``, and an MPC run blends that reading into its own first
forecast step. When the entity is unavailable -- an inverter's Modbus link
dropping takes the whole house-load sensor with it -- that blend is NaN,
which EMHASS does not guard: it fails inside its forecaster with "cannot
convert float NaN to integer" and answers 500 with an HTML error page, so
the repair read ``EMHASS did not produce a plan: POST
/action/naive-mpc-optim returned 500: <!doctype html>`` and named neither
the sensor nor the reason.

The entity is the companion's to name, so the run is stopped before the
request goes out and the repair says which sensor to go and fix.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    ACTION_DAYAHEAD,
    ACTION_MPC,
    DOMAIN,
    EMHASS_CONF_LOAD_FORECAST_METHOD,
    EMHASS_CONF_SENSOR_LOAD,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.payload import PayloadResult

LOAD_SENSOR = "sensor.net_house_load"


def _coordinator(hass: HomeAssistant) -> EmhassCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"}, options={})
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    return EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)


def _payload(method: str = "naive", sensor: str | None = LOAD_SENSOR) -> dict:
    payload: dict = {EMHASS_CONF_LOAD_FORECAST_METHOD: method}
    if sensor is not None:
        payload[EMHASS_CONF_SENSOR_LOAD] = sensor
    return payload


# --- what counts as unreadable -------------------------------------------------


@pytest.mark.parametrize("state", ["unavailable", "unknown", ""])
async def test_an_unavailable_sensor_is_reported(hass: HomeAssistant, state: str) -> None:
    hass.states.async_set(LOAD_SENSOR, state)
    coordinator = _coordinator(hass)
    assert coordinator._unreadable_load_sensor(ACTION_MPC, _payload()) == LOAD_SENSOR


async def test_a_sensor_that_does_not_exist_at_all_is_reported(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    assert coordinator._unreadable_load_sensor(ACTION_MPC, _payload()) == LOAD_SENSOR


async def test_a_readable_sensor_passes(hass: HomeAssistant) -> None:
    hass.states.async_set(LOAD_SENSOR, "820")
    coordinator = _coordinator(hass)
    assert coordinator._unreadable_load_sensor(ACTION_MPC, _payload()) is None


# --- when the check does not apply ---------------------------------------------


async def test_the_list_method_needs_no_sensor(hass: HomeAssistant) -> None:
    """A profile supplying its own series leaves EMHASS nothing to fetch."""
    hass.states.async_set(LOAD_SENSOR, "unavailable")
    coordinator = _coordinator(hass)
    assert coordinator._unreadable_load_sensor(ACTION_MPC, _payload(method="list")) is None


async def test_a_dayahead_run_is_left_alone(hass: HomeAssistant) -> None:
    """The NaN blend is an MPC-only step; day-ahead never reaches it."""
    hass.states.async_set(LOAD_SENSOR, "unavailable")
    coordinator = _coordinator(hass)
    assert coordinator._unreadable_load_sensor(ACTION_DAYAHEAD, _payload()) is None


async def test_a_payload_naming_no_sensor_passes(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    assert coordinator._unreadable_load_sensor(ACTION_MPC, _payload(sensor=None)) is None


# --- the run itself ------------------------------------------------------------


async def test_the_run_stops_before_the_request_and_names_the_sensor(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(LOAD_SENSOR, "unavailable")
    coordinator = _coordinator(hass)

    async def _build(action: str):
        return None, PayloadResult(payload=_payload())

    coordinator._build = _build  # type: ignore[method-assign]

    with pytest.raises(UpdateFailed, match=LOAD_SENSOR):
        await coordinator.async_run(ACTION_MPC, notify=False)

    coordinator.client.async_optimize.assert_not_awaited()

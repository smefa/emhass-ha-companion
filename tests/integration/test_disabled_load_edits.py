"""Disabled loads accept batched settings without requesting optimisation."""

from datetime import time
from unittest.mock import AsyncMock, Mock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import DOMAIN
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.number import LOAD_NUMBERS, LoadNumber
from custom_components.emhass_companion.select import LoadRecurrenceSelect
from custom_components.emhass_companion.switch import LOAD_SWITCHES, LoadSwitch
from custom_components.emhass_companion.time import LOAD_TIMES, LoadTime


@pytest.mark.parametrize("kind", ["daily", "on_demand", "surplus", "room", "water_tank"])
async def test_disable_edit_enable_requests_only_transition_refreshes(hass, kind):
    thermal = kind in {"room", "water_tank"}
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[
            {
                "subentry_type": "thermal_load" if thermal else "deferrable_load",
                "title": "Load",
                "unique_id": "load",
                "data": {
                    "recurrence": "daily" if thermal else kind,
                    "thermal_model": kind if thermal else "room",
                    "nominal_power_w": 2000,
                },
            }
        ],
    )
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)
    coordinator.async_request_settings_refresh = AsyncMock()
    load = loads.all()[0]
    load.enabled = True
    notified = Mock()
    load.add_listener(notified)
    switches = {
        description.key: LoadSwitch(coordinator, load, description) for description in LOAD_SWITCHES
    }
    await switches["enabled"].async_turn_off()
    coordinator.async_request_settings_refresh.assert_awaited_once()
    coordinator.async_request_settings_refresh.reset_mock()
    await switches["enabled"].async_turn_off()

    number = LoadNumber(
        coordinator, load, next(d for d in LOAD_NUMBERS if d.key == "nominal_power")
    )
    number.hass = hass
    window = LoadTime(coordinator, load, next(d for d in LOAD_TIMES if d.key == "earliest_start"))
    await number.async_set_native_value(3000)
    await window.async_set_value(time(4))
    await switches["single_constant"].async_turn_off()
    if not thermal:
        recurrence = LoadRecurrenceSelect(coordinator, load)
        await recurrence.async_select_option("on_demand" if kind == "daily" else "daily")
    assert load.nominal_power_w == 3000
    assert load.earliest_start == time(4)
    assert load.single_constant is False
    assert notified.call_count >= 5
    coordinator.async_request_settings_refresh.assert_not_awaited()

    await switches["enabled"].async_turn_on()
    coordinator.async_request_settings_refresh.assert_awaited_once()
    # The next solve will read all final values from the same live runtime.
    assert load.enabled
    assert number.native_value == 3000
    assert window.native_value == time(4)

    coordinator.async_request_settings_refresh.reset_mock()
    await number.async_set_native_value(3500)
    await window.async_set_value(time(5))
    await switches["single_constant"].async_turn_on()
    if not thermal:
        await recurrence.async_select_option("daily")
    assert coordinator.async_request_settings_refresh.await_count == (3 if thermal else 4)

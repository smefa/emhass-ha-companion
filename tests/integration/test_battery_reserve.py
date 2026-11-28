"""Integration tests for battery reserve actions, sensor, and persistence."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    BATTERY_RESERVE_KEY,
    CONF_BATTERY,
    CONF_SOC_MAX,
    CONF_SOC_MIN,
    CONF_USE_BATTERY,
    DOMAIN,
)

BATTERY_OPTIONS: dict[str, Any] = {
    CONF_BATTERY: {
        CONF_USE_BATTERY: True,
        CONF_SOC_MIN: 0.1,
        CONF_SOC_MAX: 0.9,
    }
}


async def _setup(
    hass: HomeAssistant,
    *,
    options: dict[str, Any] | None = None,
    version: str = "0.19.0",
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        options=BATTERY_OPTIONS if options is None else options,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.emhass_companion.EmhassClient") as client_cls:
        client_cls.return_value = AsyncMock(spec=EmhassClient)
        client_cls.return_value.async_get_version = AsyncMock(return_value=version)
        client_cls.return_value.base_url = "http://localhost:5000"
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _sensor_id(hass: HomeAssistant, entry: MockConfigEntry) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{BATTERY_RESERVE_KEY}"
    )


async def test_sensor_absent_without_battery(hass: HomeAssistant) -> None:
    entry = await _setup(hass, options={})
    assert _sensor_id(hass, entry) is None
    end = (dt_util.utcnow() + timedelta(hours=4)).isoformat()
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "set_battery_reserve",
            {"target_soc": 80, "end_time": end},
            blocking=True,
        )


async def test_set_and_clear_battery_reserve(hass: HomeAssistant) -> None:
    entry = await _setup(hass, version="0.19.0")
    entity_id = _sensor_id(hass, entry)
    assert entity_id is not None

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "off"
    assert state.attributes["supported"] is True
    assert state.attributes["soc_min"] == 10.0
    assert state.attributes["soc_max"] == 90.0

    now = dt_util.utcnow()
    # Scheduled future reserve
    await hass.services.async_call(
        DOMAIN,
        "set_battery_reserve",
        {
            "target_soc": 80,
            "start_time": (now + timedelta(hours=2)).isoformat(),
            "end_time": (now + timedelta(hours=10)).isoformat(),
            "reason": "Storm warning",
        },
        blocking=True,
    )
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "scheduled"
    assert state.attributes["target_soc"] == 80.0
    assert state.attributes["reason"] == "Storm warning"

    # Active reserve (start_time omitted -> now)
    await hass.services.async_call(
        DOMAIN,
        "set_battery_reserve",
        {
            "target_soc": 75,
            "end_time": (now + timedelta(hours=4)).isoformat(),
        },
        blocking=True,
    )
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "active"
    assert state.attributes["target_soc"] == 75.0
    assert state.attributes["reason"] is None

    # Clear
    await hass.services.async_call(DOMAIN, "clear_battery_reserve", {}, blocking=True)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "off"
    assert state.attributes["target_soc"] is None


async def test_validation_rejects_out_of_range_and_past_end(hass: HomeAssistant) -> None:
    await _setup(hass, version="0.18.11")
    now = dt_util.utcnow()

    # Strictly above soc_min (10%): equal to soc_min must be rejected.
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "set_battery_reserve",
            {"target_soc": 10, "end_time": (now + timedelta(hours=4)).isoformat()},
            blocking=True,
        )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "set_battery_reserve",
            {"target_soc": 95, "end_time": (now + timedelta(hours=4)).isoformat()},
            blocking=True,
        )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "set_battery_reserve",
            {"target_soc": 80, "end_time": (now - timedelta(minutes=5)).isoformat()},
            blocking=True,
        )


async def test_set_and_clear_trigger_replan_and_lead_in(hass: HomeAssistant) -> None:
    options = {
        CONF_BATTERY: {
            CONF_USE_BATTERY: True,
            CONF_SOC_MIN: 0.1,
            CONF_SOC_MAX: 0.9,
            "capacity_wh": 10000,
            "charge_power_max_w": 5000,
        },
        "soc_entity": "sensor.battery_soc",
    }
    hass.states.async_set("sensor.battery_soc", "30")
    entry = await _setup(hass, options=options, version="0.19.0")
    coordinator = entry.runtime_data.coordinator
    entity_id = _sensor_id(hass, entry)
    assert entity_id is not None

    now = dt_util.utcnow()
    with patch.object(
        coordinator, "async_request_settings_refresh", new=AsyncMock()
    ) as mock_refresh:
        # Omitted start_time with SoC 30% < 80% shifts start forward for charge lead-in.
        await hass.services.async_call(
            DOMAIN,
            "set_battery_reserve",
            {"target_soc": 80, "end_time": (now + timedelta(hours=6)).isoformat()},
            blocking=True,
        )
        assert mock_refresh.await_count == 1
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "scheduled"
        assert state.attributes["holding_below_target"] is False
        assert state.attributes["current_soc"] == 30.0

        # Clearing also requests a replan.
        await hass.services.async_call(DOMAIN, "clear_battery_reserve", {}, blocking=True)
        assert mock_refresh.await_count == 2
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "off"

    # When already at/above target, omitting start_time activates immediately.
    hass.states.async_set("sensor.battery_soc", "85")
    with patch.object(
        coordinator, "async_request_settings_refresh", new=AsyncMock()
    ) as mock_refresh:
        await hass.services.async_call(
            DOMAIN,
            "set_battery_reserve",
            {"target_soc": 80, "end_time": (now + timedelta(hours=4)).isoformat()},
            blocking=True,
        )
        assert mock_refresh.await_count == 1
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "active"
        assert state.attributes["holding_below_target"] is False

        # If SoC subsequently drops below target while active, sensor reports it.
        hass.states.async_set("sensor.battery_soc", "50")
        coordinator.async_update_listeners()
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.attributes["holding_below_target"] is True
        assert state.attributes["current_soc"] == 50.0


async def test_reserve_persists_across_reload(hass: HomeAssistant) -> None:
    entry = await _setup(hass, version="0.19.0")
    now = dt_util.utcnow()
    await hass.services.async_call(
        DOMAIN,
        "set_battery_reserve",
        {
            "target_soc": 80,
            "start_time": (now + timedelta(hours=1)).isoformat(),
            "end_time": (now + timedelta(hours=6)).isoformat(),
            "reason": "Outage prep",
        },
        blocking=True,
    )

    with patch("custom_components.emhass_companion.EmhassClient") as client_cls:
        client_cls.return_value = AsyncMock(spec=EmhassClient)
        client_cls.return_value.async_get_version = AsyncMock(return_value="0.19.0")
        client_cls.return_value.base_url = "http://localhost:5000"
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    entity_id = _sensor_id(hass, entry)
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "scheduled"
    assert state.attributes["target_soc"] == 80.0
    assert state.attributes["reason"] == "Outage prep"

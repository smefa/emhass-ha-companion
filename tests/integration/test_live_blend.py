"""Tests for the coordinator's live PV/load readers, driven directly against it.

These feed ``payload.PayloadInputs.pv_live_w``/``load_live_w`` (see
``test_payload.py`` for the blend itself); what matters here is only where the
live value comes from and when it is unavailable.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    CONF_LOAD,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    CONF_PV_ENTITY,
    DOMAIN,
    PROFILE_KEY_LOAD_SENSOR,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry


def _coordinator(hass: HomeAssistant, options: dict) -> EmhassCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"}, options=options)
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    return EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)


# --- live PV -------------------------------------------------------------------


async def test_pv_live_is_none_when_no_entity_is_configured(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass, options={})
    assert coordinator._read_pv_live() is None


async def test_pv_live_reads_the_configured_entity(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.pv_now", "4321")
    coordinator = _coordinator(hass, options={CONF_PV_ENTITY: "sensor.pv_now"})
    assert coordinator._read_pv_live() == 4321.0


async def test_pv_live_is_none_when_the_entity_is_unavailable(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.pv_now", "unavailable")
    coordinator = _coordinator(hass, options={CONF_PV_ENTITY: "sensor.pv_now"})
    assert coordinator._read_pv_live() is None


async def test_pv_live_is_none_when_the_entity_is_non_numeric(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.pv_now", "not a number")
    coordinator = _coordinator(hass, options={CONF_PV_ENTITY: "sensor.pv_now"})
    assert coordinator._read_pv_live() is None


# --- live load -------------------------------------------------------------------


async def test_load_live_is_none_without_the_house_load_sensor_profile(
    hass: HomeAssistant,
) -> None:
    """Every other load profile has no live sensor of its own to read."""
    hass.states.async_set("sensor.load_now", "800")
    coordinator = _coordinator(
        hass,
        options={
            CONF_LOAD: {
                CONF_PROFILE: "load/forecast_entity",
                CONF_PROFILE_OPTIONS: {"entity": "sensor.load_now"},
            }
        },
    )
    assert coordinator._read_load_live() is None


async def test_load_live_reads_the_house_load_sensor_profiles_entity(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("sensor.load_now", "800")
    coordinator = _coordinator(
        hass,
        options={
            CONF_LOAD: {
                CONF_PROFILE: PROFILE_KEY_LOAD_SENSOR,
                CONF_PROFILE_OPTIONS: {"entity": "sensor.load_now"},
            }
        },
    )
    assert coordinator._read_load_live() == 800.0


async def test_load_live_is_none_when_the_entity_is_unavailable(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.load_now", "unavailable")
    coordinator = _coordinator(
        hass,
        options={
            CONF_LOAD: {
                CONF_PROFILE: PROFILE_KEY_LOAD_SENSOR,
                CONF_PROFILE_OPTIONS: {"entity": "sensor.load_now"},
            }
        },
    )
    assert coordinator._read_load_live() is None

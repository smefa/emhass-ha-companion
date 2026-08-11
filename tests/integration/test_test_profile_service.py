"""Guard rails on the ``test_profile`` diagnostic action.

Home Assistant does not restrict action calls to admins, and this one both
renders a profile's ``emhass:`` block -- where a user profile's forecast API
key lives -- and feeds caller-supplied options straight into that profile's own
Jinja templates. Neither is something a non-admin household member should be
able to reach through a dashboard.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import DOMAIN
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.profiles import async_load_profiles
from custom_components.emhass_companion.services import (
    SERVICE_TEST_PROFILE,
    async_register_services,
)


class _RuntimeData:
    def __init__(self, coordinator: EmhassCoordinator) -> None:
        self.coordinator = coordinator


async def _setup(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"})
    entry.add_to_hass(hass)

    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)
    coordinator.profiles = (await async_load_profiles(hass)).profiles
    entry.runtime_data = _RuntimeData(coordinator)
    entry.mock_state(hass, ConfigEntryState.LOADED)

    async_register_services(hass)


async def _call(hass: HomeAssistant, data: dict, context: Context | None = None):
    return await hass.services.async_call(
        DOMAIN,
        SERVICE_TEST_PROFILE,
        data,
        blocking=True,
        return_response=True,
        context=context,
    )


# --- who may call it ----------------------------------------------------------


async def test_a_non_admin_user_is_turned_away(
    hass: HomeAssistant, hass_read_only_user: MockUser
) -> None:
    await _setup(hass)

    with pytest.raises(Unauthorized):
        await _call(hass, {"profile": "pv/emhass_native"}, Context(user_id=hass_read_only_user.id))


async def test_an_admin_user_may_call_it(hass: HomeAssistant, hass_admin_user: MockUser) -> None:
    await _setup(hass)

    result = await _call(hass, {"profile": "pv/emhass_native"}, Context(user_id=hass_admin_user.id))
    assert result["profile"] == "pv/emhass_native"


async def test_an_automation_keeps_working(hass: HomeAssistant) -> None:
    """No user id means Home Assistant itself triggered it -- a script, an
    automation, another integration. Gating those would break every existing
    setup for no security gain."""
    await _setup(hass)

    result = await _call(hass, {"profile": "pv/emhass_native"})
    assert result["profile"] == "pv/emhass_native"


# --- what it hands back -------------------------------------------------------


async def test_the_response_does_not_carry_the_config_directory_layout(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)

    result = await _call(hass, {"profile": "pv/emhass_native"})

    assert result["file"] == "emhass_native.yaml"
    assert "path" not in result


# --- what may be passed in ----------------------------------------------------


async def test_options_are_held_to_the_profile_own_schema(hass: HomeAssistant) -> None:
    """The Solcast profile renders ``options.entities`` into a template that
    reads those entities' attributes; an unchecked mapping makes that a way to
    read any entity by name."""
    await _setup(hass)

    with pytest.raises(ServiceValidationError, match="Invalid options"):
        await _call(
            hass,
            {
                "profile": "pv/solcast",
                "options": {"smuggled": ["sensor.something_private"]},
            },
        )


async def test_valid_options_still_go_through(hass: HomeAssistant) -> None:
    await _setup(hass)

    result = await _call(
        hass,
        {
            "profile": "pv/emhass_native",
            "options": {
                "peak_power_w": 9500,
                "inverter_power_w": 8000,
                "surface_tilt": 30,
                "surface_azimuth": 180,
            },
        },
    )

    assert result["emhass_settings"]["pv_module_model"] == [9500]

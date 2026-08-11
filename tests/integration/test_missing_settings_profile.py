"""A configured profile that no longer resolves must never fail silently.

``_series`` has always raised a named ProfileError for this; ``_settings`` used
to return ``{}``. So a deleted or renamed *settings-only* profile
(``load/emhass_native``, ``pv/none``, ``temperature/emhass_native``) contributed
nothing, raised nothing, and the run went out with EMHASS falling back to its
own persisted config -- exactly what ``async_sync_emhass_config`` exists to
prevent, and invisible from the outside.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    CONF_LOAD,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    CONF_PV,
    DOMAIN,
    PROFILE_KIND_PV,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.profiles import ProfileError, async_load_profiles

LOAD_SENSOR = "sensor.house_load_without_emhass_consumers"


async def _coordinator(hass: HomeAssistant, **options) -> EmhassCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        options=options,
    )
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)
    coordinator.profiles = (await async_load_profiles(hass)).profiles
    return coordinator


async def test_a_settings_only_profile_that_vanished_raises_by_name(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(
        hass, **{CONF_PV: {CONF_PROFILE: "pv/deleted_by_the_user", CONF_PROFILE_OPTIONS: {}}}
    )

    with pytest.raises(ProfileError, match="pv/deleted_by_the_user"):
        coordinator._settings(coordinator.config.pv, PROFILE_KIND_PV)


async def test_no_profile_chosen_is_still_simply_no_settings(hass: HomeAssistant) -> None:
    """An unset selection is a legitimate answer, not a missing profile."""
    coordinator = await _coordinator(hass)

    assert coordinator._settings(coordinator.config.pv, PROFILE_KIND_PV) == {}


async def test_setup_time_callers_degrade_instead_of_taking_setup_down(
    hass: HomeAssistant,
) -> None:
    """load_forecast_settings runs at setup, where raising costs every entity.

    The button platform asks ``uses_mlforecaster`` and the config sync asks for
    the load settings before any run exists; a hard failure there would leave
    the user with no integration at all rather than a failed optimisation they
    can read. The run path still raises -- see the test above.
    """
    coordinator = await _coordinator(
        hass, **{CONF_LOAD: {CONF_PROFILE: "load/gone", CONF_PROFILE_OPTIONS: {}}}
    )

    assert coordinator.load_forecast_settings() == {}
    assert coordinator.uses_mlforecaster is False
    await coordinator.async_sync_emhass_config()


async def test_a_resolvable_settings_profile_is_unaffected(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(
        hass,
        **{
            CONF_LOAD: {
                CONF_PROFILE: "load/sensor",
                CONF_PROFILE_OPTIONS: {"entity": LOAD_SENSOR, "method": "mlforecaster"},
            }
        },
    )

    assert coordinator.uses_mlforecaster is True

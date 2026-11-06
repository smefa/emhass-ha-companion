"""Which buttons the hub offers.

"Rebuild day-ahead plan" and "Train load forecaster" are actions only -- the
plan is rebuilt on its own schedule and the forecaster trains itself -- so the
configuration card carries just "Recalculate now" (plus a per-load "Run now").
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.button import LoadRunNowButton, async_setup_entry
from custom_components.emhass_companion.const import (
    CONF_LOAD,
    CONF_NOMINAL_POWER,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    DOMAIN,
    RECURRENCE_DAILY,
    RECURRENCE_ON_DEMAND,
    RECURRENCE_SURPLUS,
    SUBENTRY_TYPE_DEFERRABLE,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.profiles import async_load_profiles


class _RuntimeData:
    """Stand-in for EmhassRuntimeData; button.py only reads .coordinator and .loads."""

    def __init__(self, coordinator: EmhassCoordinator, loads: DeferrableRegistry) -> None:
        self.coordinator = coordinator
        self.loads = loads


async def _setup(hass: HomeAssistant, *, options: dict) -> set[str]:
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"}, options=options)
    entry.add_to_hass(hass)

    loads = DeferrableRegistry(hass, entry)
    loads.sync()

    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)
    coordinator.profiles = (await async_load_profiles(hass)).profiles
    entry.runtime_data = _RuntimeData(coordinator, loads)

    added: list = []
    await async_setup_entry(hass, entry, added.extend)
    return {button.entity_description.key for button in added}


@pytest.mark.parametrize("method", ["mlforecaster", "typical", "naive"])
async def test_training_and_day_ahead_are_actions_not_buttons(
    hass: HomeAssistant, method: str
) -> None:
    keys = await _setup(
        hass,
        options={
            CONF_LOAD: {
                CONF_PROFILE: "load/sensor",
                CONF_PROFILE_OPTIONS: {"entity": "sensor.house_load", "method": method},
            }
        },
    )
    assert "run_forecast_fit" not in keys
    assert "run_dayahead" not in keys


async def test_recalculate_is_the_only_hub_button(hass: HomeAssistant) -> None:
    keys = await _setup(hass, options={})
    assert keys == {"run_mpc"}


async def _run_now_button(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_DEFERRABLE,
                "title": "Car",
                "unique_id": "Car",
                "data": {CONF_NOMINAL_POWER: 6000},
            }
        ],
    )
    entry.add_to_hass(hass)

    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)
    coordinator.profiles = (await async_load_profiles(hass)).profiles
    coordinator.last_update_success = True
    entry.runtime_data = _RuntimeData(coordinator, loads)

    added: list = []
    # Per-load entities are added with config_subentry_id, which the bare
    # list.extend the tests above use as a stand-in does not accept.
    await async_setup_entry(hass, entry, lambda entities, **_: added.extend(entities))
    button = next(b for b in added if isinstance(b, LoadRunNowButton))
    return button, loads.all()[0]


async def test_run_now_is_unavailable_on_a_surplus_load(hass: HomeAssistant) -> None:
    """force_run means "regardless of the plan", which on a load whose premise
    is spare solar means charging off the grid at midnight -- and with a
    structurally zero operating_hours it could never disarm itself either."""
    button, load = await _run_now_button(hass)

    load.recurrence = RECURRENCE_DAILY
    assert button.available is True

    load.recurrence = RECURRENCE_SURPLUS
    assert button.available is False


async def test_run_now_follows_a_live_recurrence_change(hass: HomeAssistant) -> None:
    """Unavailable rather than never created, because the recurrence select can
    move at any time -- the button has to go then, not on the next restart."""
    button, load = await _run_now_button(hass)

    load.recurrence = RECURRENCE_SURPLUS
    assert button.available is False

    load.recurrence = RECURRENCE_ON_DEMAND
    assert button.available is True


async def test_retired_buttons_are_removed_from_the_registry(hass: HomeAssistant) -> None:
    """Hosts that created the old buttons would otherwise keep dead entities."""
    from homeassistant.helpers import entity_registry as er

    from custom_components.emhass_companion import _async_remove_retired_buttons

    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for key in ("run_dayahead", "run_forecast_fit", "export_settings", "run_mpc"):
        registry.async_get_or_create(
            "button", DOMAIN, f"{entry.entry_id}_{key}", config_entry=entry
        )
    # Same key under another entry, and a different domain: both left alone.
    other = registry.async_get_or_create("button", DOMAIN, "other_entry_run_dayahead")
    sensor = registry.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_run_dayahead")

    _async_remove_retired_buttons(hass, entry)

    remaining = {e.unique_id for e in registry.entities.values()}
    assert remaining == {
        f"{entry.entry_id}_run_mpc",
        other.unique_id,
        sensor.unique_id,
    }

    _async_remove_retired_buttons(hass, entry)  # idempotent

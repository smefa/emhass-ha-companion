"""Each planned sensor names the sensor that measures the same thing.

The cards draw measured against planned -- the battery's real power over the
plan's, the real level over the planned curve -- so every card needed to be
told which of the house's own sensors those are. They were asking separately:
the plan and overview cards under ``battery_entity``, the status card under
``power_entity`` for the same sensor and ``soc_entity`` for a level the
integration already had in its own options, with the sign convention declared
once per card on the two that asked for it at all.

A card cannot read config entry options -- it discovers this integration's
entities and reads their attributes, and nothing else -- so the pointer rides
on the planned sensor. One answer in the Companion's settings, every card.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_POWER_INVERT,
    CONF_SOC_ENTITY,
    DOMAIN,
)


async def _setup(hass: HomeAssistant, **extra: Any) -> MockConfigEntry:
    options: dict[str, Any] = {
        "battery": {"use_battery": True, "capacity_wh": 10000},
        **extra,
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"}, options=options)
    entry.add_to_hass(hass)

    with patch("custom_components.emhass_companion.EmhassClient") as client_cls:
        client_cls.return_value = AsyncMock(spec=EmhassClient)
        client_cls.return_value.async_get_version = AsyncMock(return_value="0.17.9")
        client_cls.return_value.base_url = "http://localhost:5000"
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _attributes(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> dict[str, Any]:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return dict(state.attributes)


async def test_planned_battery_power_names_the_measured_sensor(hass: HomeAssistant) -> None:
    entry = await _setup(
        hass,
        **{
            CONF_BATTERY_POWER_ENTITY: "sensor.house_battery_power",
            CONF_BATTERY_POWER_INVERT: True,
        },
    )

    attributes = _attributes(hass, entry, "battery_power")

    assert attributes["measured_entity"] == "sensor.house_battery_power"
    assert attributes["measured_invert"] is True


async def test_planned_level_names_the_soc_sensor_already_configured(
    hass: HomeAssistant,
) -> None:
    """No new setting for this one: the plan starts from it, so it was there."""
    entry = await _setup(hass, **{CONF_SOC_ENTITY: "sensor.house_battery_level"})

    assert (
        _attributes(hass, entry, "battery_soc")["measured_entity"] == "sensor.house_battery_level"
    )


@pytest.mark.parametrize("key", ["battery_power", "battery_soc"])
async def test_the_pointer_is_published_even_when_unset(hass: HomeAssistant, key: str) -> None:
    """A published `None` is what tells a card to fall back rather than wait."""
    entry = await _setup(hass)

    attributes = _attributes(hass, entry, key)

    assert "measured_entity" in attributes
    assert attributes["measured_entity"] is None


async def test_the_convention_defaults_to_the_plans_own(hass: HomeAssistant) -> None:
    """Unset must mean "positive is discharge", not "unknown" -- see the card.

    The status card states charge direction only when it believes it knows the
    convention, so a default of anything but False would have it announcing a
    direction it was never told.
    """
    entry = await _setup(hass, **{CONF_BATTERY_POWER_ENTITY: "sensor.house_battery_power"})

    assert _attributes(hass, entry, "battery_power")["measured_invert"] is False

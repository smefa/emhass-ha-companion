"""The control-authority switch."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import EmhassCoordinator
from .entity import EmhassEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: EmhassCoordinator = entry.runtime_data.coordinator
    async_add_entities([EmhassControlSwitch(coordinator)])


class EmhassControlSwitch(EmhassEntity, SwitchEntity, RestoreEntity):
    """Master gate on whether the integration may act on the plan.

    Ships **off**. Anyone installing this already has working automations, and
    handing control to a newly configured optimiser before they have seen it
    make sensible decisions is how people end up charging a battery at the
    day's peak price. While off, the executor still computes and records what
    it *would* have done, so its judgement can be compared against the existing
    setup before anything is handed over.
    """

    _attr_translation_key = "control_enabled"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: EmhassCoordinator) -> None:
        super().__init__(coordinator, "control_enabled")
        self._is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # A switch's value *is* its state, so plain RestoreEntity is correct
        # here; number and select entities need their typed Restore* variants
        # because those restore a formatted string rather than a native value.
        if (last_state := await self.async_get_last_state()) is not None:
            self._is_on = last_state.state == STATE_ON

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._is_on = False
        self.async_write_ha_state()

"""Manual override of the system's operating mode."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import LOAD_MODES, MODE_AUTO, SYSTEM_MODES
from .coordinator import EmhassCoordinator
from .deferrable import DeferrableRuntime
from .entity import EmhassEntity, EmhassLoadEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: EmhassCoordinator = entry.runtime_data.coordinator
    async_add_entities([EmhassModeSelect(coordinator)])

    for load in entry.runtime_data.loads.all():
        async_add_entities([LoadModeSelect(coordinator, load)], config_subentry_id=load.subentry_id)


class EmhassModeSelect(EmhassEntity, SelectEntity, RestoreEntity):
    """Operating mode.

    Anything other than ``auto`` suspends the executor entirely, giving a
    one-control way to take manual charge without disabling the integration or
    editing automations.
    """

    _attr_translation_key = "system_mode"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(SYSTEM_MODES)

    def __init__(self, coordinator: EmhassCoordinator) -> None:
        super().__init__(coordinator, "system_mode")
        self._current = MODE_AUTO

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in SYSTEM_MODES:
            self._current = last_state.state

    @property
    def current_option(self) -> str:
        return self._current

    async def async_select_option(self, option: str) -> None:
        self._current = option
        self.async_write_ha_state()


class LoadModeSelect(EmhassLoadEntity, SelectEntity, RestoreEntity):
    """Manual override for one deferrable load.

    This overrides the *signal*, not the optimisation: EMHASS still plans the
    load either way. A load that should be left out of planning altogether is
    turned off with its Enabled switch instead, so that "force off" never
    silently changes what is being solved.
    """

    _attr_translation_key = "load_mode"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(LOAD_MODES)

    def __init__(self, coordinator: EmhassCoordinator, load: DeferrableRuntime) -> None:
        super().__init__(coordinator, load, "mode")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in LOAD_MODES:
            self.load.mode = last.state

    @property
    def current_option(self) -> str:
        return self.load.mode

    async def async_select_option(self, option: str) -> None:
        self.load.mode = option
        self.load.notify()

"""Manual override of the system's operating mode."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import COST_FUNS, DEFAULT_COST_FUN, MODE_AUTO, RECURRENCES, SYSTEM_MODES
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
    async_add_entities([EmhassModeSelect(coordinator), EmhassCostFunSelect(coordinator)])

    for load in entry.runtime_data.loads.all():
        # A thermal load's demand is its comfort band, which stands every day
        # by nature -- recurrence is a property of a run-time target.
        if not load.is_thermal:
            async_add_entities(
                [LoadRecurrenceSelect(coordinator, load)], config_subentry_id=load.subentry_id
            )


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
        self.coordinator.system_mode = self._current

    @property
    def current_option(self) -> str:
        return self._current

    async def async_select_option(self, option: str) -> None:
        self._current = option
        self.coordinator.system_mode = option
        self.async_write_ha_state()


class EmhassCostFunSelect(EmhassEntity, SelectEntity, RestoreEntity):
    """EMHASS's optimisation objective: profit, cost, or self-consumption.

    Sent as the ``costfun`` runtime parameter on every request (see
    payload.build_payload), so a change here takes effect on the next
    optimisation run without touching EMHASS's own stored config.
    """

    _attr_translation_key = "cost_fun"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(COST_FUNS)

    def __init__(self, coordinator: EmhassCoordinator) -> None:
        super().__init__(coordinator, "cost_fun")
        self._current = DEFAULT_COST_FUN

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in COST_FUNS:
            self._current = last_state.state
        self.coordinator.cost_fun = self._current

    @property
    def current_option(self) -> str:
        return self._current

    async def async_select_option(self, option: str) -> None:
        self._current = option
        self.coordinator.cost_fun = option
        self.async_write_ha_state()


class LoadRecurrenceSelect(EmhassLoadEntity, SelectEntity, RestoreEntity):
    """What makes a load want to run: the calendar, a request, or spare solar.

    See docs/on_demand_loads.md and docs/surplus_loads.md. Any change clears a
    pending request: what "armed" means differs by recurrence -- on demand it
    is a run of a known length that disarms when finished, on surplus it is
    open-ended -- so carrying one across would leave a load armed under rules
    it was not armed under.
    """

    _attr_translation_key = "recurrence"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(RECURRENCES)

    def __init__(self, coordinator: EmhassCoordinator, load: DeferrableRuntime) -> None:
        super().__init__(coordinator, load, "recurrence")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in RECURRENCES:
            self.load.recurrence = last.state

    @property
    def current_option(self) -> str:
        return self.load.recurrence

    async def async_select_option(self, option: str) -> None:
        if option != self.load.recurrence:
            self.load.cancel()
        self.load.recurrence = option
        self.load.notify()
        await self.coordinator.async_request_refresh()

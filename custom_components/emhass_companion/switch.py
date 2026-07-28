"""The control-authority switch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import EmhassCoordinator
from .deferrable import DeferrableRuntime
from .entity import EmhassEntity, EmhassLoadEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class LoadSwitchDescription(SwitchEntityDescription):
    """A switch backed by one boolean of a deferrable load's live state."""

    get_fn: Callable[[DeferrableRuntime], bool]
    set_fn: Callable[[DeferrableRuntime, bool], None]
    default: bool


def _set_enabled(load: DeferrableRuntime, value: bool) -> None:
    load.enabled = value


def _set_use_window(load: DeferrableRuntime, value: bool) -> None:
    load.use_time_window = value


LOAD_SWITCHES: tuple[LoadSwitchDescription, ...] = (
    LoadSwitchDescription(
        key="enabled",
        translation_key="load_enabled",
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.enabled,
        set_fn=_set_enabled,
        default=True,
    ),
    LoadSwitchDescription(
        key="use_time_window",
        translation_key="use_time_window",
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.use_time_window,
        set_fn=_set_use_window,
        default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: EmhassCoordinator = entry.runtime_data.coordinator
    async_add_entities([EmhassControlSwitch(coordinator)])

    for load in entry.runtime_data.loads.all():
        async_add_entities(
            (LoadSwitch(coordinator, load, description) for description in LOAD_SWITCHES),
            config_subentry_id=load.subentry_id,
        )


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
        self._publish()

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._set(False)

    def _set(self, value: bool) -> None:
        self._is_on = value
        self._publish()
        self.async_write_ha_state()

    def _publish(self) -> None:
        # The executor reads this from the coordinator rather than parsing the
        # entity state, so the gate is never ambiguous while the entity is
        # still restoring.
        self.coordinator.control_enabled = self._is_on


class LoadSwitch(EmhassLoadEntity, SwitchEntity, RestoreEntity):
    """One boolean setting of one deferrable load."""

    entity_description: LoadSwitchDescription

    def __init__(
        self,
        coordinator: EmhassCoordinator,
        load: DeferrableRuntime,
        description: LoadSwitchDescription,
    ) -> None:
        super().__init__(coordinator, load, description.key)
        self.entity_description = description

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self.entity_description.set_fn(self.load, last.state == STATE_ON)

    @property
    def is_on(self) -> bool:
        return self.entity_description.get_fn(self.load)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._set(False)

    def _set(self, value: bool) -> None:
        self.entity_description.set_fn(self.load, value)
        self.load.notify()

"""Buttons to trigger optimisations on demand."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EmhassCoordinator
from .entity import EmhassEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class EmhassButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[EmhassCoordinator], Coroutine[Any, Any, None]]


BUTTONS: tuple[EmhassButtonDescription, ...] = (
    EmhassButtonDescription(
        key="run_dayahead",
        translation_key="run_dayahead",
        press_fn=lambda coordinator: coordinator.async_run_dayahead(),
    ),
    EmhassButtonDescription(
        key="run_mpc",
        translation_key="run_mpc",
        press_fn=lambda coordinator: coordinator.async_run_mpc(),
    ),
)

FORECAST_FIT_BUTTON = EmhassButtonDescription(
    key="run_forecast_fit",
    translation_key="run_forecast_fit",
    press_fn=lambda coordinator: coordinator.async_run_forecast_fit(),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: EmhassCoordinator = entry.runtime_data.coordinator

    descriptions = list(BUTTONS)
    # Only meaningful when the load profile trains EMHASS's own mlforecaster --
    # otherwise there is no model to fit and pressing it would just fail.
    if coordinator.uses_mlforecaster:
        descriptions.append(FORECAST_FIT_BUTTON)

    async_add_entities(EmhassButton(coordinator, description) for description in descriptions)


class EmhassButton(EmhassEntity, ButtonEntity):
    entity_description: EmhassButtonDescription
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: EmhassCoordinator, description: EmhassButtonDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self.coordinator)

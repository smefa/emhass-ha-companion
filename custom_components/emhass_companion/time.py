"""Per-load time window.

Stored as wall-clock times and converted to EMHASS timestep indices on every
request, because those indices are relative to the moment an optimisation is
launched -- a window converted once would slide through the day.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import time

from homeassistant.components.time import TimeEntity, TimeEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import EmhassCoordinator
from .deferrable import DeferrableRuntime
from .entity import EmhassLoadEntity
from .thermal import DEFAULT_COMFORT_END, DEFAULT_COMFORT_START

PARALLEL_UPDATES = 0

DEFAULT_EARLIEST = time(22, 0)
DEFAULT_LATEST = time(6, 0)


@dataclass(frozen=True, kw_only=True)
class LoadTimeDescription(TimeEntityDescription):
    get_fn: Callable[[DeferrableRuntime], time | None]
    set_fn: Callable[[DeferrableRuntime, time], None]
    fallback: time

    available_fn: Callable[[DeferrableRuntime], bool] | None = None
    """When this end of the window is actually consulted; see the matching
    field on ``LoadNumberDescription``."""

    applies_fn: Callable[[DeferrableRuntime], bool] | None = None
    """Whether this load has the setting at all; see the matching field on
    ``LoadNumberDescription``."""


def _set_earliest(load: DeferrableRuntime, value: time) -> None:
    load.earliest_start = value


def _set_latest(load: DeferrableRuntime, value: time) -> None:
    load.latest_end = value


def _set_comfort_start(load: DeferrableRuntime, value: time) -> None:
    load.comfort_start = value


def _set_comfort_end(load: DeferrableRuntime, value: time) -> None:
    load.comfort_end = value


LOAD_TIMES: tuple[LoadTimeDescription, ...] = (
    LoadTimeDescription(
        key="earliest_start",
        translation_key="earliest_start",
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.earliest_start,
        set_fn=_set_earliest,
        fallback=DEFAULT_EARLIEST,
        available_fn=lambda load: load.use_time_window,
    ),
    LoadTimeDescription(
        key="latest_end",
        translation_key="latest_end",
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.latest_end,
        set_fn=_set_latest,
        fallback=DEFAULT_LATEST,
        available_fn=lambda load: load.use_time_window,
    ),
    # The comfort window: when the comfort temperature applies, with the
    # setback temperature holding outside it. Always consulted for a thermal
    # load, unlike the run window above, so no gating switch.
    LoadTimeDescription(
        key="comfort_start",
        translation_key="comfort_start",
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.comfort_start,
        set_fn=_set_comfort_start,
        fallback=DEFAULT_COMFORT_START,
        applies_fn=lambda load: load.is_thermal,
    ),
    LoadTimeDescription(
        key="comfort_end",
        translation_key="comfort_end",
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.comfort_end,
        set_fn=_set_comfort_end,
        fallback=DEFAULT_COMFORT_END,
        applies_fn=lambda load: load.is_thermal,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: EmhassCoordinator = entry.runtime_data.coordinator
    for load in entry.runtime_data.loads.all():
        async_add_entities(
            (
                LoadTime(coordinator, load, description)
                for description in LOAD_TIMES
                if description.applies_fn is None or description.applies_fn(load)
            ),
            config_subentry_id=load.subentry_id,
        )


class LoadTime(EmhassLoadEntity, TimeEntity, RestoreEntity):
    """One end of a deferrable load's allowed time window.

    Whether the window applies at all is a separate switch, so that a window
    can be turned off without having to invent a "no time" value here.
    """

    entity_description: LoadTimeDescription

    def __init__(
        self,
        coordinator: EmhassCoordinator,
        load: DeferrableRuntime,
        description: LoadTimeDescription,
    ) -> None:
        super().__init__(coordinator, load, description.key)
        self.entity_description = description

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            # An unknown/unavailable restored state is normal on first start.
            with suppress(ValueError):
                self.entity_description.set_fn(self.load, time.fromisoformat(last.state))

    @property
    def available(self) -> bool:
        if (available_fn := self.entity_description.available_fn) is None:
            return super().available
        return super().available and available_fn(self.load)

    @property
    def native_value(self) -> time:
        # A window has to show something even when the user never set one; the
        # switch is what decides whether it is honoured.
        return self.entity_description.get_fn(self.load) or self.entity_description.fallback

    async def async_set_value(self, value: time) -> None:
        self.entity_description.set_fn(self.load, value)
        self.load.notify()
        await self.coordinator.async_request_refresh()

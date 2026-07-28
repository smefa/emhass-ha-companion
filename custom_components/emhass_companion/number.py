"""Per-load numeric settings.

These use ``RestoreNumber`` rather than writing back into the config subentry.
Updating a config entry triggers a reload, which is far too heavy for a value
a user nudges from a dashboard; the subentry supplies the initial value and the
entity owns it thereafter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EmhassCoordinator
from .deferrable import DeferrableRuntime
from .entity import EmhassLoadEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class LoadNumberDescription(NumberEntityDescription):
    """A number backed by one field of a deferrable load's live state."""

    get_fn: Callable[[DeferrableRuntime], float]
    set_fn: Callable[[DeferrableRuntime, float], None]


def _set_nominal_power(load: DeferrableRuntime, value: float) -> None:
    load.nominal_power_w = value


def _set_operating_hours(load: DeferrableRuntime, value: float) -> None:
    load.operating_hours = value


LOAD_NUMBERS: tuple[LoadNumberDescription, ...] = (
    LoadNumberDescription(
        key="nominal_power",
        translation_key="nominal_power",
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        native_min_value=1,
        native_max_value=100000,
        native_step=10,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.nominal_power_w,
        set_fn=_set_nominal_power,
    ),
    LoadNumberDescription(
        key="operating_hours",
        translation_key="operating_hours",
        native_unit_of_measurement=UnitOfTime.HOURS,
        native_min_value=0,
        native_max_value=24,
        native_step=0.25,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.operating_hours,
        set_fn=_set_operating_hours,
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
            (LoadNumber(coordinator, load, description) for description in LOAD_NUMBERS),
            config_subentry_id=load.subentry_id,
        )


class LoadNumber(EmhassLoadEntity, RestoreNumber):
    """One numeric setting of one deferrable load."""

    entity_description: LoadNumberDescription

    def __init__(
        self,
        coordinator: EmhassCoordinator,
        load: DeferrableRuntime,
        description: LoadNumberDescription,
    ) -> None:
        super().__init__(coordinator, load, description.key)
        self.entity_description = description

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # RestoreNumber, not RestoreEntity: the latter restores the formatted
        # state string rather than the native value.
        if (last := await self.async_get_last_number_data()) is not None and (
            last.native_value is not None
        ):
            self.entity_description.set_fn(self.load, last.native_value)

    @property
    def native_value(self) -> float:
        return self.entity_description.get_fn(self.load)

    async def async_set_native_value(self, value: float) -> None:
        self.entity_description.set_fn(self.load, value)
        self.load.notify()

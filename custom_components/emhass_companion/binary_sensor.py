"""Binary sensors describing plan health."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import EmhassCoordinator
from .entity import EmhassEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: EmhassCoordinator = entry.runtime_data.coordinator
    async_add_entities([EmhassPlanStaleBinarySensor(coordinator)])


class EmhassPlanStaleBinarySensor(EmhassEntity, BinarySensorEntity):
    """Whether the current plan is too old to be acted on.

    This is what an executor gates on: a plan that has stopped being refreshed
    describes a world that no longer exists, and following it is worse than
    falling back to a safe default.
    """

    _attr_translation_key = "plan_stale"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EmhassCoordinator) -> None:
        super().__init__(coordinator, "plan_stale")

    @property
    def is_on(self) -> bool:
        return self.coordinator.plan_is_stale

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        last_success = self.coordinator.data.last_success if self.coordinator.data else None
        return {
            "last_successful_run": (
                dt_util.as_local(last_success).isoformat() if last_success else None
            ),
            "stale_after": str(self.coordinator.config.stale_after),
        }

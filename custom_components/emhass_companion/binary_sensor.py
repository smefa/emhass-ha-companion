"""Binary sensors describing plan health."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import LOAD_MODE_AUTO
from .coordinator import EmhassCoordinator
from .deferrable import DeferrableRuntime, resolve_should_run
from .entity import EmhassEntity, EmhassLoadEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: EmhassCoordinator = entry.runtime_data.coordinator
    async_add_entities([EmhassPlanStaleBinarySensor(coordinator)])

    for load in entry.runtime_data.loads.all():
        async_add_entities(
            [LoadShouldRunBinarySensor(coordinator, load)],
            config_subentry_id=load.subentry_id,
        )


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


class LoadShouldRunBinarySensor(EmhassLoadEntity, BinarySensorEntity):
    """Whether this load should be running right now.

    This is the entity to automate against. It combines the plan's answer with
    any manual override, and reports unknown rather than off when there is no
    usable plan -- "we do not know" and "definitely do not run" are different
    instructions, and conflating them silently keeps a load off.
    """

    _attr_translation_key = "should_run"

    def __init__(self, coordinator: EmhassCoordinator, load: DeferrableRuntime) -> None:
        super().__init__(coordinator, load, "should_run")

    @property
    def is_on(self) -> bool | None:
        if self.load.mode != LOAD_MODE_AUTO:
            return resolve_should_run(self.load.mode, False)
        if (scheduled := self._scheduled_power()) is None:
            return None
        return resolve_should_run(self.load.mode, scheduled > self.load.running_threshold_w)

    def _scheduled_power(self) -> float | None:
        data = self.coordinator.data
        if not data or data.plan is None or self.coordinator.plan_is_stale:
            return None
        if (index := self.plan_index) is None:
            return None
        row = data.plan.row_at(dt_util.utcnow())
        if row is None or index >= len(row.deferrables):
            return None
        return row.deferrables[index]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "mode": self.load.mode,
            "scheduled_power": self._scheduled_power(),
            "currently_running": self.load.is_running,
        }

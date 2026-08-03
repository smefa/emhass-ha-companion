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
    async_add_entities(
        [EmhassPlanStaleBinarySensor(coordinator), SolarSurplusBinarySensor(coordinator)]
    )

    for load in entry.runtime_data.loads.all():
        async_add_entities(
            [
                LoadShouldRunBinarySensor(coordinator, load),
                LoadRunningBinarySensor(coordinator, load),
            ],
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


class SolarSurplusBinarySensor(EmhassEntity, BinarySensorEntity):
    """Whether the plan expects spare solar right now.

    The household-level answer, against the hub's reporting threshold -- not
    the per-load one. A load's own question is "is there enough for *me*", and
    that is its ``should_run``, which comes from the optimiser rather than from
    here.
    """

    _attr_translation_key = "solar_surplus"

    def __init__(self, coordinator: EmhassCoordinator) -> None:
        super().__init__(coordinator, "solar_surplus")

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.surplus_series().value_at(dt_util.utcnow())
        if value is None:
            # No plan, or one that does not reach now. "We do not know" and
            # "there is definitely no spare sun" lead to different automations.
            return None
        return value >= self.coordinator.surplus_threshold_w

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "surplus_w": self.coordinator.surplus_series().value_at(dt_util.utcnow()),
            "threshold": self.coordinator.surplus_threshold_w,
        }


class LoadShouldRunBinarySensor(EmhassLoadEntity, BinarySensorEntity):
    """Whether this load should be running right now.

    This is the entity to automate against. It combines the plan's answer with
    any manual override, and reports unknown rather than off when there is no
    usable plan -- "we do not know" and "definitely do not run" are different
    instructions, and conflating them silently keeps a load off.

    A load that was deliberately left out of the optimisation is the third
    case, and it is a definite *off*: the plan is perfectly good, this load
    simply is not in it, because it is disabled or is an on-demand load
    nobody has asked for. Reporting that as unknown leaves an automation
    unable to tell "I turned this off" from "the integration is broken", and
    leaves the load permanently unknown for as long as it stays off.
    """

    _attr_translation_key = "should_run"

    def __init__(self, coordinator: EmhassCoordinator, load: DeferrableRuntime) -> None:
        super().__init__(coordinator, load, "should_run")

    @property
    def is_on(self) -> bool | None:
        if self.load.mode != LOAD_MODE_AUTO:
            return resolve_should_run(self.load.mode, False)
        if not self.load.enabled or not self.load.participates:
            return False
        if (scheduled := self._scheduled_power()) is None:
            return None
        return resolve_should_run(self.load.mode, scheduled > self.load.running_threshold_w)

    @property
    def _reason(self) -> str:
        """Why the state is what it is -- the question this sensor provokes."""
        if self.load.mode != LOAD_MODE_AUTO:
            return self.load.mode
        if not self.load.enabled:
            return "disabled"
        if not self.load.participates:
            return "not requested"
        data = self.coordinator.data
        if not data or data.plan is None:
            return "no plan yet"
        if self.coordinator.plan_is_stale:
            return "plan is out of date"
        if self.plan_index is None:
            return "not in the last optimisation"
        if self._scheduled_power() is None:
            return "plan does not cover now"
        return "following the plan"

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
            "reason": self._reason,
            "emhass_deferrable": self.deferrable_number,
        }


class LoadRunningBinarySensor(EmhassLoadEntity, BinarySensorEntity):
    """Whether this load is observed to be running right now.

    The counterpart to ``should_run``: what the load is actually doing, rather
    than what the plan wants. This is the state fed back to EMHASS as
    ``def_current_state``, and the runtime it accumulates becomes
    ``def_current_operating_timesteps`` -- EMHASS itself remembers nothing
    between runs, so without this it would happily re-schedule a wash cycle
    that finished an hour ago.

    Reports off, not unknown, when there is nothing to observe: "we have no
    way of knowing" and "not running" lead to the same request here, and an
    unknown state would only make the sensor useless in a template.
    """

    _attr_translation_key = "running"

    def __init__(self, coordinator: EmhassCoordinator, load: DeferrableRuntime) -> None:
        super().__init__(coordinator, load, "running")

    @property
    def is_on(self) -> bool:
        return self.load.is_running

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        elapsed = self.load.elapsed_today(dt_util.utcnow())
        return {
            "running_source": self.load.running_source,
            "runtime_today": str(elapsed).split(".")[0],
            "completed_timesteps": self.load.completed_timesteps(
                dt_util.utcnow(), self.coordinator.config.time_step_minutes
            ),
        }

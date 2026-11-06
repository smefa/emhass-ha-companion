"""Live, config-entry-backed hot-water plan controls.

The saved plan is the single source of truth; entity restore state must not
overwrite a newer plan saved by an automation or the setup wizard.
"""

from __future__ import annotations

from datetime import time
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from .const import CONF_DRAW_END, CONF_DRAW_ENERGY, CONF_DRAW_START, CONF_HOT_WATER_PERIODS, DOMAIN
from .entity import EmhassLoadEntity
from .water_tank import MAX_HOT_WATER_PERIODS, hot_water_periods_from_data


def canonical_plan(data: Any) -> list[dict[str, Any]]:
    """Validate the entire plan before changing any saved or running state."""
    return [
        {
            CONF_DRAW_START: period.start.isoformat(),
            CONF_DRAW_END: period.end.isoformat(),
            CONF_DRAW_ENERGY: period.energy_kwh,
            "name": period.name,
            "enabled": period.enabled,
        }
        for period in hot_water_periods_from_data(data)
    ]


async def async_set_plan(coordinator, load, data: Any) -> None:
    """Save atomically, notify entities and optimise if the load is enabled."""
    if not load.is_water_tank or load.hot_water_periods is None:
        raise ValueError("This load does not have a hot-water period plan")
    plan = canonical_plan(data)
    entry = coordinator.config_entry
    subentry = entry.subentries[load.subentry_id]
    # Config entry notifications may run synchronously. Publish the runtime
    # first so its change listener does not request a second solve.
    previous = load.hot_water_periods
    load.hot_water_periods = plan
    try:
        coordinator.hass.config_entries.async_update_subentry(
            entry, subentry, data={**subentry.data, CONF_HOT_WATER_PERIODS: plan}
        )
    except Exception:
        load.hot_water_periods = previous
        raise
    load.invalidate_battery_lockout()
    load.notify()
    if load.enabled:
        await coordinator.async_request_settings_refresh()


@callback
def async_setup_period_entities(entry, async_add_entities, entity_type) -> None:
    """Add new period slots live; removed slots become unavailable.

    Slot indices are stable across names and enable/disable changes. Listening
    to the load also allows the whole-plan action to add slots without reload.
    """
    coordinator = entry.runtime_data.coordinator
    registry = er.async_get(coordinator.hass)
    domain = (
        "number"
        if issubclass(entity_type, NumberEntity)
        else "switch"
        if issubclass(entity_type, SwitchEntity)
        else "time"
    )
    for load in entry.runtime_data.loads.all():
        if not load.is_water_tank:
            continue
        seen: set[int] = set()

        @callback
        def add_slots(load=load, seen=seen) -> None:
            periods = load.hot_water_periods
            if not isinstance(periods, list):
                return
            # Keep removed slots managed and unavailable across restarts too,
            # preserving custom entity IDs for future reuse of those slots.
            existing = {
                index
                for index in range(MAX_HOT_WATER_PERIODS)
                if registry.async_get_entity_id(
                    domain,
                    DOMAIN,
                    f"{entry.entry_id}_{load.subentry_id}_hot_water_period_{index + 1}_{entity_type.label}",
                )
                is not None
            }
            new = (set(range(min(len(periods), MAX_HOT_WATER_PERIODS))) | existing) - seen
            if not new:
                return
            seen.update(new)
            async_add_entities(
                [entity_type(coordinator, load, index) for index in sorted(new)],
                config_subentry_id=load.subentry_id,
            )

        add_slots()
        entry.async_on_unload(load.add_listener(add_slots))


class PeriodEntity(EmhassLoadEntity):
    """One field of a numbered hot-water period, available even when off."""

    _attr_entity_category = EntityCategory.CONFIG
    field: str
    label: str

    def __init__(self, coordinator, load, index: int) -> None:
        super().__init__(coordinator, load, f"hot_water_period_{index + 1}_{self.label}")
        self.index = index
        self._attr_translation_key = f"hot_water_period_{self.label}"
        self._attr_translation_placeholders = {"period": str(index + 1)}

    @property
    def period(self) -> dict[str, Any] | None:
        periods = self.load.hot_water_periods
        if isinstance(periods, list) and self.index < len(periods):
            period = periods[self.index]
            if isinstance(period, dict):
                return period
        return None

    @property
    def available(self) -> bool:
        return self.period is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"period_name": (self.period or {}).get("name", "")}

    async def async_set_field(self, value: Any) -> None:
        if self.period is None:
            raise ValueError("This hot-water period no longer exists")
        plan = [dict(period) for period in self.load.hot_water_periods]
        plan[self.index][self.field] = value
        await async_set_plan(self.coordinator, self.load, plan)


class PeriodStart(PeriodEntity, TimeEntity):
    field = CONF_DRAW_START
    label = "start"

    @property
    def native_value(self) -> time | None:
        try:
            return time.fromisoformat((self.period or {})[self.field])
        except (KeyError, TypeError, ValueError):
            return None

    async def async_set_value(self, value: time) -> None:
        await self.async_set_field(value.isoformat())


class PeriodEnd(PeriodStart):
    field = CONF_DRAW_END
    label = "end"


class PeriodEnergy(PeriodEntity, NumberEntity):
    field = CONF_DRAW_ENERGY
    label = "energy"
    _attr_native_min_value = 0
    _attr_native_max_value = 1000
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float | None:
        return (self.period or {}).get(self.field)

    async def async_set_native_value(self, value: float) -> None:
        await self.async_set_field(value)


class PeriodEnabled(PeriodEntity, SwitchEntity):
    field = "enabled"
    label = "enabled"

    @property
    def is_on(self) -> bool:
        return (self.period or {}).get(self.field, True)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_set_field(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_set_field(False)

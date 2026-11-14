"""Per-load Power: one value, or a comma-separated sequence of stages.

Like the numbers, this is entity-owned after creation: the subentry seeds it,
and an edit applies live without reloading the entry. One value is a constant
load; two or more are one stage per optimisation step (see
``deferrable.parse_power``).
"""

from __future__ import annotations

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import EmhassCoordinator
from .deferrable import DeferrableRuntime, expand_power, format_power, parse_power
from .entity import EmhassLoadEntity

PARALLEL_UPDATES = 0

POWER_KEY = "nominal_power"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: EmhassCoordinator = entry.runtime_data.coordinator
    for load in entry.runtime_data.loads.all():
        if load.is_thermal:
            # A thermal load's power is set by the payload, and EMHASS ignores
            # a sequence there; it keeps its plain number.
            continue
        async_add_entities([LoadPowerText(coordinator, load)], config_subentry_id=load.subentry_id)


class LoadPowerText(EmhassLoadEntity, RestoreEntity, TextEntity):
    """The Power a deferrable load draws while running."""

    _attr_translation_key = POWER_KEY
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.TEXT
    _attr_native_min = 1
    _attr_native_max = 255

    def __init__(self, coordinator: EmhassCoordinator, load: DeferrableRuntime) -> None:
        super().__init__(coordinator, load, POWER_KEY)

    def _apply(self, values: tuple[float, ...]) -> None:
        self.load.power_sequence = values if len(values) > 1 else ()
        self.load.nominal_power_w = max(values)

    @property
    def native_value(self) -> str:
        return format_power(self.load.power_values)

    async def async_set_value(self, value: str) -> None:
        values = parse_power(value)
        if len(values) > 1:
            if self.load.on_surplus:
                raise ServiceValidationError("Sequence loads do not support surplus recurrence")
            expand_power(
                values, self.load.operating_hours, self.coordinator.config.time_step_minutes
            )
        was_sequence = self.load.is_sequence
        self._apply(values)
        if was_sequence and not self.load.is_sequence and self.load.sequence_start_pending:
            # No cycle to start any more; a pending start would hold it on.
            self.load.sequence_start_pending = False
            self.load.sequence_changed()
        self.load.invalidate_battery_lockout()
        self.load.notify()
        if self.load.enabled:
            await self.coordinator.async_request_settings_refresh()

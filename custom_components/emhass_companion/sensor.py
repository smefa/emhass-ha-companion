"""Sensors exposing the current plan."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import BATTERY_ACTIONS
from .coordinator import EmhassCoordinator, EmhassData
from .deferrable import DeferrableRuntime
from .entity import EmhassEntity, EmhassLoadEntity
from .models import Series

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EmhassSensorDescription(SensorEntityDescription):
    """Describes a sensor derived from the plan."""

    value_fn: Callable[[EmhassData, datetime], Any]
    series_fn: Callable[[EmhassData], Series] | None = None
    attrs_fn: Callable[[EmhassData], dict[str, Any]] | None = None


def _plan_value(attribute: str) -> Callable[[EmhassData, datetime], Any]:
    def _value(data: EmhassData, now: datetime) -> Any:
        if data.plan is None or (row := data.plan.row_at(now)) is None:
            return None
        return getattr(row, attribute)

    return _value


def _plan_series(attribute: str) -> Callable[[EmhassData], Series]:
    def _series(data: EmhassData) -> Series:
        return data.plan.series(attribute) if data.plan else Series.empty()

    return _series


POWER_SENSORS: tuple[EmhassSensorDescription, ...] = (
    EmhassSensorDescription(
        key="pv_forecast",
        translation_key="pv_forecast",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_plan_value("p_pv"),
        series_fn=_plan_series("p_pv"),
    ),
    EmhassSensorDescription(
        key="load_forecast",
        translation_key="load_forecast",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_plan_value("p_load"),
        series_fn=_plan_series("p_load"),
    ),
    EmhassSensorDescription(
        key="grid_forecast",
        translation_key="grid_forecast",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        # Positive is import, negative is export, matching EMHASS's convention.
        value_fn=_plan_value("p_grid"),
        series_fn=_plan_series("p_grid"),
    ),
)

BATTERY_SENSORS: tuple[EmhassSensorDescription, ...] = (
    EmhassSensorDescription(
        key="battery_power",
        translation_key="battery_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        # Positive is discharge, negative is charge.
        value_fn=_plan_value("p_batt"),
        series_fn=_plan_series("p_batt"),
    ),
    EmhassSensorDescription(
        key="battery_soc",
        translation_key="battery_soc",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        # soc_percent, not soc: the scaling from EMHASS's fraction happens once,
        # in the model.
        value_fn=_plan_value("soc_percent"),
        series_fn=lambda data: data.plan.series("soc_percent") if data.plan else Series.empty(),
    ),
)

PRICE_SENSORS: tuple[EmhassSensorDescription, ...] = (
    EmhassSensorDescription(
        key="buy_price",
        translation_key="buy_price",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda data, now: data.buy_price.value_at(now),
        series_fn=lambda data: data.buy_price,
    ),
    EmhassSensorDescription(
        key="sell_price",
        translation_key="sell_price",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda data, now: data.sell_price.value_at(now),
        series_fn=lambda data: data.sell_price,
    ),
)

DIAGNOSTIC_SENSORS: tuple[EmhassSensorDescription, ...] = (
    EmhassSensorDescription(
        key="optimization_status",
        translation_key="optimization_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, now: data.last_run.status if data.last_run else None,
        attrs_fn=lambda data: _run_attributes(data),
    ),
    EmhassSensorDescription(
        key="plan_cost",
        translation_key="plan_cost",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data, now: data.plan.total_cost if data.plan else None,
    ),
    EmhassSensorDescription(
        key="last_payload",
        translation_key="last_payload",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        # EMHASS's own configuration screen shows stale values by design, since
        # every run overrides it. This sensor is therefore the authoritative
        # answer to "what was EMHASS actually asked to solve".
        value_fn=lambda data, now: len(data.payload) or None,
        attrs_fn=lambda data: {
            "payload": data.payload,
            "warnings": data.warnings,
            "deferrable_order": data.load_order,
        },
    ),
)


def _run_attributes(data: EmhassData) -> dict[str, Any]:
    if data.last_run is None:
        return {}
    return {
        "action": data.last_run.action,
        "infeasible": data.last_run.infeasible,
        "duration_seconds": data.last_run.duration_seconds,
        "emhass_version": data.last_run.emhass_version,
        "schema_version": data.last_run.schema_version,
        "error_message": data.last_run.error_message,
        "stage_times": data.last_run.stage_times,
        "warnings": data.warnings,
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator: EmhassCoordinator = entry.runtime_data.coordinator

    descriptions = [*POWER_SENSORS, *PRICE_SENSORS, *DIAGNOSTIC_SENSORS]
    # Battery sensors would be permanently None without a battery, so they are
    # simply not created rather than cluttering the device.
    if coordinator.config.battery.enabled:
        descriptions.extend(BATTERY_SENSORS)

    async_add_entities(EmhassSensor(coordinator, description) for description in descriptions)
    async_add_entities([EmhassDecisionSensor(coordinator)])


class EmhassSensor(EmhassEntity, SensorEntity):
    """A sensor reading one value out of the current plan."""

    entity_description: EmhassSensorDescription

    def __init__(
        self, coordinator: EmhassCoordinator, description: EmhassSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data, dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        attributes: dict[str, Any] = {}

        if self.entity_description.series_fn is not None:
            # The forward-looking series is the whole point of an optimiser;
            # carrying it as an attribute is what lets a card draw the plan.
            series = self.entity_description.series_fn(data)
            attributes["forecast"] = series.to_attribute()

        if self.entity_description.attrs_fn is not None:
            attributes.update(self.entity_description.attrs_fn(data))

        return attributes or None


class LoadScheduledPowerSensor(EmhassLoadEntity, SensorEntity):
    """The power EMHASS has scheduled for this load right now."""

    _attr_translation_key = "scheduled_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: EmhassCoordinator, load: DeferrableRuntime) -> None:
        super().__init__(coordinator, load, "scheduled_power")

    def _series(self) -> Series:
        data = self.coordinator.data
        if not data or data.plan is None or (index := self.plan_index) is None:
            return Series.empty()
        return data.plan.deferrable_series(index)

    @property
    def native_value(self) -> float | None:
        return self._series().value_at(dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"schedule": self._series().to_attribute()}


class LoadNextStartSensor(EmhassLoadEntity, SensorEntity):
    """When this load is next scheduled to start."""

    _attr_translation_key = "next_start"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: EmhassCoordinator, load: DeferrableRuntime) -> None:
        super().__init__(coordinator, load, "next_start")

    @property
    def native_value(self) -> datetime | None:
        data = self.coordinator.data
        if not data or data.plan is None or (index := self.plan_index) is None:
            return None

        threshold = self.load.running_threshold_w
        now = dt_util.utcnow()
        previous_on = True  # suppress a "start" for a run already in progress
        for row in data.plan.rows:
            if index >= len(row.deferrables):
                continue
            running = row.deferrables[index] > threshold
            if row.timestamp >= now and running and not previous_on:
                return row.timestamp
            if row.timestamp >= now:
                previous_on = running
        return None


class LoadRuntimeTodaySensor(EmhassLoadEntity, RestoreSensor):
    """How long this load has run today.

    Fed back to EMHASS as ``def_current_operating_timesteps`` so it does not
    re-schedule work already done. EMHASS has no concept of a day boundary for
    that value, so the counter is reset locally at midnight.
    """

    _attr_translation_key = "runtime_today"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: EmhassCoordinator, load: DeferrableRuntime) -> None:
        super().__init__(coordinator, load, "runtime_today")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is None or last.native_value is None:
            return
        # Only restore within the same local day; a restart tomorrow morning
        # must not carry yesterday's hours into today's schedule.
        restored_at = self.coordinator.hass.states.get(self.entity_id)
        today = dt_util.as_local(dt_util.utcnow()).date()
        if restored_at is not None and dt_util.as_local(restored_at.last_updated).date() != today:
            return
        try:
            self.load.runtime_today = timedelta(hours=float(last.native_value))
            self.load.runtime_day = today
        except (TypeError, ValueError):
            pass

    @property
    def native_value(self) -> float:
        return round(self.load.elapsed_today(dt_util.utcnow()).total_seconds() / 3600, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        step = self.coordinator.config.time_step_minutes
        return {
            "completed_timesteps": self.load.completed_timesteps(dt_util.utcnow(), step),
            "currently_running": self.load.is_running,
            "power_sensor": self.load.power_sensor,
        }


class EmhassDecisionSensor(EmhassEntity, SensorEntity):
    """What the executor last decided to do.

    This is what makes the dry-run gate useful rather than merely safe: while
    control is disabled the decision is still computed and shown here, along
    with the exact service calls it resolved to, so it can be compared against
    whatever automations are currently in charge before handing over.
    """

    _attr_translation_key = "battery_action"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(BATTERY_ACTIONS)

    def __init__(self, coordinator: EmhassCoordinator) -> None:
        super().__init__(coordinator, "battery_action")

    @property
    def native_value(self) -> str | None:
        decision = self.coordinator.config_entry.runtime_data.executor.last_decision
        return decision.action if decision else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        executor = self.coordinator.config_entry.runtime_data.executor
        decision = executor.last_decision
        attributes: dict[str, Any] = {"control_enabled": executor.control_enabled}
        if decision is not None:
            attributes.update(decision.as_attributes())
        return attributes

"""Sensors exposing the current plan."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import EmhassCoordinator, EmhassData
from .entity import EmhassEntity
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

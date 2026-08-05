"""Sensors exposing the current plan."""

from __future__ import annotations

from collections.abc import Callable, Iterable
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
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import BATTERY_ACTIONS, NET_HOUSE_LOAD_KEY
from .coordinator import EmhassCoordinator, EmhassData
from .deferrable import DeferrableRuntime, state_to_watts
from .entity import EmhassEntity, EmhassLoadEntity
from .models import Series
from .surplus import total_energy_wh, window_of

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
        suggested_display_precision=1,
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
    async_add_entities(
        [
            EmhassDecisionSensor(coordinator),
            SolarSurplusSensor(coordinator),
            SolarSurplusEnergySensor(coordinator),
            SolarSurplusStartSensor(coordinator),
            SolarSurplusEndSensor(coordinator),
        ]
    )

    # Only present for an entry set up through "Create a house load sensor" --
    # every other load profile has nothing for it to subtract into.
    if total_entity := coordinator.config.house_load_total_entity:
        async_add_entities(
            [NetHouseLoadSensor(coordinator, total_entity, entry.runtime_data.loads.all())]
        )

    # These three were written, translated and unit-tested, but never actually
    # added to Home Assistant, so no load ever had them -- which is also why
    # the deferrable card had no schedule to draw.
    for load in entry.runtime_data.loads.all():
        entities: list[SensorEntity] = [
            LoadScheduledPowerSensor(coordinator, load),
            LoadNextStartSensor(coordinator, load),
            LoadRuntimeTodaySensor(coordinator, load),
            LoadDeferrableNumberSensor(coordinator, load),
        ]
        if load.is_thermal:
            entities.append(LoadPlannedTemperatureSensor(coordinator, load))
        else:
            # Recurrence is a live setting, so a load can become a surplus load
            # long after setup; the sensor is created for every standard load
            # and reports unavailable until it is one.
            entities.append(LoadSurplusBudgetSensor(coordinator, load))
        async_add_entities(entities, config_subentry_id=load.subentry_id)


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


class NetHouseLoadSensor(EmhassEntity, SensorEntity):
    """Whole-house power with every deferrable/thermal load's current draw subtracted.

    Backs the "Select a house load sensor (without deferrables)" profile when
    it was auto-configured by the "Create a house load sensor" setup step,
    which points that profile's ``entity`` option at this sensor rather than
    asking the user to build their own subtracting template sensor. EMHASS
    pulls its load-forecast history straight from this entity's own recorded
    state, so it needs real, frequent state changes of its own -- unlike the
    rest of this device it therefore does not update from the coordinator
    (which only refreshes once per optimisation run), but from its own
    subscriptions to the total sensor and every load's running source.
    """

    _attr_translation_key = "net_house_load"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: EmhassCoordinator,
        total_entity: str,
        loads: Iterable[DeferrableRuntime],
    ) -> None:
        super().__init__(coordinator, NET_HOUSE_LOAD_KEY)
        self._total_entity = total_entity
        self._loads = list(loads)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        sources = {self._total_entity}
        sources.update(source for load in self._loads if (source := load.running_source))
        self.async_on_remove(
            async_track_state_change_event(self.hass, list(sources), self._async_source_changed)
        )

    @callback
    def _async_source_changed(self, event: Event[EventStateChangedData]) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # Unlike the rest of this device (see EmhassEntity.available), this
        # sensor's whole value is the total entity's reading -- with nothing
        # to subtract from, reporting a number would just be the deferrables'
        # draw counted as free, silently wrong rather than absent.
        state = self.hass.states.get(self._total_entity)
        return state is not None and state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, "")

    @property
    def native_value(self) -> float | None:
        total_state = self.hass.states.get(self._total_entity)
        if total_state is None:
            return None
        total = state_to_watts(total_state)
        if total is None:
            return None

        deferred = 0.0
        for load in self._loads:
            source = load.running_source
            if source is None:
                continue
            deferred += load.state_to_power(self.hass.states.get(source)) or 0.0

        # Clamped rather than left negative: the total and each deferrable's
        # power sensor update on their own schedules, not in lockstep, so a
        # deferrable's reading can momentarily lead the total's -- a real
        # negative net load is not a state this sensor can ever mean.
        return max(total - deferred, 0.0)


class LoadDeferrableNumberSensor(EmhassLoadEntity, SensorEntity):
    """Which ``P_deferrable{k}`` this load is inside EMHASS.

    EMHASS names its deferrable loads by number and says nowhere which
    appliance a number belongs to -- its own charts, logs and the sensors it
    publishes are all ``P_deferrable0``, ``P_deferrable1``, and so on. Reading
    any of that against this integration means having the mapping to hand,
    which is what this sensor is for.

    It is meaningful as a stored number only because every configured load is
    now sent on every run, parked at zero hours when it wants nothing; before
    that, disabling one load silently renumbered the others.
    """

    _attr_translation_key = "deferrable_number"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EmhassCoordinator, load: DeferrableRuntime) -> None:
        super().__init__(coordinator, load, "deferrable_number")

    @property
    def native_value(self) -> int | None:
        return self.deferrable_number

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "emhass_variable": f"P_deferrable{self.deferrable_number}",
            # What the load was in the plan currently in hand. Differs from the
            # number above only in the window between adding or removing a load
            # and the next optimisation.
            "in_current_plan": self.plan_index,
        }


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
    def native_value(self) -> float:
        # No plan yet or before the first point still means "nothing
        # scheduled", which is a real 0 W, not an unknown state.
        return self._series().value_at(dt_util.utcnow()) or 0.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "schedule": self._series().to_attribute(),
            "emhass_deferrable": self.deferrable_number,
        }


class LoadPlannedTemperatureSensor(EmhassLoadEntity, SensorEntity):
    """The temperature EMHASS expects this thermal load's room to be at now.

    EMHASS's docs suggest using exactly this as a climate entity's setpoint:
    the plan already accounts for price, so following the predicted trajectory
    is what realises the savings. The full trajectory rides along as a
    ``forecast`` attribute, same shape as every other planned series here.
    """

    _attr_translation_key = "planned_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: EmhassCoordinator, load: DeferrableRuntime) -> None:
        super().__init__(coordinator, load, "planned_temperature")

    def _series(self) -> Series:
        data = self.coordinator.data
        if not data or data.plan is None or (index := self.plan_index) is None:
            return Series.empty()
        return data.plan.temperature_series(index)

    @property
    def native_value(self) -> float | None:
        return self._series().value_at(dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"forecast": self._series().to_attribute()}


class LoadNextStartSensor(EmhassLoadEntity, SensorEntity):
    """When this load is next scheduled to start."""

    _attr_translation_key = "next_start"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    # Why the state is unknown, exposed via extra_state_attributes so the
    # frontend card can show something better than a bare "unknown".
    _REASON_NO_PLAN = "no_plan"
    _REASON_ALREADY_RUNNING = "already_running"
    _REASON_NOT_SCHEDULED = "not_scheduled"

    def __init__(self, coordinator: EmhassCoordinator, load: DeferrableRuntime) -> None:
        super().__init__(coordinator, load, "next_start")

    def _resolve(self) -> tuple[datetime | None, str | None]:
        data = self.coordinator.data
        if not data or data.plan is None or (index := self.plan_index) is None:
            return None, self._REASON_NO_PLAN

        threshold = self.load.running_threshold_w
        now = dt_util.utcnow()

        def _is_running(row) -> bool:
            return index < len(row.deferrables) and row.deferrables[index] > threshold

        # Seed from the plan's own answer to "is it running right now"
        # (row_at's hold-last semantics), not an assumption of True. Assuming
        # already-running silently swallowed the very next start whenever the
        # load was actually off and the immediately following timestep turns
        # it on -- exactly the case this sensor exists to report.
        current_row = data.plan.row_at(now)
        previous_on = _is_running(current_row) if current_row is not None else False

        for row in data.plan.rows:
            if row.timestamp <= now:
                continue
            running = _is_running(row)
            if running and not previous_on:
                return row.timestamp, None
            previous_on = running

        reason = self._REASON_ALREADY_RUNNING if previous_on else self._REASON_NOT_SCHEDULED
        return None, reason

    @property
    def native_value(self) -> datetime | None:
        return self._resolve()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        _, reason = self._resolve()
        return {"reason": reason} if reason else None


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


class SolarSurplusBase(EmhassEntity):
    """Shared reading of the plan's spare PV.

    All four surplus sensors answer questions about one series, so they compute
    it the same way -- through the coordinator, which is also where the budgets
    handed to EMHASS came from. A sensor that derived its own would eventually
    disagree with what the pool was actually given.
    """

    def _series(self) -> Series:
        return self.coordinator.surplus_series()

    def _window(self) -> tuple[datetime | None, datetime | None]:
        return window_of(self._series(), self.coordinator.surplus_threshold_w)


class SolarSurplusSensor(SolarSurplusBase, SensorEntity):
    """PV the plan expects the house not to need, before any surplus load takes it.

    This is the quantity a "run it on spare sun only" load is entitled to:
    generation left after the house and after every *ordinary* scheduled
    deferrable. Deliberately independent of what the battery or the grid do
    with it -- a slot the battery is charging on is still spare PV, just PV
    the optimiser chose to put somewhere else for now, not proof the sun isn't
    up. So this is not the same as exported power: consuming it here may well
    forgo battery charge or export revenue the plan had earmarked for it, the
    same trade a surplus load always makes against the rest of the plan.
    """

    _attr_translation_key = "solar_surplus"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: EmhassCoordinator) -> None:
        super().__init__(coordinator, "solar_surplus")

    @property
    def native_value(self) -> float | None:
        return self._series().value_at(dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        series = self._series()
        start, end = self._window()
        return {
            "forecast": series.to_attribute(),
            "threshold": self.coordinator.surplus_threshold_w,
            "window_start": start.isoformat() if start else None,
            "window_end": end.isoformat() if end else None,
        }


class SolarSurplusEnergySensor(SolarSurplusBase, SensorEntity):
    """How much spare solar is left in the plan's horizon.

    Usually the more useful trigger of the two: whether to start heating a pool
    or charge a car is a question about kilowatt-hours coming, not about the
    watts available at this instant.
    """

    _attr_translation_key = "solar_surplus_energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: EmhassCoordinator) -> None:
        super().__init__(coordinator, "solar_surplus_energy")

    @property
    def native_value(self) -> float | None:
        # Only what is still ahead: energy already exported is not a budget
        # anything can be planned against.
        now = dt_util.utcnow()
        series = Series(point for point in self._series() if point.time >= now)
        if not series:
            return 0.0
        step = series.step() or timedelta(minutes=self.coordinator.config.time_step_minutes)
        return total_energy_wh(series, step) / 1000


class SolarSurplusStartSensor(SolarSurplusBase, SensorEntity):
    """When the plan's surplus first reaches the reporting threshold."""

    _attr_translation_key = "solar_surplus_start"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: EmhassCoordinator) -> None:
        super().__init__(coordinator, "solar_surplus_start")

    @property
    def native_value(self) -> datetime | None:
        return self._window()[0]


class SolarSurplusEndSensor(SolarSurplusBase, SensorEntity):
    """When the plan's surplus was last above the reporting threshold.

    Spans any dips in between rather than closing at the first cloud: "spare
    sun until 15:30" is the answer worth having, and the gaps inside that are
    the optimiser's problem rather than the user's.
    """

    _attr_translation_key = "solar_surplus_end"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: EmhassCoordinator) -> None:
        super().__init__(coordinator, "solar_surplus_end")

    @property
    def native_value(self) -> datetime | None:
        return self._window()[1]


class LoadSurplusBudgetSensor(EmhassLoadEntity, SensorEntity):
    """What this surplus load was allowed to ask EMHASS for on the last run.

    The whole feature is a derivation the user never sees the inputs to, so
    this is the entity that makes it inspectable: a pool that is not running
    is either short of surplus, short of headroom, or beaten to it by a
    higher-priority load, and those look identical from the outside.
    """

    _attr_translation_key = "surplus_budget"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: EmhassCoordinator, load: DeferrableRuntime) -> None:
        super().__init__(coordinator, load, "surplus_budget")

    @property
    def available(self) -> bool:
        return self.load.on_surplus

    @property
    def native_value(self) -> float | None:
        return self.load.surplus_budget.hours

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        budget = self.load.surplus_budget
        remaining = self.load.remaining_energy_wh(dt_util.utcnow())
        return {
            "window_start": (budget.window_start.isoformat() if budget.window_start else None),
            "window_end": budget.window_end.isoformat() if budget.window_end else None,
            "energy_wh": round(budget.energy_wh, 1),
            "timesteps": budget.steps,
            # The threshold this load actually budgets against, which is not the
            # hub's reporting one and is the first thing to check when a load
            # gets nothing on an obviously sunny day.
            "run_floor_w": self.load.surplus_run_floor_w,
            "headroom_w": self.load.surplus_headroom_w,
            "qualifies_above_w": self.load.surplus_run_floor_w + self.load.surplus_headroom_w,
            "energy_remaining_wh": None if remaining is None else round(remaining, 1),
            # What was actually sent to EMHASS as this load's ceiling for the
            # run -- equal to nominal_power_w unless the load may modulate and
            # the day fell short of it. See surplus.allocate.
            "nominal_power_w": round(budget.nominal_w, 1),
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

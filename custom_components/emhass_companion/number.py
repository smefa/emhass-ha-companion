"""Per-load numeric settings.

These use ``RestoreNumber`` rather than writing back into the config subentry.
Updating a config entry triggers a reload, which is far too heavy for a value
a user nudges from a dashboard; the subentry supplies the initial value and the
entity owns it thereafter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, ISSUE_THERMAL_UNREACHABLE
from .coordinator import EmhassCoordinator
from .deferrable import DeferrableRuntime
from .entity import EmhassEntity, EmhassLoadEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class LoadNumberDescription(NumberEntityDescription):
    """A number backed by one field of a deferrable load's live state."""

    get_fn: Callable[[DeferrableRuntime], float]
    set_fn: Callable[[DeferrableRuntime, float], None]

    step_is_time_step: bool = False
    """Take the step from the configured optimisation timestep instead of
    ``native_step``, for a value EMHASS can only honour in whole timesteps."""

    available_fn: Callable[[DeferrableRuntime], bool] | None = None
    """When this setting is actually consulted. A setting that another setting
    has made irrelevant reports unavailable, so the UI greys it out rather than
    accepting a value that would silently never be read."""

    applies_fn: Callable[[DeferrableRuntime], bool] | None = None
    """Whether this load has the setting at all. Unlike ``available_fn`` --
    which reacts to live state -- this is decided by what the load *is*, fixed
    at creation, so a setting that can never apply is simply not created
    rather than permanently greyed out."""


def _set_nominal_power(load: DeferrableRuntime, value: float) -> None:
    load.nominal_power_w = value


def _set_minimum_power(load: DeferrableRuntime, value: float) -> None:
    load.minimum_power_w = value


def _set_run_within(load: DeferrableRuntime, value: float) -> None:
    load.run_within_hours = value


def _set_operating_hours(load: DeferrableRuntime, value: float) -> None:
    load.operating_hours = value


def _set_energy_needed(load: DeferrableRuntime, value: float) -> None:
    load.energy_needed_kwh = value


def _set_surplus_headroom(load: DeferrableRuntime, value: float) -> None:
    load.surplus_headroom_w = value


def _set_surplus_priority(load: DeferrableRuntime, value: float) -> None:
    load.surplus_priority = int(value)


def _set_startup_penalty(load: DeferrableRuntime, value: float) -> None:
    load.startup_penalty = value


def _set_max_startups(load: DeferrableRuntime, value: float) -> None:
    load.max_startups = int(value)


def _set_minimum_on_time(load: DeferrableRuntime, value: float) -> None:
    load.minimum_on_time_minutes = value


def _set_minimum_off_time(load: DeferrableRuntime, value: float) -> None:
    load.minimum_off_time_minutes = value


def _set_comfort_temperature(load: DeferrableRuntime, value: float) -> None:
    load.comfort_temperature = value


def _set_setback_temperature(load: DeferrableRuntime, value: float) -> None:
    load.setback_temperature = value


def _set_max_temperature(load: DeferrableRuntime, value: float) -> None:
    load.max_temperature = value


def _set_heating_rate(load: DeferrableRuntime, value: float) -> None:
    load.heating_rate = value


def _set_cooling_constant(load: DeferrableRuntime, value: float) -> None:
    load.cooling_constant = value


def _set_thermal_inertia(load: DeferrableRuntime, value: float) -> None:
    load.thermal_inertia = value


def _is_standard(load: DeferrableRuntime) -> bool:
    return not load.is_thermal


def _is_thermal(load: DeferrableRuntime) -> bool:
    return load.is_thermal


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
        key="minimum_power",
        translation_key="minimum_power",
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        native_min_value=0,
        native_max_value=100000,
        native_step=10,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.minimum_power_w,
        set_fn=_set_minimum_power,
        # Only bites for a load that may modulate; a semi-continuous load is
        # either off or at its nominal power, and the floor is never read.
        available_fn=lambda load: not load.semi_continuous,
    ),
    LoadNumberDescription(
        key="run_within",
        translation_key="run_within",
        native_unit_of_measurement=UnitOfTime.HOURS,
        native_min_value=0,
        native_max_value=48,
        native_step=0.25,
        step_is_time_step=True,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.run_within_hours,
        set_fn=_set_run_within,
        # A duration needs an instant to count from, and only a request
        # provides one. On a daily load "within 4 h" of a fixed opening is just
        # a narrower window, which Latest finish already says.
        available_fn=lambda load: load.on_demand,
        applies_fn=_is_standard,
    ),
    LoadNumberDescription(
        key="operating_hours",
        translation_key="operating_hours",
        native_unit_of_measurement=UnitOfTime.HOURS,
        native_min_value=0,
        native_max_value=24,
        native_step=0.25,
        step_is_time_step=True,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.operating_hours,
        set_fn=_set_operating_hours,
        # A thermal load has no run-time target: EMHASS derives its running
        # from the comfort band instead, and ignores operating hours entirely.
        applies_fn=_is_standard,
        # Nor does a surplus load. Its hours are whatever the exported power in
        # the last plan can feed, recomputed every run -- a number typed here
        # would simply never be read.
        available_fn=lambda load: not load.on_surplus,
    ),
    LoadNumberDescription(
        key="energy_needed",
        translation_key="energy_needed",
        device_class=NumberDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        native_min_value=0,
        native_max_value=500,
        native_step=0.5,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.energy_needed_kwh,
        set_fn=_set_energy_needed,
        applies_fn=_is_standard,
        # The one cap a surplus load takes: "put this much in and stop". Zero
        # means no cap, and the load runs on whatever is spare until its
        # request is turned off -- which is the pool's case, and the default.
        available_fn=lambda load: load.on_surplus,
    ),
    LoadNumberDescription(
        key="surplus_headroom",
        translation_key="surplus_headroom",
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        native_min_value=0,
        native_max_value=10000,
        native_step=50,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.surplus_headroom_w,
        set_fn=_set_surplus_headroom,
        applies_fn=_is_standard,
        available_fn=lambda load: load.on_surplus,
    ),
    LoadNumberDescription(
        key="surplus_priority",
        translation_key="surplus_priority",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: float(load.surplus_priority),
        set_fn=_set_surplus_priority,
        applies_fn=_is_standard,
        available_fn=lambda load: load.on_surplus,
    ),
    LoadNumberDescription(
        key="comfort_temperature",
        translation_key="comfort_temperature",
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=5,
        native_max_value=35,
        native_step=0.5,
        mode=NumberMode.BOX,
        get_fn=lambda load: load.comfort_temperature,
        set_fn=_set_comfort_temperature,
        applies_fn=_is_thermal,
    ),
    LoadNumberDescription(
        key="setback_temperature",
        translation_key="setback_temperature",
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=5,
        native_max_value=35,
        native_step=0.5,
        mode=NumberMode.BOX,
        get_fn=lambda load: load.setback_temperature,
        set_fn=_set_setback_temperature,
        applies_fn=_is_thermal,
    ),
    LoadNumberDescription(
        key="max_temperature",
        translation_key="max_temperature",
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=5,
        native_max_value=40,
        native_step=0.5,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.max_temperature,
        set_fn=_set_max_temperature,
        applies_fn=_is_thermal,
    ),
    LoadNumberDescription(
        key="heating_rate",
        translation_key="heating_rate",
        native_unit_of_measurement="°C/h",
        native_min_value=0.01,
        native_max_value=20,
        native_step=0.01,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.heating_rate,
        set_fn=_set_heating_rate,
        applies_fn=_is_thermal,
    ),
    LoadNumberDescription(
        key="cooling_constant",
        translation_key="cooling_constant",
        native_min_value=0,
        native_max_value=2,
        native_step=0.001,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.cooling_constant,
        set_fn=_set_cooling_constant,
        applies_fn=_is_thermal,
    ),
    LoadNumberDescription(
        key="thermal_inertia",
        translation_key="thermal_inertia",
        native_unit_of_measurement=UnitOfTime.HOURS,
        native_min_value=0,
        native_max_value=6,
        native_step=0.25,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.thermal_inertia,
        set_fn=_set_thermal_inertia,
        applies_fn=_is_thermal,
    ),
    LoadNumberDescription(
        key="startup_penalty",
        translation_key="startup_penalty",
        native_min_value=0,
        native_max_value=100,
        native_step=0.01,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.startup_penalty,
        set_fn=_set_startup_penalty,
    ),
    LoadNumberDescription(
        key="max_startups",
        translation_key="max_startups",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: float(load.max_startups),
        set_fn=_set_max_startups,
    ),
    LoadNumberDescription(
        key="minimum_on_time",
        translation_key="minimum_on_time",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        native_min_value=0,
        native_max_value=240,
        native_step=1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.minimum_on_time_minutes,
        set_fn=_set_minimum_on_time,
    ),
    LoadNumberDescription(
        key="minimum_off_time",
        translation_key="minimum_off_time",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        native_min_value=0,
        native_max_value=240,
        native_step=1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.minimum_off_time_minutes,
        set_fn=_set_minimum_off_time,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: EmhassCoordinator = entry.runtime_data.coordinator
    async_add_entities([SurplusThresholdNumber(coordinator), MixBetaNumber(coordinator)])
    for load in entry.runtime_data.loads.all():
        async_add_entities(
            (
                LoadNumber(coordinator, load, description)
                for description in LOAD_NUMBERS
                if description.applies_fn is None or description.applies_fn(load)
            ),
            config_subentry_id=load.subentry_id,
        )


class SurplusThresholdNumber(EmhassEntity, RestoreNumber):
    """What the hub's surplus sensors count as a surplus window.

    Reporting only. No load budgets against this: a load's own threshold is its
    power plus its own headroom, because "is there enough spare sun for the
    pool" and "...for the car" are different questions. This one exists so the
    surplus window shown on a dashboard, and triggered on by automations that
    care about the household rather than one appliance, is a number the user
    picked rather than one baked in.
    """

    _attr_translation_key = "surplus_threshold"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_min_value = 0
    _attr_native_max_value = 20000
    _attr_native_step = 50
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: EmhassCoordinator) -> None:
        super().__init__(coordinator, "surplus_threshold")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) is not None and (
            last.native_value is not None
        ):
            self.coordinator.surplus_threshold_w = last.native_value

    @property
    def native_value(self) -> float:
        return round(self.coordinator.surplus_threshold_w)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.surplus_threshold_w = value
        self.async_write_ha_state()
        # The surplus sensors read this off the coordinator, and nothing else
        # is going to tell them it moved.
        self.coordinator.async_update_listeners()


class MixBetaNumber(EmhassEntity, RestoreNumber):
    """How much an MPC run trusts the live PV/load reading over the forecast.

    One slider rather than an independent alpha/beta pair: the two always sum
    to 1 in practice (see payload.build_payload, models.Series.blend_first),
    and a pair invites confusing combinations like 0.3/0.3 that don't mean
    what they look like. 0 is pure forecast, 1 is pure live reading, 0.5 is
    EMHASS's own default split for the same correction.
    """

    _attr_translation_key = "mix_beta"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 1
    _attr_native_step = 0.05
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: EmhassCoordinator) -> None:
        super().__init__(coordinator, "mix_beta")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) is not None and (
            last.native_value is not None
        ):
            self.coordinator.mix_beta = last.native_value

    @property
    def native_value(self) -> float:
        return self.coordinator.mix_beta

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.mix_beta = value
        self.async_write_ha_state()


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
        if description.step_is_time_step:
            # EMHASS can only run a load for a whole number of timesteps, so
            # the arrows should land on values it can actually honour. The
            # payload quantises regardless (payload.operating_timesteps) --
            # this is about not inviting a value that has to be adjusted.
            self._attr_native_step = coordinator.config.time_step_minutes / 60

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # RestoreNumber, not RestoreEntity: the latter restores the formatted
        # state string rather than the native value.
        if (last := await self.async_get_last_number_data()) is not None and (
            last.native_value is not None
        ):
            self.entity_description.set_fn(self.load, last.native_value)

    @property
    def available(self) -> bool:
        # Settles on the first coordinator update after a restart: the fields
        # this reads are restored by *other* entities, which may not have been
        # added yet when this one first reports.
        if (available_fn := self.entity_description.available_fn) is None:
            return super().available
        return super().available and available_fn(self.load)

    @property
    def native_value(self) -> float:
        value = self.entity_description.get_fn(self.load)
        # A whole-number step can only land on whole numbers, so report an int.
        # `number` has no display-precision option; the state string *is* the
        # precision, and a float state stringifies as "1600.0", which the box
        # control renders through the locale ("1600,0" in Swedish).
        # (native_step, not step: the latter reaches for the unit, which is not
        # resolvable until the entity has been added to its platform.)
        step = self.native_step
        if step is not None and float(step).is_integer():
            return round(value)
        return value

    async def async_set_native_value(self, value: float) -> None:
        self.entity_description.set_fn(self.load, value)
        self.load.notify()
        await self.coordinator.async_request_refresh()
        if self.load.is_thermal:
            self._check_thermal_reachability()

    def _check_thermal_reachability(self) -> None:
        """Repair-issue warning if this load's own rates can't meet its schedule.

        Runs on every tuning change to a thermal load, since heating_rate,
        cooling_constant, the comfort/setback temperatures, and the comfort
        window are exactly the values that decide the answer. See
        ``ThermalConfig.first_unreachable_step`` for what this does and does
        not catch -- it is a cheap local hint, not a replacement for actually
        running the optimisation.
        """
        issue_id = f"{ISSUE_THERMAL_UNREACHABLE}_{self.load.subentry_id}"
        current = self.coordinator.loads.room_temperature(self.load)
        if current is None:
            # No sensor, or it is unavailable: nothing to replay from, and
            # deferrable.room_temperature already warned about this on its
            # own account.
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return

        thermal = self.load.thermal_config(current)
        result = (
            thermal.first_unreachable_step(
                current,
                dt_util.utcnow(),
                timedelta(minutes=self.coordinator.config.time_step_minutes),
                self.coordinator.config.horizon_steps,
            )
            if thermal is not None
            else None
        )

        if result is None:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return

        moment, required, achievable = result
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_THERMAL_UNREACHABLE,
            translation_placeholders={
                "name": self.load.name,
                "heating_rate": f"{self.load.heating_rate:g}",
                "current": f"{current:.1f}",
                "required": f"{required:.1f}",
                "achievable": f"{achievable:.1f}",
                "time": dt_util.as_local(moment).strftime("%H:%M"),
            },
        )

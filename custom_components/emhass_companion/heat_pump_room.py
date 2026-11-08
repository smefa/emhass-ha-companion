"""A room heated by a heat pump, sent as an EMHASS ``thermal_battery``.

The Room model (``thermal_config``) needs a measured heating rate and cooling
constant. This one describes the building instead and lets EMHASS work the
rest out: the heat pump's COP from the Carnot formula at each step's outdoor
temperature, and the house's heat demand from the degree-day model.

Everything mirrors ``_add_thermal_battery_constraints`` in EMHASS 0.18.8:

    T[i+1] = T[i] + 3600 / (density * heat_capacity * volume)
             * (COP[i] * P[i] / 1000 * dt - demand[i] - loss[i] * dt)

with ``COP = carnot * T_supply_K / (T_supply_K - T_out_K)``, clipped to 1..8
(``calculate_cop_heatpump``), ``demand = specific_heating_demand * area *
max(base - T_out, 0) * (dt / 24) / annual_reference_hdd``
(``calculate_heating_demand``), and ``loss = thermal_loss`` signed negative
when it is at least as warm outdoors as the start temperature
(``calculate_thermal_loss_signed``). ``thermal_loss`` is left to EMHASS's
default; the degree-day demand already is the house's heat loss.

Only heating: EMHASS's degree-day model is heating-only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from math import isfinite
from typing import Any

from homeassistant.util import dt as dt_util

from .const import (
    CONF_ANNUAL_HDD,
    CONF_BASE_TEMPERATURE,
    CONF_CARNOT_EFFICIENCY,
    CONF_FLOOR_AREA,
    CONF_INERTIA_TIME_CONSTANT,
    CONF_SPECIFIC_HEATING_DEMAND,
    CONF_SUPPLY_TEMPERATURE,
    CONF_THERMAL_MASS,
)
from .thermal import MAX_THERMAL_TEMPERATURE, MIN_THERMAL_TEMPERATURE, ThermalConfig
from .water_tank import CLAMP_EPSILON_K, REACH_MARGIN_K, UNREACHABLE_TOLERANCE_K, TankShortfall

# EMHASS's own defaults, used where the request leaves a key out.
CONCRETE_DENSITY_KG_M3 = 2400.0
CONCRETE_HEAT_CAPACITY_KJ_KG_K = 0.88
EMHASS_THERMAL_LOSS_KW = 0.045
EMHASS_FALLBACK_OUTDOOR = 15.0
EMHASS_DEFAULT_START = 20.0
MIN_COP = 1.0
MAX_COP = 8.0

DEFAULT_SUPPLY_TEMPERATURE = 35.0
DEFAULT_CARNOT_EFFICIENCY = 0.4
DEFAULT_BASE_TEMPERATURE = 18.0
DEFAULT_ANNUAL_HDD = 3000.0
DEFAULT_INERTIA_HOURS = 0.0
MAX_INERTIA_HOURS = 24.0


@dataclass(frozen=True, slots=True)
class HeatPumpModel:
    """The building and heat pump, as the subentry stores them."""

    thermal_mass_m3: float
    floor_area_m2: float
    specific_heating_demand: float
    """kWh per m2 and year, for a year of ``annual_reference_hdd`` degree days."""
    supply_temperature: float = DEFAULT_SUPPLY_TEMPERATURE
    carnot_efficiency: float = DEFAULT_CARNOT_EFFICIENCY
    base_temperature: float = DEFAULT_BASE_TEMPERATURE
    annual_reference_hdd: float = DEFAULT_ANNUAL_HDD
    inertia_hours: float = DEFAULT_INERTIA_HOURS

    def __post_init__(self) -> None:
        for name in (
            "thermal_mass_m3",
            "floor_area_m2",
            "specific_heating_demand",
            "supply_temperature",
            "carnot_efficiency",
            "base_temperature",
            "annual_reference_hdd",
            "inertia_hours",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        for name in ("thermal_mass_m3", "floor_area_m2", "annual_reference_hdd"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.specific_heating_demand < 0:
            raise ValueError("specific_heating_demand must be nonnegative")
        if not 0 < self.carnot_efficiency <= 1:
            raise ValueError("carnot_efficiency must be above 0 and at most 1")
        if not 0 <= self.inertia_hours <= MAX_INERTIA_HOURS:
            raise ValueError(f"inertia_hours must be between 0 and {MAX_INERTIA_HOURS:g}")

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> HeatPumpModel:
        """Read stored subentry data; raises ``ValueError`` if it is incomplete."""
        try:
            return cls(
                thermal_mass_m3=float(data[CONF_THERMAL_MASS]),
                floor_area_m2=float(data[CONF_FLOOR_AREA]),
                specific_heating_demand=float(data[CONF_SPECIFIC_HEATING_DEMAND]),
                supply_temperature=float(
                    data.get(CONF_SUPPLY_TEMPERATURE, DEFAULT_SUPPLY_TEMPERATURE)
                ),
                carnot_efficiency=float(
                    data.get(CONF_CARNOT_EFFICIENCY, DEFAULT_CARNOT_EFFICIENCY)
                ),
                base_temperature=float(data.get(CONF_BASE_TEMPERATURE, DEFAULT_BASE_TEMPERATURE)),
                annual_reference_hdd=float(data.get(CONF_ANNUAL_HDD, DEFAULT_ANNUAL_HDD)),
                inertia_hours=float(data.get(CONF_INERTIA_TIME_CONSTANT, DEFAULT_INERTIA_HOURS)),
            )
        except (KeyError, TypeError) as err:
            raise ValueError("the heat pump model settings are incomplete") from err

    @property
    def capacity_kwh_per_k(self) -> float:
        return CONCRETE_DENSITY_KG_M3 * CONCRETE_HEAT_CAPACITY_KJ_KG_K * self.thermal_mass_m3 / 3600

    def cop(self, outdoor: float) -> float:
        """``calculate_cop_heatpump`` in heating mode, for one outdoor temperature."""
        lift = self.supply_temperature - outdoor
        if lift <= 0:
            return MIN_COP
        cop = self.carnot_efficiency * (self.supply_temperature + 273.15) / lift
        return min(MAX_COP, max(MIN_COP, cop))

    def demand_keys(self) -> dict[str, float]:
        """The ``thermal_battery`` keys that carry this model's heat demand.

        TEMPORARY WORKAROUND, remove once EMHASS fixes the degree-day refresh.
        EMHASS 0.18.8 builds the degree-day demand (``specific_heating_demand``
        + ``area``) correctly on a fresh problem, but its cache-hit refresh
        (``update_thermal_params``) only knows the physics keys and otherwise
        sets ``heating_demand`` to zero. Every warm-started solve then plans a
        building with no heat loss and switches the heat pump off for the day.

        The physics model is the same straight line through ``base_temperature``,
        so it is sent instead: ``u_value * envelope_area`` is the loss in W/K
        (``ventilation_rate`` 0 drops the second term; ``heated_volume`` only has
        to be present). ``calculate_thermal_balance_physics`` floors at zero like
        ``calculate_heating_demand``, so the two agree step for step. Checked
        against EMHASS 0.18.8 (identical demand and plan, on build and refresh).

        To remove: send ``specific_heating_demand``, ``area``, ``base_temperature``
        and ``annual_reference_hdd`` again and delete this method, its test and
        the note in docs/thermal_loads.md.
        """
        loss_w_per_k = (
            self.specific_heating_demand
            * self.floor_area_m2
            / (self.annual_reference_hdd * 24)
            * 1000
        )
        return {
            "u_value": 1.0,
            "envelope_area": loss_w_per_k,
            "ventilation_rate": 0.0,
            "heated_volume": 1.0,
            "indoor_target_temperature": self.base_temperature,
        }

    def demand_kwh(self, outdoor: float, step_hours: float) -> float:
        """``calculate_heating_demand`` for one step."""
        degrees = max(self.base_temperature - outdoor, 0.0)
        return (
            self.specific_heating_demand
            * self.floor_area_m2
            * degrees
            * (step_hours / 24)
            / self.annual_reference_hdd
        )


@dataclass(slots=True)
class HeatPumpRoomConfig:
    """One heat pump room for one request."""

    model: HeatPumpModel
    nominal_power_w: float
    comfort_temperature: float
    setback_temperature: float
    max_temperature: float
    comfort_start: time
    comfort_end: time
    current_temperature: float | None = None
    outdoor: tuple[float, ...] = ()
    """Outdoor temperature at each step of the horizon, as EMHASS will see it."""

    sense = "heat"

    def __post_init__(self) -> None:
        if not isinstance(self.model, HeatPumpModel):
            raise ValueError("model must be a HeatPumpModel")
        for name in (
            "nominal_power_w",
            "comfort_temperature",
            "setback_temperature",
            "max_temperature",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        if self.nominal_power_w <= 0:
            raise ValueError("nominal_power_w must be positive")
        if not (
            MIN_THERMAL_TEMPERATURE
            <= self.setback_temperature
            <= self.comfort_temperature
            <= self.max_temperature
            <= MAX_THERMAL_TEMPERATURE
        ):
            raise ValueError(
                "require 5 <= setback_temperature <= comfort_temperature <= max_temperature <= 95"
            )
        if self.current_temperature is not None and not isfinite(self.current_temperature):
            raise ValueError("current_temperature must be a finite number")

    def with_outdoor_forecast(self, outdoor: Sequence[float]) -> HeatPumpRoomConfig:
        return replace(self, outdoor=tuple(float(value) for value in outdoor))

    def _outdoor_at(self, index: int) -> float:
        """EMHASS pads a short forecast with its 15 C fallback."""
        return self.outdoor[index] if index < len(self.outdoor) else EMHASS_FALLBACK_OUTDOOR

    @property
    def heating_rate(self) -> float:
        """Ideal degrees per hour at nominal power and the forecast's worst COP.

        Before demand and loss, like ``WaterTankConfig.heating_rate``: it only
        shapes the comfort ramp. What the room can really reach is
        :meth:`best_case`, and the floors sent are held to that.
        """
        outdoor = self.outdoor or (EMHASS_FALLBACK_OUTDOOR,)
        cop = min(self.model.cop(value) for value in outdoor)
        return cop * self.nominal_power_w / 1000 / self.model.capacity_kwh_per_k

    def comfort_band(
        self, grid_start: datetime, step: timedelta, horizon_steps: int
    ) -> tuple[list[float], list[float]]:
        """The Room model's band and ramp, at this model's heating rate."""
        return ThermalConfig(
            heating_rate=self.heating_rate,
            comfort_temperature=self.comfort_temperature,
            setback_temperature=self.setback_temperature,
            max_temperature=self.max_temperature,
            comfort_start=self.comfort_start,
            comfort_end=self.comfort_end,
        ).comfort_band(grid_start, step, horizon_steps)

    def best_case(
        self, step: timedelta, horizon_steps: int, maximums: Sequence[float]
    ) -> list[float]:
        """Warmest the room can be at each step with the heat pump flat out.

        The inertia filter starts empty; EMHASS may start it with heat a
        previous run committed, so this can only be too cold, which lowers a
        floor further than needed rather than leaving one unreachable. The
        ceiling caps the trajectory (heat the room cannot hold does not count
        towards later floors) when the room starts at or below it and nothing
        is held back in the filter. Without a reading EMHASS starts from its
        own default, so that is replayed instead.
        """
        start = self.start_temperature
        dt = step / timedelta(hours=1)
        alpha = min(dt / self.model.inertia_hours, 1.0) if self.model.inertia_hours > 0 else None
        cap = alpha is None and start <= min(maximums)
        temperature = start
        filtered = 0.0
        best = [temperature]
        for index in range(horizon_steps - 1):
            outdoor = self._outdoor_at(index)
            raw = self.model.cop(outdoor) * self.nominal_power_w / 1000 * dt
            if alpha is None:
                heat = raw
            else:
                heat = filtered
                filtered += alpha * (raw - filtered)
            loss = EMHASS_THERMAL_LOSS_KW * (-1.0 if outdoor >= start else 1.0)
            temperature += (heat - self.model.demand_kwh(outdoor, dt) - loss * dt) / (
                self.model.capacity_kwh_per_k
            )
            if cap:
                temperature = min(temperature, maximums[index + 1])
            best.append(temperature)
        return best

    @property
    def start_temperature(self) -> float:
        """Where EMHASS starts the room: the reading, or its 20 C default."""
        if self.current_temperature is None:
            return EMHASS_DEFAULT_START
        return self.current_temperature

    def _floors(
        self, grid_start: datetime, step: timedelta, horizon_steps: int
    ) -> tuple[list[float], list[float], list[float]]:
        minimums, maximums = self.comfort_band(grid_start, step, horizon_steps)
        return minimums, maximums, self.best_case(step, horizon_steps, maximums)

    def first_unreachable_step(
        self, grid_start: datetime, step: timedelta, horizon_steps: int
    ) -> TankShortfall | None:
        """The first floor :meth:`to_emhass` has to lower, or None."""
        minimums, _, best = self._floors(grid_start, step, horizon_steps)
        for index in range(1, horizon_steps):
            if best[index] < minimums[index] - UNREACHABLE_TOLERANCE_K:
                return TankShortfall(
                    moment=dt_util.as_local(grid_start.astimezone(UTC) + step * index),
                    required=minimums[index],
                    achievable=best[index],
                    current=self.start_temperature,
                    power_w=self.nominal_power_w,
                )
        return None

    def to_emhass(
        self, grid_start: datetime, step: timedelta, horizon_steps: int
    ) -> dict[str, Any]:
        """The ``thermal_battery`` entry.

        EMHASS holds floors hard, so one the room cannot reach flat out (a cold
        start, a cold night) would leave it no plan. Like the water tank, such
        a floor is lowered to just under the best case; EMHASS then heats as
        fast as it can and the schedule catches up.
        """
        minimums, maximums, best = self._floors(grid_start, step, horizon_steps)
        minimums = [
            best[index] - REACH_MARGIN_K
            if index and floor > best[index] + CLAMP_EPSILON_K
            else floor
            for index, floor in enumerate(minimums)
        ]
        entry: dict[str, Any] = {
            "volume": self.model.thermal_mass_m3,
            "density": CONCRETE_DENSITY_KG_M3,
            "heat_capacity": CONCRETE_HEAT_CAPACITY_KJ_KG_K,
            "supply_temperature": self.model.supply_temperature,
            "carnot_efficiency": self.model.carnot_efficiency,
            **self.model.demand_keys(),
            "sense": "heat",
            "min_temperatures": minimums,
            "max_temperatures": maximums,
        }
        if self.model.inertia_hours > 0:
            entry["thermal_inertia_time_constant"] = self.model.inertia_hours
        if self.current_temperature is not None:
            entry["start_temperature"] = self.current_temperature
        return entry

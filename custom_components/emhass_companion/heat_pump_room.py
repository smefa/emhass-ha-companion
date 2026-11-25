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
from itertools import pairwise
from math import isfinite
from typing import Any

from homeassistant.util import dt as dt_util

from .const import (
    CONF_ANNUAL_HDD,
    CONF_BASE_TEMPERATURE,
    CONF_CARNOT_EFFICIENCY,
    CONF_CONSTANT_COP,
    CONF_FLOOR_AREA,
    CONF_HEATING_CURVE,
    CONF_INERTIA_TIME_CONSTANT,
    CONF_MAX_THERMAL_POWER,
    CONF_SHGC,
    CONF_SOURCE_TEMPERATURE,
    CONF_SPECIFIC_HEATING_DEMAND,
    CONF_SUPPLY_TEMPERATURE,
    CONF_THERMAL_MASS,
    CONF_WINDOW_AREA,
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
MIN_SUPPLY_TEMPERATURE = 20.0
MAX_SUPPLY_TEMPERATURE = 80.0
DEFAULT_CARNOT_EFFICIENCY = 0.4
DEFAULT_BASE_TEMPERATURE = 18.0
DEFAULT_ANNUAL_HDD = 3000.0
DEFAULT_INERTIA_HOURS = 0.0
MAX_INERTIA_HOURS = 24.0
DEFAULT_SHGC = 0.6  # EMHASS's own default

HeatingCurve = tuple[tuple[float, float], ...]
"""Outdoor temperature (°C) and supply water temperature (°C) pairs."""


def parse_heating_curve(value: str | HeatingCurve | None) -> HeatingCurve:
    """Parse ``"15: 25, 0: 35, -15: 45"`` into outdoor/supply temperature pairs.

    Blank means no curve (use the fixed ``supply_temperature``). Raises
    ``ValueError`` with a message fit to show the user.
    """
    if value is None or value == ():
        return ()
    if isinstance(value, tuple):
        points: list[tuple[float, float]] = []
        for item in value:
            if not isinstance(item, tuple | list) or len(item) != 2:
                raise ValueError("each heating curve point must be an (outdoor, supply) pair")
            outdoor, supply = item
            if (
                isinstance(outdoor, bool)
                or isinstance(supply, bool)
                or not isinstance(outdoor, int | float)
                or not isinstance(supply, int | float)
                or not (isfinite(outdoor) and isfinite(supply))
            ):
                raise ValueError("heating curve temperatures must be finite numbers")
            if not MIN_SUPPLY_TEMPERATURE <= supply <= MAX_SUPPLY_TEMPERATURE:
                raise ValueError(
                    f"supply temperature must be between "
                    f"{MIN_SUPPLY_TEMPERATURE:g} and {MAX_SUPPLY_TEMPERATURE:g} °C"
                )
            points.append((float(outdoor), float(supply)))
    else:
        text = str(value).strip()
        if not text:
            return ()
        points = []
        for part in text.replace(";", ",").replace("\n", ",").split(","):
            if not part.strip():
                continue
            outdoor_s, separator, supply_s = part.partition(":")
            try:
                if not separator:
                    raise ValueError
                point = (float(outdoor_s), float(supply_s))
            except ValueError:
                raise ValueError(f"'{part.strip()}' is not in the form 'outdoor: supply'") from None
            if not (isfinite(point[0]) and isfinite(point[1])):
                raise ValueError(f"'{part.strip()}': temperatures must be finite numbers")
            if not MIN_SUPPLY_TEMPERATURE <= point[1] <= MAX_SUPPLY_TEMPERATURE:
                raise ValueError(
                    f"'{part.strip()}': supply temperature must be between "
                    f"{MIN_SUPPLY_TEMPERATURE:g} and {MAX_SUPPLY_TEMPERATURE:g} °C"
                )
            points.append(point)
    if len(points) < 2:
        raise ValueError("give at least two points")
    outdoors = [outdoor for outdoor, _ in points]
    if len(set(outdoors)) != len(outdoors):
        raise ValueError("each outdoor temperature can only be listed once")
    return tuple(points)


def supply_temperature_at_curve(curve: HeatingCurve, outdoor: float) -> float:
    """Linear interpolation between the points; end points hold beyond them."""
    ordered = sorted(curve)
    if outdoor <= ordered[0][0]:
        return ordered[0][1]
    if outdoor >= ordered[-1][0]:
        return ordered[-1][1]
    for (t0, s0), (t1, s1) in pairwise(ordered):
        if t0 <= outdoor <= t1:
            return s0 + (s1 - s0) * (outdoor - t0) / (t1 - t0)
    return ordered[-1][1]  # pragma: no cover - bounds above cover every case


def _optional_float(value: Any) -> float | None:
    return None if value in (None, "") else float(value)


def _float_or(value: Any, default: float) -> float:
    """A stored number, or ``default`` when it is blank (0 is a real value)."""
    return default if value in (None, "") else float(value)


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
    constant_cop: float | None = None
    """A flat COP, for a heat pump whose COP barely follows the outdoor air."""
    source_temperature: float | None = None
    """Cold-side temperature in °C (brine, ground) used instead of the outdoor air for the COP."""
    window_area_m2: float | None = None
    """Effective glazing area. EMHASS adds ``area * shgc * GHI`` to the heat; unset or 0 means no solar gain."""
    shgc: float = DEFAULT_SHGC
    """Solar heat gain coefficient, 0-1. Only used with a window area."""
    max_thermal_power_w: float | None = None
    """Ceiling on delivered heat in W (thermal): ``COP * electrical power`` never exceeds it."""
    heating_curve: HeatingCurve = ()
    """Optional outdoor-to-supply curve; overrides ``supply_temperature`` per step on EMHASS 0.19+."""

    def __post_init__(self) -> None:
        if self.heating_curve:
            object.__setattr__(self, "heating_curve", parse_heating_curve(self.heating_curve))
        for name in (
            "thermal_mass_m3",
            "floor_area_m2",
            "specific_heating_demand",
            "supply_temperature",
            "carnot_efficiency",
            "base_temperature",
            "annual_reference_hdd",
            "inertia_hours",
            "shgc",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        if not 0 <= self.shgc <= 1:
            raise ValueError("shgc must be between 0 and 1")
        for name in ("thermal_mass_m3", "floor_area_m2", "annual_reference_hdd"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.specific_heating_demand < 0:
            raise ValueError("specific_heating_demand must be nonnegative")
        for name in ("constant_cop", "source_temperature", "window_area_m2", "max_thermal_power_w"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value)
            ):
                raise ValueError(f"{name} must be a finite number")
        if self.window_area_m2 is not None and self.window_area_m2 < 0:
            raise ValueError("window_area_m2 must be nonnegative")
        if self.max_thermal_power_w is not None and self.max_thermal_power_w <= 0:
            raise ValueError("max_thermal_power_w must be positive")
        if self.constant_cop is not None and not MIN_COP <= self.constant_cop <= MAX_COP:
            raise ValueError(f"constant_cop must be between {MIN_COP:g} and {MAX_COP:g}")
        if (
            self.source_temperature is not None
            and self.source_temperature >= self.supply_temperature
        ):
            raise ValueError("source_temperature must be below supply_temperature")
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
                constant_cop=_optional_float(data.get(CONF_CONSTANT_COP)),
                source_temperature=_optional_float(data.get(CONF_SOURCE_TEMPERATURE)),
                window_area_m2=_optional_float(data.get(CONF_WINDOW_AREA)),
                shgc=_float_or(data.get(CONF_SHGC), DEFAULT_SHGC),
                max_thermal_power_w=_optional_float(data.get(CONF_MAX_THERMAL_POWER)),
                heating_curve=parse_heating_curve(data.get(CONF_HEATING_CURVE)),
            )
        except (KeyError, TypeError) as err:
            raise ValueError("the heat pump model settings are incomplete") from err

    @property
    def capacity_kwh_per_k(self) -> float:
        return CONCRETE_DENSITY_KG_M3 * CONCRETE_HEAT_CAPACITY_KJ_KG_K * self.thermal_mass_m3 / 3600

    def fixed_cop(self) -> float | None:
        """The COP EMHASS is given as ``efficiency``, or None to let it use the outdoor air.

        A constant COP wins. Otherwise a source temperature gives one Carnot COP
        that does not change over the horizon, which ``efficiency`` can carry.
        """
        if self.constant_cop is not None:
            return self.constant_cop
        if self.source_temperature is not None:
            return self._carnot_cop(self.source_temperature)
        return None

    def supply_temperature_at(self, outdoor: float) -> float:
        """Supply water temperature in °C at one outdoor temperature."""
        if not self.heating_curve:
            return self.supply_temperature
        return supply_temperature_at_curve(self.heating_curve, outdoor)

    def cop(self, outdoor: float) -> float:
        """The COP EMHASS will use, for one outdoor temperature."""
        fixed = self.fixed_cop()
        if fixed is not None:
            return fixed
        return self._carnot_cop(outdoor, self.supply_temperature_at(outdoor))

    def min_capped_electrical_power_w(
        self,
        nominal_power_w: float,
        minimum_power_w: float = 0.0,
        *,
        max_cop: float | None = None,
    ) -> float:
        """Lowest electrical power EMHASS can schedule when ON under ``max_thermal_power_w``.

        Used by the running-threshold check so a cap that throttles the ON level
        below 10% of nominal power is still recognised as running, without
        dropping below ``minimum_power_w`` (which EMHASS will never schedule under).
        ``max_cop`` should be the highest COP actually forecast over the horizon;
        when absent, falls back to the COP at ``base_temperature``.
        """
        if self.max_thermal_power_w is None:
            return nominal_power_w
        fixed = self.fixed_cop()
        if fixed is not None:
            peak_cop = fixed
        elif max_cop is not None:
            peak_cop = min(MAX_COP, max(MIN_COP, max_cop))
        else:
            peak_cop = self.cop(self.base_temperature)
        capped = min(nominal_power_w, self.max_thermal_power_w / peak_cop)
        return max(capped, min(minimum_power_w, nominal_power_w))

    def _carnot_cop(self, cold_side: float, supply_temperature: float | None = None) -> float:
        """``calculate_cop_heatpump`` in heating mode, for one cold-side temperature."""
        supply = self.supply_temperature if supply_temperature is None else supply_temperature
        lift = supply - cold_side
        if lift <= 0:
            return MIN_COP
        cop = self.carnot_efficiency * (supply + 273.15) / lift
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
    minimum_power_w: float = 0.0
    """Electrical minimum EMHASS is sent; a capped step that cannot reach it gives no heat."""
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

    def without_max_thermal_power(self) -> HeatPumpRoomConfig:
        """Return a copy with ``max_thermal_power_w`` cleared (for EMHASS < 0.19.0)."""
        if self.model.max_thermal_power_w is None:
            return self
        return replace(self, model=replace(self.model, max_thermal_power_w=None))

    def without_heating_curve(self) -> HeatPumpRoomConfig:
        """Return a copy with ``heating_curve`` cleared (for EMHASS < 0.19.0)."""
        if not self.model.heating_curve:
            return self
        return replace(self, model=replace(self.model, heating_curve=()))

    def _outdoor_at(self, index: int) -> float:
        """EMHASS pads a short forecast with its 15 C fallback."""
        return self.outdoor[index] if index < len(self.outdoor) else EMHASS_FALLBACK_OUTDOOR

    def electrical_power_w(self, outdoor: float) -> float:
        """Electrical power the heat pump can really run at, for one outdoor temperature.

        Mirrors EMHASS's ``max_thermal_power`` handling: the ON level drops to
        ``cap / COP`` when that is below nominal, and a step where it falls under
        the minimum power cannot run at all.
        """
        cap = self.model.max_thermal_power_w
        if cap is None:
            return self.nominal_power_w
        power = min(self.nominal_power_w, cap / self.model.cop(outdoor))
        return 0.0 if power < min(self.minimum_power_w, self.nominal_power_w) else power

    @property
    def heating_rate(self) -> float:
        """Ideal degrees per hour at nominal power and the forecast's worst COP.

        Before demand and loss, like ``WaterTankConfig.heating_rate``: it only
        shapes the comfort ramp. What the room can really reach is
        :meth:`best_case`, and the floors sent are held to that.
        """
        outdoor = self.outdoor or (EMHASS_FALLBACK_OUTDOOR,)
        runnable_heat_kw = [
            self.model.cop(value) * power / 1000
            for value in outdoor
            if (power := self.electrical_power_w(value)) > 0
        ]
        heat_kw = min(runnable_heat_kw, default=0.0)
        return heat_kw / self.model.capacity_kwh_per_k

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
            raw = self.model.cop(outdoor) * self.electrical_power_w(outdoor) / 1000 * dt
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
        fixed_cop = self.model.fixed_cop()
        supply_temperature: float | list[float] = (
            [
                round(self.model.supply_temperature_at(self._outdoor_at(index)), 2)
                for index in range(horizon_steps)
            ]
            if self.model.heating_curve and fixed_cop is None
            else self.model.supply_temperature
        )
        entry: dict[str, Any] = {
            "volume": self.model.thermal_mass_m3,
            "density": CONCRETE_DENSITY_KG_M3,
            "heat_capacity": CONCRETE_HEAT_CAPACITY_KJ_KG_K,
            "supply_temperature": supply_temperature,
            "carnot_efficiency": self.model.carnot_efficiency,
            **self.model.demand_keys(),
            "sense": "heat",
            "min_temperatures": minimums,
            "max_temperatures": maximums,
        }
        if self.model.window_area_m2:
            # EMHASS subtracts window_area * shgc * GHI from the heat demand. The
            # replay above ignores it, so the lowered floors stay conservative.
            entry["window_area"] = self.model.window_area_m2
            entry["shgc"] = self.model.shgc
        if self.model.max_thermal_power_w is not None:
            # Needs EMHASS 0.19.0+ (emhass#1369); omitted for older backends.
            entry["max_thermal_power"] = self.model.max_thermal_power_w
        if self.model.inertia_hours > 0:
            entry["thermal_inertia_time_constant"] = self.model.inertia_hours
        if fixed_cop is not None:
            # EMHASS then uses this as the COP and ignores the outdoor air for it;
            # the outdoor forecast still drives the heat demand.
            entry["efficiency"] = fixed_cop
        if self.current_temperature is not None:
            entry["start_temperature"] = self.current_temperature
        return entry

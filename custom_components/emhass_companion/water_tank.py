"""Pure tank physics and daily draw-off scheduling for EMHASS v0.18.8.

Upstream optimization.py _add_thermal_battery_constraints reads volume (m3),
density (kg/m3), heat_capacity (kJ/(kg K)), thermal_loss (kW), and
draw_off_demand (kWh/step). The field is NOT specific_heat_capacity.

``efficiency`` is the heater's COP: heat out per unit of electricity in. EMHASS
only requires it to be positive, so a heat-pump water heater passes its
seasonal COP (above 1) there. ``desired_temperatures`` prices a shortfall below
the target instead of forbidding it, but only together with
``overshoot_temperature`` (``_add_thermal_battery_constraints``); the floor
(``min_temperatures``) stays hard either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from math import isfinite
from typing import Any

from homeassistant.util import dt as dt_util

from .const import CONF_DRAW_END, CONF_DRAW_ENERGY, CONF_DRAW_START, CONF_NAME
from .infeasibility import best_case_temperatures
from .thermal import MAX_THERMAL_TEMPERATURE, MIN_THERMAL_TEMPERATURE

MAX_HOT_WATER_PERIODS = 8

# A floor this far above the flat-out trajectory is reported as unreachable (the
# same tolerance as infeasibility.check_thermal_unreachable). Any floor above the
# trajectory by more than float noise is lowered, because EMHASS holds it hard:
# to REACH_MARGIN_K under the trajectory so the solver has some tolerance to
# work with. A floor at the trajectory (a schedule built to just fit) is left
# exactly as scheduled.
UNREACHABLE_TOLERANCE_K = 0.01
CLAMP_EPSILON_K = 1e-6
REACH_MARGIN_K = 0.02

MAX_HEATER_COP = 10.0
MAX_COMFORT_PENALTY = 1000.0

WATER_DENSITY_KG_M3 = 997.0
WATER_HEAT_CAPACITY_KJ_KG_K = 4.184


@dataclass(frozen=True, slots=True)
class HotWaterPeriod:
    """One daily availability and estimated consumption period."""

    start: time
    end: time
    energy_kwh: float
    name: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        for value in (self.start, self.end):
            if not isinstance(value, time) or value.tzinfo is not None:
                raise ValueError("period endpoints must be naive local times")
        if (
            isinstance(self.energy_kwh, bool)
            or not isinstance(self.energy_kwh, int | float)
            or not isfinite(self.energy_kwh)
            or self.energy_kwh < 0
        ):
            raise ValueError("period energy must be a finite nonnegative number")
        if not isinstance(self.name, str):
            raise ValueError("period name must be text")
        if not isinstance(self.enabled, bool):
            raise ValueError("period enabled must be boolean")


def hot_water_periods_from_data(data: Any) -> tuple[HotWaterPeriod, ...]:
    """Validate stored periods at the model boundary."""
    if not isinstance(data, list) or not 1 <= len(data) <= MAX_HOT_WATER_PERIODS:
        raise ValueError(f"require 1 to {MAX_HOT_WATER_PERIODS} hot-water periods")
    periods = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("invalid hot-water period")
        try:
            start = time.fromisoformat(item[CONF_DRAW_START])
            end = time.fromisoformat(item[CONF_DRAW_END])
            energy = item[CONF_DRAW_ENERGY]
        except (KeyError, TypeError, ValueError) as err:
            raise ValueError("invalid hot-water period") from err
        periods.append(
            HotWaterPeriod(start, end, energy, item.get(CONF_NAME, ""), item.get("enabled", True))
        )
    return tuple(periods)


@dataclass(frozen=True, slots=True)
class TankShortfall:
    """The first moment a tank cannot reach the floor its schedule asks for."""

    moment: datetime
    required: float
    achievable: float
    current: float
    power_w: float


@dataclass(slots=True)
class WaterTankConfig:
    """One immersion tank; all targets, timings and daily energy are explicit.

    Current temperature may be outside the target band (0..100 C). Targets
    use shared 5..95 C input limits, not defaults. Zero draw removes expected
    withdrawal, not availability targets.

    ``nominal_power_w`` is electrical input; ``heater_cop`` turns it into heat
    (1 for an immersion element). ``comfort_penalty`` 0 keeps the required
    temperature a hard floor; above 0 only the lowest standby temperature is
    hard and a shortfall below the required temperature costs that much per
    degree and timestep.
    """

    current_temperature: float
    volume_litres: float
    nominal_power_w: float
    standby_loss_w: float
    comfort_temperature: float
    setback_temperature: float
    max_temperature: float
    comfort_start: time
    comfort_end: time
    draw_start: time | None
    draw_end: time | None
    draw_energy_kwh: float
    periods: tuple[HotWaterPeriod, ...] | None = None
    heater_cop: float = 1.0
    comfort_penalty: float = 0.0

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        for name in (
            "current_temperature",
            "volume_litres",
            "nominal_power_w",
            "standby_loss_w",
            "comfort_temperature",
            "setback_temperature",
            "max_temperature",
            "draw_energy_kwh",
            "heater_cop",
            "comfort_penalty",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        for name in ("volume_litres", "nominal_power_w"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("standby_loss_w", "draw_energy_kwh", "comfort_penalty"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if not 0 < self.heater_cop <= MAX_HEATER_COP:
            raise ValueError(f"heater_cop must be above 0 and at most {MAX_HEATER_COP:g}")
        if self.comfort_penalty > MAX_COMFORT_PENALTY:
            raise ValueError(f"comfort_penalty must be at most {MAX_COMFORT_PENALTY:g}")
        if not 0 <= self.current_temperature <= 100:
            raise ValueError("current_temperature must be between 0 and 100 C")
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
        if self.periods is not None:
            if (
                not isinstance(self.periods, tuple)
                or not 1 <= len(self.periods) <= MAX_HOT_WATER_PERIODS
                or not all(isinstance(period, HotWaterPeriod) for period in self.periods)
            ):
                raise ValueError("invalid hot-water periods")
            return
        for name in ("comfort_start", "comfort_end", "draw_start", "draw_end"):
            value = getattr(self, name)
            if not isinstance(value, time) or value.tzinfo is not None:
                raise ValueError(f"{name} must be a naive local time")

    @property
    def heating_rate(self) -> float:
        """Ideal degrees/hour at nominal power, before loss and draw-off."""
        capacity_kwh_per_degree = (
            self.volume_litres / 1000 * WATER_DENSITY_KG_M3 * WATER_HEAT_CAPACITY_KJ_KG_K / 3600
        )
        return self.nominal_power_w * self.heater_cop / 1000 / capacity_kwh_per_degree

    def comfort_band(
        self, grid_start: datetime, step: timedelta, horizon_steps: int
    ) -> tuple[list[float], list[float]]:
        """Apply target windows and the heating ramp on actual elapsed UTC time.

        The ramp uses ideal tank heating capability, not a fitted room rate.
        It is a target schedule, not a feasibility guarantee under demand.
        """
        self._validate()
        start = _grid_start_utc(grid_start, step, horizon_steps)
        timezone = dt_util.DEFAULT_TIME_ZONE
        windows = (
            [(period.start, period.end) for period in self.periods if period.enabled]
            if self.periods is not None
            else [(self.comfort_start, self.comfort_end)]
        )
        minimums = []
        for index in range(horizon_steps):
            instant = start + index * step
            day = instant.astimezone(timezone).date()
            minimum = self.setback_temperature
            for window_from, window_until in windows:
                inside = window_from == window_until
                for window_day in (day - timedelta(days=1), day):
                    end_day = window_day + timedelta(days=int(window_until <= window_from))
                    window_start = _resolve_endpoint(
                        datetime.combine(window_day, window_from), timezone
                    )
                    window_end = _resolve_endpoint(
                        datetime.combine(end_day, window_until), timezone
                    )
                    inside = inside or window_start <= instant < window_end
                if inside:
                    minimum = self.comfort_temperature
                    break
                next_start = _resolve_endpoint(datetime.combine(day, window_from), timezone)
                if next_start <= instant:
                    next_start = _resolve_endpoint(
                        datetime.combine(day + timedelta(days=1), window_from), timezone
                    )
                remaining = (next_start - instant) / timedelta(hours=1)
                minimum = max(minimum, self.comfort_temperature - self.heating_rate * remaining)
            minimums.append(minimum)
        return minimums, [self.max_temperature] * horizon_steps

    def draw_off_demand(
        self, grid_start: datetime, step: timedelta, horizon_steps: int
    ) -> list[float]:
        """Allocate daily kWh by UTC overlap, never list tiling.

        Windows belong to their local start date; equal endpoints mean a full
        local day. Ambiguous endpoints use the first occurrence. Nonexistent
        endpoints advance to the first valid wall time (not by a fixed hour).
        Daily energy is spread over the actual elapsed window, including DST.
        If both endpoints collapse onto one instant in a missing hour, energy
        is withdrawn in the interval containing that instant. Partial horizons
        receive only the energy overlapping their half-open intervals.
        """
        self._validate()
        start = _grid_start_utc(grid_start, step, horizon_steps)
        demand = [0.0] * horizon_steps
        periods = (
            self.periods
            if self.periods is not None
            else (HotWaterPeriod(self.draw_start, self.draw_end, self.draw_energy_kwh),)
        )
        end = start + step * horizon_steps
        timezone = dt_util.DEFAULT_TIME_ZONE
        last_day = end.astimezone(timezone).date()
        for period in periods:
            if not period.enabled or period.energy_kwh == 0:
                continue
            day = start.astimezone(timezone).date() - timedelta(days=1)
            while day <= last_day:
                end_day = day + timedelta(days=int(period.end <= period.start))
                window_start = _resolve_endpoint(datetime.combine(day, period.start), timezone)
                window_end = _resolve_endpoint(datetime.combine(end_day, period.end), timezone)
                duration = window_end - window_start
                if duration == timedelta(0):
                    if start <= window_start < end:
                        demand[(window_start - start) // step] += period.energy_kwh
                elif duration > timedelta(0):
                    for index in range(horizon_steps):
                        interval_start = start + index * step
                        overlap = min(interval_start + step, window_end) - max(
                            interval_start, window_start
                        )
                        if overlap > timedelta(0):
                            demand[index] += period.energy_kwh * (overlap / duration)
                day += timedelta(days=1)
        return demand

    def _thermal_battery(
        self, grid_start: datetime, step: timedelta, horizon_steps: int
    ) -> tuple[dict[str, Any], list[float]]:
        """The tank entry, and the temperature schedule it was asked to meet.

        With no comfort penalty the schedule is the hard floor. With one, only
        the lowest standby temperature is hard and the schedule goes in as
        ``desired_temperatures``, whose shortfall EMHASS prices instead.
        """
        minimums, maximums = self.comfort_band(grid_start, step, horizon_steps)
        entry: dict[str, Any] = {
            "volume": self.volume_litres / 1000,
            "density": WATER_DENSITY_KG_M3,
            "heat_capacity": WATER_HEAT_CAPACITY_KJ_KG_K,
            "thermal_loss": self.standby_loss_w / 1000,
            "efficiency": self.heater_cop,
            "sense": "heat",
            "start_temperature": self.current_temperature,
            "min_temperatures": minimums,
            "max_temperatures": maximums,
            "draw_off_demand": self.draw_off_demand(grid_start, step, horizon_steps),
        }
        if self.comfort_penalty > 0:
            entry["min_temperatures"] = [self.setback_temperature] * horizon_steps
            entry["desired_temperatures"] = minimums
            # Without an overshoot threshold EMHASS ignores the target. The
            # ceiling is already hard, so this adds nothing beyond enabling it.
            entry["overshoot_temperature"] = self.max_temperature
            entry["penalty_factor"] = self.comfort_penalty
        return entry, minimums

    def first_unreachable_step(
        self, grid_start: datetime, step: timedelta, horizon_steps: int
    ) -> TankShortfall | None:
        """The first timestep whose floor the heater cannot reach, even flat out.

        Same bound as ``infeasibility.check_thermal_unreachable`` -- heater at
        nominal power at every timestep, with standby loss and draw-off -- so
        it flags exactly the floors :meth:`to_emhass` has to lower. With a
        comfort penalty only the setback is a floor: a comfort target the heater
        cannot reach in time just costs penalty, so it is not reported.
        """
        entry, _ = self._thermal_battery(grid_start, step, horizon_steps)
        minimums = entry["min_temperatures"]
        best = best_case_temperatures(
            entry, self.nominal_power_w, step / timedelta(minutes=1), horizon_steps
        )
        if best is None:
            return None
        for index in range(1, horizon_steps):
            if best[index] < minimums[index] - UNREACHABLE_TOLERANCE_K:
                return TankShortfall(
                    moment=dt_util.as_local(grid_start.astimezone(UTC) + step * index),
                    required=minimums[index],
                    achievable=best[index],
                    current=self.current_temperature,
                    power_w=self.nominal_power_w,
                )
        return None

    def to_emhass(
        self, grid_start: datetime, step: timedelta, horizon_steps: int
    ) -> dict[str, Any]:
        """Return the contents of one def_load_config thermal_battery entry.

        EMHASS holds the floor as a hard constraint and relaxes nothing on that
        side, so a floor the heater cannot reach in time (a tank that has gone
        cold, a comfort window starting soon) leaves it no plan at all -- and
        with no plan applied the tank never heats, so it stays unreachable.
        Each such floor is lowered to just under what the heater can reach flat
        out (and the tank can hold, below its ceiling). EMHASS then heats as
        fast as it can and the schedule catches up on its own;
        :meth:`first_unreachable_step` reports that it happened. With a comfort
        penalty the hard floor is only the standby temperature, so this seldom
        applies; the required temperature is a priced target and is not lowered.
        """
        entry, _ = self._thermal_battery(grid_start, step, horizon_steps)
        best = best_case_temperatures(
            entry, self.nominal_power_w, step / timedelta(minutes=1), horizon_steps
        )
        if best is not None:
            entry["min_temperatures"] = [
                best[index] - REACH_MARGIN_K
                if index and floor > best[index] + CLAMP_EPSILON_K
                else floor
                for index, floor in enumerate(entry["min_temperatures"])
            ]
        return entry


def _grid_start_utc(grid_start: datetime, step: timedelta, horizon_steps: int) -> datetime:
    """Reject empty/invalid grids rather than selecting upstream building mode."""
    if not isinstance(grid_start, datetime) or grid_start.utcoffset() is None:
        raise ValueError("grid_start must be a timezone-aware datetime")
    if not isinstance(step, timedelta) or step <= timedelta(0):
        raise ValueError("step must be a positive timedelta")
    if isinstance(horizon_steps, bool) or not isinstance(horizon_steps, int) or horizon_steps <= 0:
        raise ValueError("horizon_steps must be a positive integer")
    return grid_start.astimezone(UTC)


def _valid_instants(wall_time: datetime, timezone: tzinfo) -> list[datetime]:
    """Round trips distinguish valid local instants from imaginary wall times."""
    candidates = [wall_time.replace(tzinfo=timezone, fold=fold).astimezone(UTC) for fold in (0, 1)]
    return [
        instant
        for instant in candidates
        if instant.astimezone(timezone).replace(tzinfo=None) == wall_time
    ]


def _resolve_endpoint(wall_time: datetime, timezone: tzinfo) -> datetime:
    """First fold, or the exact first valid wall instant after a gap."""
    valid = _valid_instants(wall_time, timezone)
    if valid:
        return min(valid)
    projections = [
        wall_time.replace(tzinfo=timezone, fold=fold)
        .astimezone(UTC)
        .astimezone(timezone)
        .replace(tzinfo=None)
        for fold in (0, 1)
    ]
    upper = min(moment for moment in projections if moment > wall_time)
    lower = wall_time
    while upper - lower > timedelta(microseconds=1):
        middle = lower + (upper - lower) // 2
        if _valid_instants(middle, timezone):
            upper = middle
        else:
            lower = middle
    return min(_valid_instants(upper, timezone))

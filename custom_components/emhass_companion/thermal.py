"""Thermal deferrable loads.

A thermal load is one whose *temperature* the optimiser controls rather than
its run time: a heat pump, an immersion heater, an air conditioner. Instead of
"run for three hours", the constraint is "keep this room between 18 and 24
degrees, and at least 21 while we are awake" -- and the optimiser picks the
cheapest way to satisfy it.

EMHASS expresses that as ``min_temperatures`` and ``max_temperatures``: lists
with one value per timestep of the horizon. Users do not think in timesteps,
so this module turns a comfort temperature, a setback temperature and a
wall-clock comfort window into those lists.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta, tzinfo
from itertools import pairwise
from math import isfinite
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .heat_pump_room import HeatPumpRoomConfig
    from .water_tank import WaterTankConfig

from homeassistant.util import dt as dt_util

SENSE_HEAT = "heat"
SENSE_COOL = "cool"
SENSES = (SENSE_HEAT, SENSE_COOL)

# Shared UI bounds for room and tank targets; these are input limits, not defaults.
MIN_THERMAL_TEMPERATURE = 5.0
MAX_THERMAL_TEMPERATURE = 95.0

# Defaults chosen to be safe rather than efficient: a narrow band and a modest
# heating rate produce a conservative plan until the user tunes them.
DEFAULT_HEATING_RATE = 3.0
DEFAULT_COOLING_CONSTANT = 0.1
DEFAULT_THERMAL_INERTIA = 0.0
DEFAULT_COMFORT_TEMPERATURE = 21.0
DEFAULT_SETBACK_TEMPERATURE = 18.0
DEFAULT_MAX_TEMPERATURE = 24.0
DEFAULT_COMFORT_START = time(6, 30)
DEFAULT_COMFORT_END = time(23, 0)


CopCurve = tuple[tuple[float, float], ...]
"""Outdoor temperature (deg C) and COP pairs; the first pair is the reference."""

MAX_COP = 12.0


def parse_cop_curve(text: str | None) -> CopCurve:
    """Parse ``"7: 5.2, 0: 4.0, -10: 3.0"`` into temperature/COP pairs.

    Blank means no curve. The first pair is the point the user's heating rate
    was measured at, so order is kept as typed. Raises ``ValueError`` with a
    message fit to show the user.
    """
    if text is None or not str(text).strip():
        return ()
    points: list[tuple[float, float]] = []
    for part in str(text).replace(";", ",").replace("\n", ",").split(","):
        if not part.strip():
            continue
        temperature, separator, cop = part.partition(":")
        try:
            if not separator:
                raise ValueError
            point = (float(temperature), float(cop))
        except ValueError:
            raise ValueError(f"'{part.strip()}' is not in the form 'temperature: COP'") from None
        if not (isfinite(point[0]) and isfinite(point[1])) or not 0 < point[1] <= MAX_COP:
            raise ValueError(f"'{part.strip()}': the COP must be between 0 and {MAX_COP:g}")
        points.append(point)
    if len(points) < 2:
        raise ValueError("give at least two points")
    temperatures = [temperature for temperature, _ in points]
    if len(set(temperatures)) != len(temperatures):
        raise ValueError("each outdoor temperature can only be listed once")
    return tuple(points)


def format_cop_curve(curve: CopCurve) -> str:
    return ", ".join(f"{temperature:g}: {cop:g}" for temperature, cop in curve)


def cop_at(curve: CopCurve, temperature: float) -> float:
    """Linear interpolation between the points; the end points hold beyond them."""
    ordered = sorted(curve)
    if temperature <= ordered[0][0]:
        return ordered[0][1]
    if temperature >= ordered[-1][0]:
        return ordered[-1][1]
    for (t0, c0), (t1, c1) in pairwise(ordered):
        if t0 <= temperature <= t1:
            return c0 + (c1 - c0) * (temperature - t0) / (t1 - t0)
    return ordered[-1][1]  # pragma: no cover - the bounds above cover every case


def cop_rate_factor(curve: CopCurve, outdoor: Iterable[float]) -> float:
    """How much of the reference heating rate survives the coldest forecast hour.

    The rate is one number per solve, so it is set by the worst COP the horizon
    reaches (the minimum of the interpolated curve, which also holds for a
    curve that is not monotonic). Never above 1: a mild forecast does not make
    the load faster than the user's own measurement.
    """
    coldest = min((cop_at(curve, temperature) for temperature in outdoor), default=None)
    if coldest is None:
        raise ValueError("no outdoor temperature forecast")
    return min(1.0, coldest / curve[0][1])


@dataclass(slots=True)
class ThermalConfig:
    """How one thermal load behaves, and the comfort it must maintain."""

    sense: str = SENSE_HEAT
    heating_rate: float = DEFAULT_HEATING_RATE
    """Degrees per hour at the load's nominal power."""

    cooling_constant: float = DEFAULT_COOLING_CONSTANT
    """Degrees lost per hour per degree of difference to outdoors."""

    thermal_inertia: float = DEFAULT_THERMAL_INERTIA
    """Hours of lag between switching on and the temperature responding."""

    comfort_temperature: float = DEFAULT_COMFORT_TEMPERATURE
    setback_temperature: float = DEFAULT_SETBACK_TEMPERATURE
    max_temperature: float = DEFAULT_MAX_TEMPERATURE
    comfort_start: time = DEFAULT_COMFORT_START
    comfort_end: time = DEFAULT_COMFORT_END

    current_temperature: float | None = None
    """Read live each run, so the plan starts from the actual room."""

    cop_curve: CopCurve = ()
    """Heat pump COP against outdoor temperature; empty leaves the rate alone."""

    clamp_floors: bool = False
    """Lower floors the load cannot reach from ``current_temperature`` in time."""

    def with_outdoor_forecast(self, outdoor: Iterable[float]) -> ThermalConfig:
        """This load with its heating rate scaled for the forecast's coldest hour.

        Shared by the request and every display of it, so the comfort ramp, the
        reachability check and the floors EMHASS receives all use one rate.
        """
        if not self.cop_curve or self.sense != SENSE_HEAT:
            return self
        factor = cop_rate_factor(self.cop_curve, outdoor)
        return replace(self, heating_rate=self.heating_rate * factor, clamp_floors=factor < 1)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ThermalConfig:
        data = data or {}
        return cls(
            sense=data.get("sense", SENSE_HEAT),
            heating_rate=float(data.get("heating_rate", DEFAULT_HEATING_RATE)),
            cooling_constant=float(data.get("cooling_constant", DEFAULT_COOLING_CONSTANT)),
            thermal_inertia=float(data.get("thermal_inertia", DEFAULT_THERMAL_INERTIA)),
            comfort_temperature=float(data.get("comfort_temperature", DEFAULT_COMFORT_TEMPERATURE)),
            setback_temperature=float(data.get("setback_temperature", DEFAULT_SETBACK_TEMPERATURE)),
            max_temperature=float(data.get("max_temperature", DEFAULT_MAX_TEMPERATURE)),
            comfort_start=_parse_time(data.get("comfort_start"), DEFAULT_COMFORT_START),
            comfort_end=_parse_time(data.get("comfort_end"), DEFAULT_COMFORT_END),
        )

    # -- band -----------------------------------------------------------------

    def comfort_band(
        self, grid_start: datetime, step: timedelta, horizon_steps: int
    ) -> tuple[list[float], list[float]]:
        """Build the per-timestep ``(min_temperatures, max_temperatures)``.

        Timestep *i* is the interval starting ``i * step`` of real time after
        ``grid_start`` -- the same elapsed-time grid as every forecast row --
        and is then read in local wall-clock time, because that is what a
        comfort schedule means to the person who set it. Stepping in wall time
        instead would shift the whole schedule by an hour after a DST change.

        Outside the comfort window the floor ramps linearly from
        ``setback_temperature`` up to ``comfort_temperature`` over the last
        stretch before ``comfort_start``, sized to what ``heating_rate`` can
        actually deliver: ``|comfort - setback| / heating_rate`` hours. Without
        this, a plain step at ``comfort_start`` bets that the load always has
        enough lead time to close whatever gap it woke up with -- a bet a
        colder-than-usual morning loses, making the whole optimisation
        infeasible even though the load is correctly sized. Ramping over the
        same distance the load can be relied on to cover keeps the
        requirement physically reachable regardless of the starting
        temperature.

        Both lists are exactly ``horizon_steps`` long. EMHASS requires at least
        that many; sending more would be silently truncated, and sending fewer
        is an error there rather than a clamp.
        """
        timezone = dt_util.as_local(grid_start).tzinfo
        origin = grid_start.astimezone(UTC)
        ramp_duration = (
            timedelta(
                hours=abs(self.comfort_temperature - self.setback_temperature) / self.heating_rate
            )
            if self.heating_rate > 0
            else timedelta(0)
        )
        minimums: list[float] = []

        for index in range(horizon_steps):
            instant = origin + step * index
            moment_dt = instant.astimezone(timezone)
            inside = _in_comfort(moment_dt.time(), self.comfort_start, self.comfort_end)
            if inside:
                minimums.append(self.comfort_temperature)
                continue

            # Both in UTC, so the ramp measures real time to the comfort start
            # (a start that does not exist on a spring-forward night resolves
            # to the first valid instant after it, as for water tanks). A start
            # in the repeated autumn hour happens twice; the comfort window
            # opens again at the second, so the ramp aims at whichever is next.
            day = moment_dt.date()
            next_start = min(
                (
                    start
                    for offset in (0, 1)
                    for start in _comfort_starts(
                        datetime.combine(day + timedelta(days=offset), self.comfort_start),
                        timezone,
                    )
                    if start > instant
                ),
            )
            time_until_start = next_start - instant

            if ramp_duration > timedelta(0) and time_until_start <= ramp_duration:
                fraction = 1 - (time_until_start / ramp_duration)
                minimums.append(
                    self.setback_temperature
                    + fraction * (self.comfort_temperature - self.setback_temperature)
                )
            else:
                minimums.append(self.setback_temperature)

        maximums = [self.max_temperature] * horizon_steps
        return minimums, maximums

    def first_unreachable_step(
        self, current_temperature: float, grid_start: datetime, step: timedelta, horizon_steps: int
    ) -> tuple[datetime, float, float] | None:
        """The first future moment this comfort schedule cannot possibly reach.

        A cheap local sanity check, not a substitute for EMHASS's own solve.
        It assumes the load runs flat out for the whole horizon and ignores
        heat loss to the outdoors entirely, so it can only *under*-warn: if
        this reports a gap, EMHASS's real solve -- which also fights heat
        loss, other loads, and grid limits -- cannot do any better either. If
        this reports nothing, EMHASS may still find the full problem
        infeasible for reasons this never looks at (issue investigated
        2026-08-01: a comfort target unreachable from the load's own rates was
        the actual cause of a real infeasible day-ahead run).

        Mirrors only the gain term of EMHASS's thermal model (see
        docs/thermal_model.md and optimization.py's
        ``_add_thermal_load_constraints``): at most ``heating_rate`` degrees
        per hour, for however many hours are left until each timestep. If
        EMHASS's formula for that term changes, this is the one line to
        update to match it -- everything else here is scheduling, not
        physics.

        Only meaningful for a heating load: an active cooling load does not
        help meet a *rising* floor by running harder, so this returns None
        for ``sense == "cool"`` rather than guess at that direction.

        Returns ``(moment, required, achievable)`` for the first violation,
        or None if the schedule looks reachable.
        """
        if self.sense != SENSE_HEAT or self.heating_rate <= 0:
            return None

        minimums, _ = self.comfort_band(grid_start, step, horizon_steps)
        origin = grid_start.astimezone(UTC)

        for index in range(1, horizon_steps):
            elapsed_hours = (step * index) / timedelta(hours=1)
            achievable = current_temperature + self.heating_rate * elapsed_hours
            if achievable < minimums[index]:
                return (
                    dt_util.as_local(origin + step * index),
                    minimums[index],
                    achievable,
                )
        return None

    def to_emhass(
        self, grid_start: datetime, step: timedelta, horizon_steps: int
    ) -> dict[str, Any]:
        """The ``thermal_config`` entry for this load."""
        minimums, maximums = self.comfort_band(grid_start, step, horizon_steps)
        if self.clamp_floors and self.current_temperature is not None and self.heating_rate > 0:
            # A slower rate stretches the ramp backwards, so a floor that was
            # reachable under the full rate can now sit above the flat-out
            # trajectory. EMHASS holds floors hard, which would make the whole
            # solve infeasible; ask for what the load can still deliver.
            minimums = [
                min(
                    floor,
                    self.current_temperature
                    + self.heating_rate * (step * index) / timedelta(hours=1),
                )
                if index
                else floor
                for index, floor in enumerate(minimums)
            ]
        config: dict[str, Any] = {
            "heating_rate": self.heating_rate,
            "cooling_constant": self.cooling_constant,
            "thermal_inertia": self.thermal_inertia,
            "sense": self.sense,
            "min_temperatures": minimums,
            "max_temperatures": maximums,
        }
        if self.current_temperature is not None:
            config["start_temperature"] = self.current_temperature
        return config


def _comfort_starts(wall_time: datetime, timezone: tzinfo) -> list[datetime]:
    """Every real instant a comfort start happens on one day, in UTC.

    Two in the repeated autumn hour; one otherwise, a start inside the
    spring-forward gap resolving to the first valid instant after it.
    """
    # Imported here: water_tank imports this module.
    from .water_tank import _resolve_endpoint, _valid_instants

    return _valid_instants(wall_time, timezone) or [_resolve_endpoint(wall_time, timezone)]


def _in_comfort(moment: time, start: time, end: time) -> bool:
    """Whether a wall-clock time is inside the comfort window.

    Handles a window that crosses midnight, which a night-shift household or
    an overnight setback both produce.
    """
    if start == end:
        # A zero-length window is read as "always comfortable" rather than
        # "never", because the latter would silently let a house go cold.
        return True
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


def _parse_time(value: Any, default: time) -> time:
    if value in (None, ""):
        return default
    if isinstance(value, time):
        return value
    return time.fromisoformat(str(value))


def build_def_load_config(
    thermal_by_index: dict[int, ThermalConfig | WaterTankConfig | HeatPumpRoomConfig],
    load_count: int,
    grid_start: datetime,
    step: timedelta,
    horizon_steps: int,
) -> list[dict[str, Any]] | None:
    """Build EMHASS's ``def_load_config`` list, or None if no load is thermal.

    The list must have exactly one entry per deferrable load, in the same
    order as every other per-load array, with ``{}`` for the ordinary ones.
    Sending it also *overwrites* ``number_of_deferrable_loads`` with its
    length, so a short list would silently drop loads off the end of the
    optimisation -- hence building it from the authoritative count rather than
    from the thermal loads alone.
    """
    if not thermal_by_index:
        return None

    entries: list[dict[str, Any]] = []
    for index in range(load_count):
        thermal = thermal_by_index.get(index)
        if thermal is None:
            entries.append({})
        else:
            key = "thermal_config" if isinstance(thermal, ThermalConfig) else "thermal_battery"
            entries.append({key: thermal.to_emhass(grid_start, step, horizon_steps)})
    return entries

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

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

SENSE_HEAT = "heat"
SENSE_COOL = "cool"
SENSES = (SENSE_HEAT, SENSE_COOL)

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

        Timestep *i* is the interval starting at ``grid_start + i * step``,
        evaluated in local wall-clock time because that is what a comfort
        schedule means to the person who set it.

        Both lists are exactly ``horizon_steps`` long. EMHASS requires at least
        that many; sending more would be silently truncated, and sending fewer
        is an error there rather than a clamp.
        """
        local_start = dt_util.as_local(grid_start)
        minimums: list[float] = []

        for index in range(horizon_steps):
            moment = (local_start + step * index).time()
            inside = _in_comfort(moment, self.comfort_start, self.comfort_end)
            minimums.append(self.comfort_temperature if inside else self.setback_temperature)

        maximums = [self.max_temperature] * horizon_steps
        return minimums, maximums

    def to_emhass(
        self, grid_start: datetime, step: timedelta, horizon_steps: int
    ) -> dict[str, Any]:
        """The ``thermal_config`` entry for this load."""
        minimums, maximums = self.comfort_band(grid_start, step, horizon_steps)
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
    thermal_by_index: dict[int, ThermalConfig],
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
            entries.append({"thermal_config": thermal.to_emhass(grid_start, step, horizon_steps)})
    return entries

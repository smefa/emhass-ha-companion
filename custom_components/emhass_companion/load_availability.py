"""Weekly Schedule helpers and their optional power ceilings.

The public schedule.get_schedule service supplies future blocks. Live control
reads the helper's state and attributes, so it never waits for a new solve.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import math
from typing import Any

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
POWER_LIMIT = "power_limit_w"


def power_limit(data: dict[str, Any], nominal: float) -> float:
    """A missing ceiling means full power; malformed ceilings are errors."""
    value = data.get(POWER_LIMIT, nominal)
    if isinstance(value, bool):
        raise ValueError("power_limit_w must be a finite, non-negative number")
    try:
        watts = float(value)
    except (TypeError, ValueError) as err:
        raise ValueError("power_limit_w must be a finite, non-negative number") from err
    if not math.isfinite(watts) or watts < 0:
        raise ValueError("power_limit_w must be a finite, non-negative number")
    return min(watts, nominal)


def _seconds(value: Any) -> int:
    if value == time.max:
        return 86400
    if isinstance(value, time):
        return value.hour * 3600 + value.minute * 60 + value.second
    if value in ("24:00", "24:00:00"):
        return 86400
    parsed = dt_util.parse_time(str(value))
    if parsed is None:
        raise ValueError(f"Invalid schedule time: {value}")
    return parsed.hour * 3600 + parsed.minute * 60 + parsed.second


@dataclass(frozen=True)
class PowerControl:
    """A power number's actual writable range and increments, expressed in W."""

    minimum_w: float
    maximum_w: float
    step_w: float
    scale: float

    @classmethod
    def from_state(cls, state: State | None) -> PowerControl:
        if state is None or state.state in ("unknown", "unavailable"):
            raise ValueError("power control is unavailable")
        unit = state.attributes.get("unit_of_measurement")
        if unit not in ("W", "kW"):
            raise ValueError("power control must use W or kW")
        scale = 1000 if unit == "kW" else 1
        try:
            minimum = float(state.attributes["min"]) * scale
            maximum = float(state.attributes["max"]) * scale
            step = float(state.attributes.get("step", 1)) * scale
            current = float(state.state)
        except (KeyError, TypeError, ValueError) as err:
            raise ValueError("invalid power control bounds/state") from err
        if (
            not all(math.isfinite(value) for value in (minimum, maximum, step, current))
            or not 0 <= minimum <= maximum
            or step <= 0
        ):
            raise ValueError("invalid power control bounds/state")
        return cls(minimum, maximum, step, scale)

    def ceiling(self, watts: float) -> float:
        watts = min(watts, self.maximum_w)
        if watts < self.minimum_w:
            return 0.0
        return (
            self.minimum_w + math.floor((watts - self.minimum_w) / self.step_w + 1e-9) * self.step_w
        )


@dataclass(frozen=True)
class WeeklyAvailability:
    """Validated blocks, in local wall time, with watt ceilings."""

    blocks: tuple[tuple[int, int, int, float], ...]

    @classmethod
    def parse(cls, raw: dict[str, Any], nominal: float) -> WeeklyAvailability:
        blocks = []
        for day, name in enumerate(DAYS):
            ranges = raw.get(name, [])
            if not isinstance(ranges, list):
                raise ValueError(f"Invalid schedule day: {name}")
            previous_end = 0
            for block in sorted(ranges, key=lambda item: _seconds(item["from"])):
                start, end = _seconds(block["from"]), _seconds(block["to"])
                if not previous_end <= start < end <= 86400:
                    raise ValueError(f"Invalid or overlapping schedule blocks: {name}")
                data = block.get("data", {})
                if not isinstance(data, dict):
                    raise ValueError("Schedule block data must be a mapping")
                blocks.append((day, start, end, power_limit(data, nominal)))
                previous_end = end
        return cls(tuple(blocks))

    def ceiling(self, when: datetime, outside: float = 0.0) -> float:
        local = dt_util.as_local(when)
        second = local.hour * 3600 + local.minute * 60 + local.second
        return next(
            (
                watts
                for day, start, end, watts in self.blocks
                if day == local.weekday() and start <= second < end
            ),
            outside,
        )

    def step_ceiling(self, start: datetime, step: timedelta, outside: float = 0.0) -> float:
        """Use the lowest ceiling throughout a slot, including partial blocks/DST."""
        end = start + step
        # Include the clock on either side of DST jumps. A repeated hour can
        # cross into a closed period without crossing a configured boundary.
        candidates = [start]
        tick = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
        while tick < end:
            candidates.append(tick)
            tick += timedelta(minutes=1)
        date = dt_util.as_local(start).date()
        last = dt_util.as_local(end).date()
        while date <= last:
            for day, begin, finish, _ in self.blocks:
                if day != date.weekday():
                    continue
                for seconds in (begin, finish):
                    wall = datetime.combine(date, time()) + timedelta(seconds=seconds)
                    for fold in (0, 1):
                        boundary = dt_util.as_utc(
                            wall.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE, fold=fold)
                        )
                        if start < boundary < end:
                            candidates.append(boundary)
            date += timedelta(days=1)
        return min(self.ceiling(when, outside) for when in candidates)


async def async_read_schedule(
    hass: HomeAssistant, entity_id: str, nominal: float
) -> WeeklyAvailability:
    """Fetch the complete week through HA's supported response service."""
    state = hass.states.get(entity_id)
    if state is None or state.state not in ("on", "off"):
        raise ValueError(f"Schedule {entity_id} is unavailable")
    response = await hass.services.async_call(
        "schedule",
        "get_schedule",
        {},
        target={"entity_id": entity_id},
        blocking=True,
        return_response=True,
    )
    if not isinstance(response, dict) or not isinstance(response.get(entity_id), dict):
        raise ValueError(f"Schedule {entity_id} did not return its weekly blocks")
    return WeeklyAvailability.parse(response[entity_id], nominal)


def run_floor(load: Any) -> float:
    """The lowest power this load can usefully run at, as the planner sees it.

    A full-power-only or sequence load ignores its minimum power setting, and
    a minimum above the load's power is clamped the way the payload clamps it.
    """
    if load.semi_continuous or load.is_sequence:
        return load.nominal_power_w
    return min(load.minimum_power_w, load.nominal_power_w)


def live_ceiling(hass: HomeAssistant, load: Any) -> float:
    """Immediate permission, including Run now's time-only override.

    Fixed-power appliances cannot honour a reduced ceiling. Once a sequence
    has started, allow it to finish; the planner must fit the whole cycle.
    """
    nominal = load.nominal_power_w
    if load.power_control_entity:
        if not load.control_entity:
            return 0.0
        try:
            nominal = PowerControl.from_state(hass.states.get(load.power_control_entity)).ceiling(
                nominal
            )
        except ValueError:
            return 0.0
    floor = run_floor(load)
    if nominal < floor:
        return 0.0
    if not load.availability_schedule or load.sequence_cycle_active:
        return nominal
    state = hass.states.get(load.availability_schedule)
    if state is None or state.state not in ("on", "off"):
        return 0.0
    if state.state == "off":
        return nominal if load.mode == "force_on" or load.sequence_start_pending else 0.0
    try:
        ceiling = power_limit(dict(state.attributes), nominal)
    except ValueError:
        return 0.0
    if ceiling < nominal and (
        load.semi_continuous or load.is_sequence or not load.power_control_entity
    ):
        return 0.0
    if load.power_control_entity:
        try:
            ceiling = PowerControl.from_state(hass.states.get(load.power_control_entity)).ceiling(
                ceiling
            )
        except ValueError:
            return 0.0
    return ceiling if ceiling >= floor else 0.0


def commanded_power(hass: HomeAssistant, load: Any, planned_w: float) -> float:
    """The setpoint a power control gets for a planned power; 0 keeps the load off.

    Shared by the executor and the Should run sensor so the two cannot
    disagree: a plan below the number's lowest setting runs nothing.
    """
    try:
        control = PowerControl.from_state(hass.states.get(load.power_control_entity))
    except ValueError:
        return 0.0
    target = control.ceiling(min(planned_w, live_ceiling(hass, load)))
    return target if target > 0 and target >= run_floor(load) else 0.0


def _allowed(state: State) -> float:
    """A helper state's live permission in W; no limit ranks above any limit."""
    if state.state != "on":
        return 0.0
    return power_limit(dict(state.attributes), 1e12)


def _expected_transition(old: State | None, new: State | None) -> bool:
    """A helper closing or lowering its limit at the boundary it announced.

    The plan already holds that, and the live gate enforces it at once. An
    opening or a higher limit is different: a slot the boundary falls inside
    was planned at its lowest ceiling, so only a new plan can use the rest.
    """
    if old is None or new is None:
        return False
    if old.state not in ("on", "off") or new.state not in ("on", "off"):
        return False
    due = old.attributes.get("next_event")
    if isinstance(due, str):
        due = dt_util.parse_datetime(due)
    if not isinstance(due, datetime):
        return False
    if not -1 <= (new.last_updated - dt_util.as_utc(due)).total_seconds() <= 10:
        return False
    try:
        return _allowed(new) <= _allowed(old)
    except ValueError:
        return False


def async_track_availability(
    hass: HomeAssistant, loads: Iterable[Any], on_change: Callable[[bool], None]
) -> Callable[[], None]:
    """Watch helper transitions and number capability changes, ignoring setpoints.

    ``on_change`` is told whether a new plan is needed. A helper closing on
    time is not: the plan was built from the same blocks, so only the live
    gate has to be re-applied. An opening, edit, outage or recovery is.
    """
    loads = list(loads)
    schedules = {load.availability_schedule for load in loads if load.availability_schedule}
    controls = {load.power_control_entity for load in loads if load.power_control_entity}
    if not schedules and not controls:
        return lambda: None

    def bounds(state: State | None) -> PowerControl | None:
        try:
            return PowerControl.from_state(state)
        except ValueError:
            return None

    @callback
    def changed(event: Event) -> None:
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if event.data["entity_id"] in schedules:
            on_change(not _expected_transition(old, new))
        elif bounds(old) != bounds(new):
            on_change(True)
        # Otherwise a setpoint change, including our own writes: it must not
        # cause a solve/write/solve feedback loop.

    return async_track_state_change_event(hass, list(schedules | controls), changed)

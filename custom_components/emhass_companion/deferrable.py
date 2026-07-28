"""Live state for deferrable loads.

Each deferrable load is a config subentry, and each gets a device. The
subentry stores what the user answered when adding the load; this module holds
the values they can change afterwards.

Those live values are *not* written back into the subentry. Updating a config
entry triggers a reload, which is far too heavy for something a user toggles
from a dashboard. Instead the registry here is the single source of truth at
runtime: entities read and write it, the coordinator reads it when building a
payload, and each entity persists its own value through ``Restore*Entity`` so
the registry can be repopulated after a restart.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONTROL_ENTITY,
    CONF_EARLIEST_START,
    CONF_LATEST_END,
    CONF_NOMINAL_POWER,
    CONF_OPERATING_HOURS,
    CONF_POWER_SENSOR,
    CONF_SEMI_CONTINUOUS,
    CONF_SINGLE_CONSTANT,
    CONF_STARTUP_PENALTY,
    LOAD_MODE_AUTO,
    LOAD_MODE_FORCE_OFF,
    LOAD_MODE_FORCE_ON,
    SUBENTRY_TYPE_DEFERRABLE,
)
from .models import DeferrableLoad

_LOGGER = logging.getLogger(__name__)

# A load is considered running above this fraction of its nominal power, with a
# floor so that a standby draw of a few watts never counts as operation.
RUNNING_FRACTION = 0.10
RUNNING_FLOOR_W = 10.0


@dataclass(slots=True)
class DeferrableRuntime:
    """One deferrable load's live state."""

    subentry_id: str
    name: str

    # Set when the load was added; used to seed the entities below.
    semi_continuous: bool = True
    single_constant: bool = False
    startup_penalty: float = 0.0
    power_sensor: str | None = None
    control_entity: str | None = None

    # Owned by entities from here down.
    enabled: bool = True
    nominal_power_w: float = 0.0
    operating_hours: float = 0.0
    use_time_window: bool = False
    earliest_start: time | None = None
    latest_end: time | None = None
    mode: str = LOAD_MODE_AUTO

    # Observed from the load's own power sensor.
    runtime_today: timedelta = timedelta()
    running_since: datetime | None = None
    runtime_day: date | None = None

    _listeners: list[Callable[[], None]] = field(default_factory=list, repr=False)

    # -- observation ----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self.running_since is not None

    @property
    def running_threshold_w(self) -> float:
        return max(self.nominal_power_w * RUNNING_FRACTION, RUNNING_FLOOR_W)

    def elapsed_today(self, now: datetime) -> timedelta:
        """Runtime so far today, including any in-progress run."""
        total = self.runtime_today
        if self.running_since is not None:
            total += now - self.running_since
        return total

    def observe_power(self, watts: float | None, now: datetime) -> None:
        """Fold a power reading into the runtime accumulator."""
        if watts is None:
            # Treat an unavailable sensor as "stop counting" rather than
            # guessing; an unbounded open interval would inflate runtime.
            self._stop(now)
            return

        if watts >= self.running_threshold_w:
            if self.running_since is None:
                self.running_since = now
        else:
            self._stop(now)

    def _stop(self, now: datetime) -> None:
        if self.running_since is not None:
            self.runtime_today += now - self.running_since
            self.running_since = None

    def reset_day(self, now: datetime) -> None:
        """Start a new day's runtime accounting.

        EMHASS has no notion of a day boundary for
        ``def_current_operating_timesteps``; it is the caller's job to reset it,
        otherwise yesterday's hours keep suppressing today's schedule.
        """
        self._stop(now)
        self.runtime_today = timedelta()
        self.runtime_day = dt_util.as_local(now).date()
        if self.is_running:  # pragma: no cover - _stop clears it
            self.running_since = now

    # -- payload --------------------------------------------------------------

    def completed_timesteps(self, now: datetime, step_minutes: int) -> int:
        elapsed = self.elapsed_today(now).total_seconds() / 60
        return int(elapsed // step_minutes)

    def to_load(self, now: datetime, step_minutes: int) -> DeferrableLoad:
        """Project the live state into what the payload builder consumes."""
        windowed = self.use_time_window
        return DeferrableLoad(
            subentry_id=self.subentry_id,
            name=self.name,
            nominal_power_w=self.nominal_power_w,
            operating_hours=self.operating_hours,
            earliest_start=self.earliest_start if windowed else None,
            latest_end=self.latest_end if windowed else None,
            semi_continuous=self.semi_continuous,
            single_constant=self.single_constant,
            startup_penalty=self.startup_penalty,
            enabled=self.enabled,
            # A load that is already running should not be charged a startup
            # penalty again, nor be re-scheduled for work it has done today.
            current_state=self.is_running or self.mode == LOAD_MODE_FORCE_ON,
            current_power_w=self.nominal_power_w if self.is_running else 0.0,
            completed_timesteps=self.completed_timesteps(now, step_minutes),
        )

    # -- change notification --------------------------------------------------

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def notify(self) -> None:
        for listener in list(self._listeners):
            listener()


class DeferrableRegistry:
    """All deferrable loads for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._loads: dict[str, DeferrableRuntime] = {}
        self._unsubscribes: list[Callable[[], None]] = []

    # -- lifecycle ------------------------------------------------------------

    def sync(self) -> None:
        """Rebuild the registry from the entry's subentries."""
        seen: set[str] = set()
        for subentry_id, subentry in self.entry.subentries.items():
            if subentry.subentry_type != SUBENTRY_TYPE_DEFERRABLE:
                continue
            seen.add(subentry_id)
            data = dict(subentry.data)
            if (existing := self._loads.get(subentry_id)) is None:
                self._loads[subentry_id] = _from_subentry(subentry_id, subentry.title, data)
            else:
                _apply_subentry_fields(existing, subentry.title, data)

        for stale in set(self._loads) - seen:
            del self._loads[stale]

    @callback
    def async_start(self) -> None:
        """Begin observing each load's power sensor."""
        sensors = {load.power_sensor: None for load in self._loads.values() if load.power_sensor}
        if sensors:
            self._unsubscribes.append(
                async_track_state_change_event(self.hass, list(sensors), self._async_power_changed)
            )
            # Seed from current state so a restart mid-run is not counted as off.
            now = dt_util.utcnow()
            for load in self._loads.values():
                if load.power_sensor:
                    load.observe_power(_read_power(self.hass, load.power_sensor), now)

        self._unsubscribes.append(
            async_track_time_change(self.hass, self._async_midnight, hour=0, minute=0, second=0)
        )

    @callback
    def async_stop(self) -> None:
        while self._unsubscribes:
            self._unsubscribes.pop()()

    # -- access ---------------------------------------------------------------

    def get(self, subentry_id: str) -> DeferrableRuntime | None:
        return self._loads.get(subentry_id)

    def all(self) -> list[DeferrableRuntime]:
        """Every load, in a stable order.

        The order defines which ``P_deferrable{k}`` column belongs to which
        load, so it must not depend on dictionary iteration order.
        """
        return sorted(self._loads.values(), key=lambda load: (load.name.lower(), load.subentry_id))

    def to_loads(self, now: datetime, step_minutes: int) -> list[DeferrableLoad]:
        return [load.to_load(now, step_minutes) for load in self.all()]

    # -- events ---------------------------------------------------------------

    @callback
    def _async_power_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        watts = _state_to_power(event.data["new_state"])
        now = dt_util.utcnow()
        for load in self._loads.values():
            if load.power_sensor == entity_id:
                was_running = load.is_running
                load.observe_power(watts, now)
                if load.is_running != was_running:
                    load.notify()

    @callback
    def _async_midnight(self, now: datetime) -> None:
        for load in self._loads.values():
            load.reset_day(now)
            load.notify()
        _LOGGER.debug("Reset deferrable load runtime counters for the new day")


# --- helpers -----------------------------------------------------------------


def _from_subentry(subentry_id: str, title: str, data: dict) -> DeferrableRuntime:
    load = DeferrableRuntime(subentry_id=subentry_id, name=title)
    _apply_subentry_fields(load, title, data)
    # Seed the entity-owned values; a restored entity overwrites these on start.
    load.nominal_power_w = float(data.get(CONF_NOMINAL_POWER, 0) or 0)
    load.operating_hours = float(data.get(CONF_OPERATING_HOURS, 0) or 0)
    load.earliest_start = _parse_time(data.get(CONF_EARLIEST_START))
    load.latest_end = _parse_time(data.get(CONF_LATEST_END))
    load.use_time_window = load.earliest_start is not None or load.latest_end is not None
    return load


def _apply_subentry_fields(load: DeferrableRuntime, title: str, data: dict) -> None:
    """Refresh the fields the subentry owns, leaving entity-owned ones alone."""
    load.name = title
    load.semi_continuous = bool(data.get(CONF_SEMI_CONTINUOUS, True))
    load.single_constant = bool(data.get(CONF_SINGLE_CONSTANT, False))
    load.startup_penalty = float(data.get(CONF_STARTUP_PENALTY, 0) or 0)
    load.power_sensor = data.get(CONF_POWER_SENSOR) or None
    load.control_entity = data.get(CONF_CONTROL_ENTITY) or None


def _parse_time(value) -> time | None:
    if value in (None, ""):
        return None
    if isinstance(value, time):
        return value
    return time.fromisoformat(str(value))


def _read_power(hass: HomeAssistant, entity_id: str) -> float | None:
    return _state_to_power(hass.states.get(entity_id))


def _state_to_power(state) -> float | None:
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def resolve_should_run(mode: str, scheduled: bool) -> bool:
    """Combine the plan's answer with any manual override.

    The mode overrides the *signal*, not the optimisation. A user who wants a
    load left out of planning altogether turns off its Enabled switch instead;
    keeping the two orthogonal means "force off" never silently changes what
    EMHASS is solving.
    """
    if mode == LOAD_MODE_FORCE_ON:
        return True
    if mode == LOAD_MODE_FORCE_OFF:
        return False
    return scheduled

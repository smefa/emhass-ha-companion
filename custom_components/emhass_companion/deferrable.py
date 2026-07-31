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

from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfPower,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import PowerConverter

from .const import (
    CONF_CONTROL_ENTITY,
    CONF_EARLIEST_START,
    CONF_LATEST_END,
    CONF_MAX_STARTUPS,
    CONF_MINIMUM_POWER,
    CONF_NOMINAL_POWER,
    CONF_OPERATING_HOURS,
    CONF_POWER_SENSOR,
    CONF_SEMI_CONTINUOUS,
    CONF_SINGLE_CONSTANT,
    CONF_STARTUP_PENALTY,
    LOAD_MODE_AUTO,
    LOAD_MODE_FORCE_OFF,
    LOAD_MODE_FORCE_ON,
    RECURRENCE_DAILY,
    RECURRENCE_ON_DEMAND,
    SUBENTRY_TYPE_DEFERRABLE,
)
from .models import DeferrableLoad, Plan, PlanRow

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

    # Owned by the subentry: what the load *is*, rather than what it is
    # currently being asked to do.
    power_sensor: str | None = None
    control_entity: str | None = None

    # Owned by entities from here down.
    enabled: bool = True
    nominal_power_w: float = 0.0
    minimum_power_w: float = 0.0
    operating_hours: float = 0.0
    use_time_window: bool = False
    earliest_start: time | None = None
    latest_end: time | None = None
    semi_continuous: bool = True
    single_constant: bool = False
    startup_penalty: float = 0.0
    max_startups: int = 0
    mode: str = LOAD_MODE_AUTO

    # Whether this load wants its operating_hours every day (the only
    # behaviour before this field existed) or only once armed by a request --
    # see docs/on_demand_loads.md. Entity-owned, like everything else below.
    recurrence: str = RECURRENCE_DAILY
    requested: bool = False

    # Observed from the load's own power sensor.
    runtime_today: timedelta = timedelta()
    running_since: datetime | None = None
    runtime_day: date | None = None

    # How far the plan-trusting fallback below has already replayed. Only ever
    # advanced for a load with no running_source; irrelevant otherwise.
    plan_assumed_until: datetime | None = field(default=None, repr=False)

    _listeners: list[Callable[[], None]] = field(default_factory=list, repr=False)

    # -- observation ----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self.running_since is not None

    @property
    def participates(self) -> bool:
        """Whether this load has standing demand right now.

        A daily load always does, matching the only behaviour that existed
        before recurrence was a choice. An on-demand load has none until
        armed -- dropping out of participation while unrequested is what
        stops EMHASS being asked for operating_hours every day regardless of
        whether anyone needs it.
        """
        return self.recurrence == RECURRENCE_DAILY or self.requested

    @property
    def running_threshold_w(self) -> float:
        return max(self.nominal_power_w * RUNNING_FRACTION, RUNNING_FLOOR_W)

    @property
    def running_source(self) -> str | None:
        """The entity whose state says whether this load is running.

        A power sensor is the honest answer -- it measures the load rather than
        the intent. The control entity is a good enough stand-in when there is
        no meter: if it is on, then either the executor or the user's own
        automation switched the load on, which is exactly what EMHASS is being
        told. Tracking this ourselves is what lets EMHASS know work has already
        been done today; EMHASS keeps no history of its own between runs.
        """
        return self.power_sensor or self.control_entity

    def observe(self, state: State | None, now: datetime) -> None:
        """Fold a reading from this load's running source into the accumulator."""
        self.observe_power(self.state_to_power(state), now)

    def state_to_power(self, state: State | None) -> float | None:
        """Interpret a running-source state as watts, or None for "no reading"."""
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
            return None
        if state.state in (STATE_ON, STATE_OFF):
            # A switch, input_boolean or script only says on or off. Reading
            # "on" as the full nominal power is the same assumption EMHASS's
            # semi-continuous model makes, and clears running_threshold_w by
            # construction rather than by luck.
            return self.nominal_power_w if state.state == STATE_ON else 0.0
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None

        # nominal_power_w, minimum_power_w and running_threshold_w are all
        # watts; a sensor reporting kW (a `power` device class is not
        # guaranteed to be W -- kW is just as common on a whole-appliance
        # meter) would otherwise be read 1000x too small, and every load would
        # look permanently idle.
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit and unit != UnitOfPower.WATT:
            # Not a power unit HA recognises -- nothing sensible to convert
            # from, so fall through and use the raw number rather than
            # discarding a reading entirely.
            with suppress(HomeAssistantError):
                value = PowerConverter.convert(value, unit, UnitOfPower.WATT)
        return value

    def assume_from_plan(self, rows: Iterable[PlanRow], index: int, now: datetime) -> None:
        """Trust our own last plan in place of a reading nothing can provide.

        With no power sensor and no control entity, there is no way to observe
        this load at all -- so without this, ``completed_timesteps`` would stay
        0 forever and EMHASS would re-propose the full ``operating_hours``
        target on every MPC cycle for the rest of the day, never learning that
        an earlier window was already served (see docs/plan_output_schema.md
        and the "already running" discussion in the PR that added this).

        This is a deliberate trade: with a real running_source, EMHASS's
        knowledge tracks the physical world. Without one, it tracks *our own
        past recommendation* -- accurate only to the extent the plan was
        actually followed, with no way to notice if it was not. That is a much
        better default than pretending nothing ever ran, but it is still a
        guess, which is why a real source always wins when one exists (guarded
        by the caller checking ``running_source is None`` first).

        Replays exactly the newly-elapsed slice of ``rows`` -- the half-open
        interval ``(plan_assumed_until, now]`` -- through the same
        ``observe_power`` accumulator a live sensor would drive, so a plan that
        scheduled this load on and then off is folded into ``runtime_today``
        the same way a real reading crossing the threshold would be. Advancing
        the cursor to ``now`` unconditionally (not just to the last replayed
        row) is what keeps this idempotent if the same plan is handed back
        again next cycle, e.g. while EMHASS is repeatedly infeasible: rows
        already replayed are skipped, and once the plan's own horizon is behind
        us there is nothing left to assume, exactly as if the trail had gone
        cold.
        """
        since = self.plan_assumed_until
        for row in rows:
            if row.timestamp > now:
                break
            if since is not None and row.timestamp <= since:
                continue
            if index < len(row.deferrables):
                self.observe_power(row.deferrables[index], row.timestamp)
        self.plan_assumed_until = now

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

    def check_auto_disarm(self, now: datetime) -> bool:
        """Clear a fulfilled on-demand request. Returns True if it changed.

        No user automation can build this reliably: it needs the same
        elapsed-today accounting the payload itself is built from. A daily
        load has no request to clear, and an on-demand load's request stays
        armed until it has actually run its operating_hours -- returning here
        early leaves it armed rather than clearing it too soon.
        """
        if self.recurrence != RECURRENCE_ON_DEMAND or not self.requested:
            return False
        if self.elapsed_today(now) < timedelta(hours=self.operating_hours):
            return False
        self.requested = False
        return True

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
            minimum_power_w=self.minimum_power_w,
            operating_hours=self.operating_hours,
            earliest_start=self.earliest_start if windowed else None,
            latest_end=self.latest_end if windowed else None,
            semi_continuous=self.semi_continuous,
            single_constant=self.single_constant,
            startup_penalty=self.startup_penalty,
            max_startups=self.max_startups,
            # Enabled/disabled and armed/unarmed are independent gates; both
            # must pass for the optimiser to be asked about this load at all.
            enabled=self.enabled and self.participates,
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
        """Begin observing whatever tells each load it is running."""
        sources = {
            load.running_source: None for load in self._loads.values() if load.running_source
        }
        if sources:
            self._unsubscribes.append(
                async_track_state_change_event(self.hass, list(sources), self._async_source_changed)
            )
            # Seed from current state so a restart mid-run is not counted as off.
            now = dt_util.utcnow()
            for load in self._loads.values():
                if (source := load.running_source) is not None:
                    load.observe(self.hass.states.get(source), now)

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

    def assume_from_plan(self, plan: Plan | None, load_order: list[str], now: datetime) -> None:
        """Give every sourceless load a chance to trust the previous plan.

        Called once per run, with the *previous* run's plan and load order --
        the pairing that was actually in effect while that plan's now-elapsed
        rows played out. A load with a real running_source is skipped
        entirely; see :meth:`DeferrableRuntime.assume_from_plan` for why this
        is a last resort, not a default.
        """
        if plan is None:
            return
        for load in self._loads.values():
            if load.running_source is not None:
                continue
            try:
                index = load_order.index(load.subentry_id)
            except ValueError:
                continue
            load.assume_from_plan(plan.rows, index, now)
            if load.check_auto_disarm(now):
                load.notify()

    # -- events ---------------------------------------------------------------

    @callback
    def _async_source_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        state = event.data["new_state"]
        now = dt_util.utcnow()
        for load in self._loads.values():
            if load.running_source == entity_id:
                was_running = load.is_running
                load.observe(state, now)
                if load.is_running != was_running:
                    if not load.is_running:
                        load.check_auto_disarm(now)
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
    load.minimum_power_w = float(data.get(CONF_MINIMUM_POWER, 0) or 0)
    load.operating_hours = float(data.get(CONF_OPERATING_HOURS, 0) or 0)
    load.earliest_start = _parse_time(data.get(CONF_EARLIEST_START))
    load.latest_end = _parse_time(data.get(CONF_LATEST_END))
    load.use_time_window = load.earliest_start is not None or load.latest_end is not None
    load.semi_continuous = bool(data.get(CONF_SEMI_CONTINUOUS, True))
    load.single_constant = bool(data.get(CONF_SINGLE_CONSTANT, False))
    load.startup_penalty = float(data.get(CONF_STARTUP_PENALTY, 0) or 0)
    load.max_startups = int(data.get(CONF_MAX_STARTUPS, 0) or 0)
    return load


def _apply_subentry_fields(load: DeferrableRuntime, title: str, data: dict) -> None:
    """Refresh the fields the subentry owns, leaving entity-owned ones alone.

    Everything the optimiser is *told* -- powers, hours, window, semi-cont,
    startup limits -- is entity-owned so an automation can change it without
    reloading the entry, which is why none of it is refreshed here. The
    subentry's copy is the value a newly created load starts from, and the
    subentry form stops offering it once the load exists.
    """
    load.name = title
    load.power_sensor = data.get(CONF_POWER_SENSOR) or None
    load.control_entity = data.get(CONF_CONTROL_ENTITY) or None


def _parse_time(value) -> time | None:
    if value in (None, ""):
        return None
    if isinstance(value, time):
        return value
    return time.fromisoformat(str(value))


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

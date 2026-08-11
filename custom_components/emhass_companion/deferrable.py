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
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import PowerConverter, TemperatureConverter

from .const import (
    COMPLETION_CANCELLED,
    COMPLETION_COMPLETED,
    COMPLETION_CUT_SHORT,
    COMPLETION_FINISHED_EARLY,
    COMPLETION_NEVER_STARTED,
    CONF_COMFORT_END,
    CONF_COMFORT_START,
    CONF_COMFORT_TEMPERATURE,
    CONF_CONTROL_ENTITY,
    CONF_COOLING_CONSTANT,
    CONF_EARLIEST_START,
    CONF_ENERGY_NEEDED,
    CONF_HEATING_RATE,
    CONF_LATEST_END,
    CONF_MAX_STARTUPS,
    CONF_MAX_TEMPERATURE,
    CONF_MINIMUM_OFF_TIME,
    CONF_MINIMUM_ON_TIME,
    CONF_MINIMUM_POWER,
    CONF_NOMINAL_POWER,
    CONF_OPERATING_HOURS,
    CONF_POWER_SENSOR,
    CONF_RECURRENCE,
    CONF_SEMI_CONTINUOUS,
    CONF_SENSE,
    CONF_SETBACK_TEMPERATURE,
    CONF_SINGLE_CONSTANT,
    CONF_STARTUP_PENALTY,
    CONF_SURPLUS_HEADROOM,
    CONF_SURPLUS_PRIORITY,
    CONF_TEMPERATURE_SENSOR,
    CONF_THERMAL_INERTIA,
    CONTROL_ENTITY_DOMAINS,
    DEFAULT_IDLE_MINUTES,
    DEFAULT_IDLE_POWER_W,
    DEFAULT_SURPLUS_HEADROOM_W,
    DEFAULT_SURPLUS_PRIORITY,
    DEFAULT_TIME_STEP,
    LOAD_MODE_AUTO,
    LOAD_MODE_FORCE_ON,
    LOAD_SUBENTRY_TYPES,
    LOAD_TYPE_STANDARD,
    LOAD_TYPE_THERMAL,
    RECURRENCE_DAILY,
    RECURRENCE_ON_DEMAND,
    RECURRENCE_SURPLUS,
    RECURRENCES,
    SUBENTRY_TYPE_THERMAL,
)
from .models import DeferrableLoad, Plan, PlanRow, Series
from .stored_time import parse_stored_time
from .surplus import SurplusBudget, SurplusSpec, allocate, battery_reserved_series, surplus_series
from .thermal import (
    DEFAULT_COMFORT_END,
    DEFAULT_COMFORT_START,
    DEFAULT_COMFORT_TEMPERATURE,
    DEFAULT_COOLING_CONSTANT,
    DEFAULT_HEATING_RATE,
    DEFAULT_MAX_TEMPERATURE,
    DEFAULT_SETBACK_TEMPERATURE,
    DEFAULT_THERMAL_INERTIA,
    SENSE_HEAT,
    ThermalConfig,
)

_LOGGER = logging.getLogger(__name__)

# A load is considered running above this fraction of its nominal power, with a
# floor so that a standby draw of a few watts never counts as operation.
RUNNING_FRACTION = 0.10
RUNNING_FLOOR_W = 10.0


def state_to_watts(state: State) -> float | None:
    """Parse a state's numeric value as watts, converting from other power units.

    Shared by every reader of a raw power sensor (deferrable and otherwise):
    a `power` device class is not guaranteed to be W -- kW is just as common
    on a whole-appliance meter -- so a reading in another unit would otherwise
    come out 1000x wrong rather than merely absent.
    """
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None

    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if unit and unit != UnitOfPower.WATT:
        # Not a power unit HA recognises -- nothing sensible to convert
        # from, so fall through and use the raw number rather than
        # discarding a reading entirely.
        with suppress(HomeAssistantError):
            value = PowerConverter.convert(value, unit, UnitOfPower.WATT)
    return value


@dataclass(slots=True)
class DeferrableRuntime:
    """One deferrable load's live state."""

    subentry_id: str
    name: str

    # Owned by the subentry: what the load *is*, rather than what it is
    # currently being asked to do.
    load_type: str = LOAD_TYPE_STANDARD
    power_sensor: str | None = None
    control_entity: str | None = None
    temperature_sensor: str | None = None
    sense: str = SENSE_HEAT

    # Thermal tuning, owned by entities like every other optimiser input. The
    # physics (rates, inertia) live here rather than in the subentry because
    # they are calibrated iteratively -- "my house warms faster than that" is a
    # dashboard adjustment, not a reconfiguration.
    heating_rate: float = DEFAULT_HEATING_RATE
    cooling_constant: float = DEFAULT_COOLING_CONSTANT
    thermal_inertia: float = DEFAULT_THERMAL_INERTIA
    comfort_temperature: float = DEFAULT_COMFORT_TEMPERATURE
    setback_temperature: float = DEFAULT_SETBACK_TEMPERATURE
    max_temperature: float = DEFAULT_MAX_TEMPERATURE
    comfort_start: time = DEFAULT_COMFORT_START
    comfort_end: time = DEFAULT_COMFORT_END

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
    minimum_on_time_minutes: float = 0.0
    minimum_off_time_minutes: float = 0.0
    mode: str = LOAD_MODE_AUTO
    # When Run now was last pressed. Only a backstop for ending a forced run;
    # like ``mode`` itself it does not survive a restart, which already returns
    # the load to auto.
    forced_at: datetime | None = None

    # What counts as "finished" for an on-demand run, and for how long it has to
    # read that way. Only consulted for a load with a real power sensor -- see
    # :attr:`metered`. 0 minutes means one optimisation timestep.
    idle_power_w: float = DEFAULT_IDLE_POWER_W
    idle_minutes: float = DEFAULT_IDLE_MINUTES

    # Whether this load wants its operating_hours every day (the only
    # behaviour before this field existed) or only once armed by a request --
    # see docs/on_demand_loads.md. Entity-owned, like everything else below.
    recurrence: str = RECURRENCE_DAILY
    requested: bool = False

    # How long after a request the run must be finished. 0 means no deadline,
    # matching EMHASS's own convention that 0 is "unconstrained". Only
    # meaningful on demand: a duration needs an instant to count from, and a
    # daily load has no request event to anchor to -- for one of those, "within
    # 4 h of a fixed 06:00 opening" is just a narrower window, which latest_end
    # already expresses. See docs/on_demand_loads.md.
    run_within_hours: float = 0.0

    # Surplus loads only. The headroom is a margin against PV forecast error;
    # the energy cap is a total ("put 20 kWh in the car"), 0 meaning the load
    # simply runs on whatever is spare until its request is turned off.
    surplus_headroom_w: float = DEFAULT_SURPLUS_HEADROOM_W
    energy_needed_kwh: float = 0.0
    # Whether an armed run takes the front of the sunny stretch or is left for
    # EMHASS to place anywhere inside it. Not a second way to arm the load --
    # the request is still what decides *whether* it runs, this only decides
    # *when* inside what the sun allows. See surplus.allocate.
    start_asap: bool = False
    # Which surplus load claims a shared series first -- lower first, ties
    # broken by name (see DeferrableRegistry.apply_surplus). Meaningless with
    # only one surplus load.
    surplus_priority: int = DEFAULT_SURPLUS_PRIORITY

    # What surplus.allocate last decided this load may ask for. Re-derived from
    # the previous plan on every run, so it is never carried across cycles: an
    # empty budget is the correct answer before the first optimisation, and the
    # load parks itself until one exists.
    surplus_budget: SurplusBudget = field(default_factory=SurplusBudget)

    # Observed from the load's own power sensor.
    runtime_today: timedelta = timedelta()
    running_since: datetime | None = None
    # Symmetric to running_since -- "continuously off since". Neither survives
    # a restart (see continuous_on_timesteps/continuous_off_timesteps below),
    # the same conservative fallback runtime_today already uses.
    off_since: datetime | None = None
    runtime_day: date | None = None

    # The anchor a deadline counts from, and progress measured against *it*
    # rather than the calendar day. A request armed at 23:00 outlives the
    # midnight reset (docs/on_demand_loads.md), so runtime_today would report
    # zero completed steps after midnight and EMHASS would re-schedule the
    # whole target -- running the load twice.
    requested_at: datetime | None = None
    request_runtime: timedelta = timedelta()

    # The commanded clock, and the two observations that judge it.
    #
    # An on-demand run is measured by how long it was *told* to run, not by how
    # long its meter saw it draw. The meter under-counts every appliance that
    # duty-cycles -- a dishwasher spends a third of its program filling, soaking
    # and draining below any sensible running threshold -- so a request judged
    # on measured time can outlive a finished cycle indefinitely, and keeps
    # asking EMHASS for hours it has already been given. Commanded time is also
    # what a load with no power sensor has always been judged on (state_to_power
    # reports nominal whenever the control entity is on), so this makes the
    # metered and unmetered paths agree rather than inventing a third rule.
    #
    # It is *commanded* time and not wall clock: a plan that splits a run across
    # two blocks must not be credited with the gap between them.
    commanded_since: datetime | None = None
    command_runtime: timedelta = timedelta()
    # Whether the executor has said anything about this load yet. False for the
    # whole of a restart until the first apply, and the difference between "we
    # are not commanding this load" and "we have not heard yet" -- readings
    # arriving in that gap must not be allowed to throw away a restored idle
    # window on the strength of a command nobody has issued.
    commands_seen: bool = field(default=False, repr=False)
    # Latched by the first reading at or above the running threshold since the
    # request was armed. Its absence is what separates "the appliance finished"
    # from "the appliance never started", which otherwise read identically: no
    # power now. Also what stops the fill phase at the very start of a program
    # being mistaken for the end of one.
    seen_running: bool = False
    # Continuously idle since -- idle meaning a *reading* below idle_power_w
    # while the load is being told to run. Both halves matter. An unavailable
    # sensor is not a reading, and a load nobody is asking to run is expected to
    # draw nothing; treating either as idle would call an interrupted run
    # finished.
    idle_since: datetime | None = None

    # Why the last run ended, and when. Outlives the request -- see cancel().
    last_completion_reason: str | None = None
    last_completion_at: datetime | None = None

    # How far the plan-trusting fallback below has already replayed. Only ever
    # advanced for a load with no running_source; irrelevant otherwise.
    plan_assumed_until: datetime | None = field(default=None, repr=False)

    _listeners: list[Callable[[], None]] = field(default_factory=list, repr=False)

    # -- observation ----------------------------------------------------------

    @property
    def is_thermal(self) -> bool:
        """Whether the optimiser controls this load's temperature, not its run time."""
        return self.load_type == LOAD_TYPE_THERMAL

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
        whether anyone needs it. A surplus load is armed the same way, and so
        needs no special case here.
        """
        return self.recurrence == RECURRENCE_DAILY or self.requested

    @property
    def on_demand(self) -> bool:
        """Whether a request is the thing that makes this load want to run.

        Also the gate on the *deadline*-shaped settings specifically:
        ``run_within_hours`` needs a target run length to count down against,
        which a surplus load does not have. Use :attr:`armable` for the
        question "does the Requested switch mean anything here".
        """
        return self.recurrence == RECURRENCE_ON_DEMAND

    @property
    def on_surplus(self) -> bool:
        """Whether this load's run time is derived from the plan's exports.

        The gate on everything a surplus load has taken over: its operating
        hours, time window and deadline are computed in :mod:`surplus`, so the
        entities offering them report unavailable rather than accepting values
        nothing will read.
        """
        return self.recurrence == RECURRENCE_SURPLUS

    @property
    def armable(self) -> bool:
        """Whether the Requested switch decides if this load runs.

        True for both request-driven recurrences. What differs is how the
        request *ends*: an on-demand run disarms itself once it has had its
        operating hours, while a surplus run keeps taking whatever is spare
        until either its energy cap is met or the switch is turned off.
        """
        return self.on_demand or self.on_surplus

    @property
    def metered(self) -> bool:
        """Whether this load has a meter of its own to be believed.

        Gated on the configured sensor and deliberately *not* on
        :attr:`running_source` or :attr:`is_running`: without a power sensor
        those fall back to the control entity, which is on because we just
        switched it on. That reading is our own intent handed back to us, and
        every completion rule below would be reading its own command as
        evidence -- "still drawing" would be permanently true, and the load
        would hold past its target every single run.
        """
        return self.power_sensor is not None

    @property
    def idle_threshold_w(self) -> float:
        """The reading below which this load counts as finished, not merely quiet.

        Clamped below the running threshold: an idle floor at or above the
        point where the load counts as running would make a load both running
        and finished at once, and the number entity's own maximum can be
        outrun by a later change to nominal_power_w.
        """
        return min(self.idle_power_w, self.running_threshold_w)

    def idle_window(self, step_minutes: int) -> timedelta:
        """How long a load must read idle before the run is called finished.

        One optimisation timestep by default, which is comfortably longer than
        the gaps a program leaves between its own phases and short enough to
        free the appliance within one planning cycle.
        """
        minutes = self.idle_minutes if self.idle_minutes > 0 else step_minutes
        return timedelta(minutes=minutes)

    @property
    def surplus_run_floor_w(self) -> float:
        """The power below which giving this load a timestep achieves nothing.

        A semi-continuous load can only be off or at its nominal power, so a
        timestep offering less than that is useless to it. One that may
        modulate can make do with anything down to its floor.
        """
        if self.semi_continuous:
            return self.nominal_power_w
        return self.minimum_power_w

    def delivered_energy_wh(self, now: datetime) -> float:
        """Energy delivered since the current request was armed.

        Exact for a semi-continuous load, which draws its nominal power or
        nothing. For one that may modulate this over-states delivery whenever
        it runs below nominal, so the cap is reached early and the load stops
        short rather than over-running -- the safe direction, and the reason
        the energy cap is documented as approximate for variable-power loads.
        """
        hours = self.elapsed_since_request(now).total_seconds() / 3600
        return hours * self.nominal_power_w

    def remaining_energy_wh(self, now: datetime) -> float | None:
        """What is left of an energy cap, or None when there is no cap."""
        if self.energy_needed_kwh <= 0:
            return None
        return max(self.energy_needed_kwh * 1000 - self.delivered_energy_wh(now), 0.0)

    @property
    def deadline_at(self) -> datetime | None:
        """When a pending request must be finished, or None for no deadline.

        Anchored at ``requested_at`` and *not* recomputed from "now": a
        deadline re-derived on every optimisation would slide forward with the
        clock and never arrive.
        """
        if not self.on_demand or not self.requested:
            return None
        if self.requested_at is None or self.run_within_hours <= 0:
            return None
        return self.requested_at + timedelta(hours=self.run_within_hours)

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
        return state_to_watts(state)

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

    def elapsed_since_request(self, now: datetime) -> timedelta:
        """Runtime since the pending request was armed.

        A run already in progress when the request arrived counts only from
        the request onwards: the user asked for a *fresh* run, and crediting
        minutes that predate the request would disarm it early.
        """
        if self.requested_at is None:
            return timedelta()
        total = self.request_runtime
        if self.running_since is not None:
            total += now - max(self.running_since, self.requested_at)
        return total

    def elapsed_commanded(self, now: datetime) -> timedelta:
        """How long this load has been told to run since the request was armed.

        The clock an on-demand run is judged against, on both sides of the
        question: it decides when the run is over *and* how many timesteps the
        payload credits as already done, so the disarm and EMHASS can never
        disagree about how far along the same run is.
        """
        total = self.command_runtime
        if self.commanded_since is not None:
            anchor = self.commanded_since
            if self.requested_at is not None:
                anchor = max(anchor, self.requested_at)
            total += now - anchor
        return total

    def elapsed_towards_target(self, now: datetime) -> timedelta:
        """Progress against whatever this load's target currently is.

        Commanded time for a pending on-demand run, measured time otherwise.
        The split is deliberate rather than an inconsistency. An on-demand run
        is a deal -- "you get 1.25 h of cheap electricity" -- and only the deal
        can say when it has been honoured; a daily load is asking a different
        question, "has this appliance done today's work", where a cycle the user
        started by hand counts and a commanded hour that never reached the
        appliance does not.

        A surplus load keeps measured time too: its target is an energy cap, not
        a run length, and commanded time cannot be compared against it.

        An armed request with no anchor falls back to the day. That pairing is
        not reachable through :meth:`request`, but a request restored from
        before anchors existed is exactly that, and per-day accounting is the
        behaviour it was armed under.
        """
        if self.requested and self.requested_at is not None:
            if self.on_demand:
                return self.elapsed_commanded(now)
            if self.armable:
                return self.elapsed_since_request(now)
        return self.elapsed_today(now)

    def observe_command(self, commanded: bool, now: datetime) -> None:
        """Fold the executor's decision for this load into the commanded clock.

        Called with what was actually commanded, from the one place that knows
        it was applied -- a decision computed while the control switch is off
        commands nothing, and must not be credited as run time.
        """
        self.commands_seen = True
        if commanded:
            if self.commanded_since is None:
                self.commanded_since = now
            return
        if self.commanded_since is None:
            return
        anchor = self.commanded_since
        if self.requested_at is not None:
            anchor = max(anchor, self.requested_at)
        self.command_runtime += max(now - anchor, timedelta())
        self.commanded_since = None
        # Not being asked to run is not evidence of having finished.
        self.idle_since = None

    @property
    def is_commanded(self) -> bool:
        return self.commanded_since is not None

    @property
    def idle_counts(self) -> bool:
        """Whether an idle reading right now could mean the appliance finished.

        Only while the load is being told to run: a gap the plan itself left
        between two blocks of the same run is idle for a reason that has
        nothing to do with the appliance. The "not heard from yet" case counts
        as well, so that the readings arriving between a restart and the first
        executor apply cannot discard an idle window restored from before it.
        """
        return self.is_commanded or not self.commands_seen

    def observe_power(self, watts: float | None, now: datetime) -> None:
        """Fold a power reading into the runtime accumulator."""
        if watts is None:
            # Treat an unavailable sensor as "stop counting" rather than
            # guessing; an unbounded open interval would inflate runtime. It is
            # not an idle reading either -- a plug that dropped off the network
            # tells us nothing about whether the appliance behind it finished.
            self._stop(now)
            self.idle_since = None
            return

        if watts >= self.running_threshold_w:
            self.seen_running = True
            if self.running_since is None:
                self.running_since = now
                self.off_since = None
        else:
            self._stop(now)

        if watts >= self.idle_threshold_w or not self.idle_counts:
            # Above the idle floor the appliance is doing *something*, even if
            # too little to count as running -- a drying phase is not a finished
            # program. Uncommanded, an idle reading is only what we asked for.
            self.idle_since = None
        elif self.idle_since is None:
            self.idle_since = now

    def idle_for(self, now: datetime) -> timedelta:
        """How long this load has read idle while being told to run."""
        if self.idle_since is None:
            return timedelta()
        return max(now - self.idle_since, timedelta())

    def _stop(self, now: datetime) -> None:
        if self.running_since is None:
            return
        self.runtime_today += now - self.running_since
        if self.requested_at is not None:
            self.request_runtime += now - max(self.running_since, self.requested_at)
        self.running_since = None
        self.off_since = now

    def reset_day(self, now: datetime) -> None:
        """Start a new day's runtime accounting.

        EMHASS has no notion of a day boundary for
        ``def_current_operating_timesteps``; it is the caller's job to reset it,
        otherwise yesterday's hours keep suppressing today's schedule.

        A pending request is deliberately untouched -- neither the flag, the
        anchor, nor its progress. Only the calendar-day counter restarts.
        """
        was_running = self.is_running
        self._stop(now)
        self.runtime_today = timedelta()
        self.runtime_day = dt_util.as_local(now).date()
        if was_running:
            # _stop closed the open span to bank it against yesterday; the load
            # itself is still on, so today's span starts now. Without this the
            # accumulator would sit idle until the source next changes state --
            # which for a control entity left on can be hours. _stop also set
            # off_since as a side effect of closing the span -- undo that, since
            # the load never actually went off.
            self.running_since = now
            self.off_since = None

    # -- requests -------------------------------------------------------------

    def request(self, now: datetime) -> None:
        """Arm an on-demand run, anchoring any deadline at ``now``.

        The anchor has to be set wherever the flag is, which is why this is a
        method rather than an attribute assignment: a bare ``requested = True``
        from the switch would leave a deadline with nothing to count from.
        """
        self.requested = True
        self.requested_at = now
        self.request_runtime = timedelta()
        # A fresh run is judged on its own evidence. Carrying the previous run's
        # commanded time or its latch forward would have the new request start
        # already spent, or already believing an appliance it has not yet
        # switched on has drawn power.
        self.command_runtime = timedelta()
        self.commanded_since = now if self.is_commanded else None
        self.seen_running = False
        self.idle_since = None

    def cancel(self, reason: str = COMPLETION_CANCELLED, now: datetime | None = None) -> None:
        """Clear a request, fulfilled or abandoned, along with its anchor.

        The reason is recorded outside everything this clears, because "why did
        it stop" is asked precisely when the switch is already off. Defaults to
        the user's own doing: every caller that is *not* the user reaching for
        the switch has a more specific reason to pass.
        """
        self.requested = False
        self.requested_at = None
        self.request_runtime = timedelta()
        self.command_runtime = timedelta()
        self.commanded_since = None
        self.seen_running = False
        self.idle_since = None
        self.last_completion_reason = reason
        self.last_completion_at = now or dt_util.utcnow()

    def force_run(self, now: datetime | None = None) -> None:
        """Arm a direct run right now, bypassing both the plan and any request.

        Unlike ``request``, this has no anchor of its own: it disarms against
        ``elapsed_today`` in :meth:`check_auto_disarm`, the same target EMHASS
        itself is solving the load's day around, rather than a fresh window
        counted from the button press.

        ``forced_at`` is only a backstop for that rule -- see
        :meth:`check_auto_disarm` -- not a second target.
        """
        self.mode = LOAD_MODE_FORCE_ON
        self.forced_at = now or dt_util.utcnow()
        if not self.requested:
            # Same reasoning as request(): a run is judged on its own evidence.
            # Guarded, because a forced top-up of an already-armed request must
            # not throw away what that request has seen.
            self.seen_running = False
            self.idle_since = None

    def in_completion_hold(self, now: datetime) -> bool:
        """Whether this run is over on paper but the appliance is still drawing.

        The reason the run is not simply ended at ``operating_hours``: the plan
        stops asking for the load the moment its target is met, and obeying
        that would cut power mid-program to any appliance whose cycle outlasts
        the hours it was given. The executor keeps such a load on -- for at most
        one more timestep, after which :meth:`check_auto_disarm` takes it away
        as ``cut_short``.

        Only a metered load can be held. Without a meter "still drawing" is our
        own command read back, so every run would hold to the hard cut.
        """
        return (
            self.on_demand
            and self.requested
            and self.metered
            and self.is_running
            and self.elapsed_towards_target(now) >= timedelta(hours=self.operating_hours)
        )

    def check_auto_disarm(self, now: datetime, step_minutes: int = DEFAULT_TIME_STEP) -> bool:
        """End a finished request or forced run. Returns True if either changed.

        No user automation can build this reliably: it needs the same elapsed
        accounting the payload itself is built from.

        An on-demand run ends on whichever of these comes first:

        * the appliance reads idle for a whole idle window, having been seen to
          draw power at least once -- the normal ending, and the only one that
          is right without anyone configuring a run length correctly;
        * the run had its whole target and the appliance is not drawing;
        * the run had its whole target and never drew a watt -- ``never_started``,
          the one ending that means the user's dishes are still dirty;
        * one timestep past the target, whatever the appliance is doing --
          ``cut_short``, the signal that operating_hours is set shorter than
          the program actually needs.

        A daily load has none of this: it has no request to clear, and "has
        today's work been done" is a question about the appliance, not about a
        deal, so it stays on measured time. A surplus load keeps its energy cap
        for the same reason its idle time proves nothing -- being idle under a
        cloud is what a surplus load *does*.
        """
        changed = False
        target = timedelta(hours=self.operating_hours)
        idle_done = (
            self.metered
            and self.seen_running
            and self.idle_for(now) >= self.idle_window(step_minutes)
        )

        if self.mode == LOAD_MODE_FORCE_ON and (
            self.elapsed_today(now) >= target
            or idle_done
            # Backstop. Without it a forced run on a metered load that stalls
            # short of its target -- a duty-cycling appliance, or one that never
            # started at all -- holds the load on indefinitely, with no way back
            # to auto short of restarting Home Assistant.
            or (
                self.forced_at is not None
                and now - self.forced_at >= target + timedelta(minutes=step_minutes)
            )
        ):
            self.mode = LOAD_MODE_AUTO
            self.forced_at = None
            changed = True

        if self.on_demand and self.requested:
            # Not elapsed_commanded directly: a request restored from before
            # anchors existed has no anchor for a commanded clock to start
            # from, and elapsed_towards_target keeps the per-day accounting
            # that such a request was armed under.
            elapsed = self.elapsed_towards_target(now)
            reason: str | None = None
            if elapsed >= target + timedelta(minutes=step_minutes):
                reason = COMPLETION_CUT_SHORT
            elif idle_done:
                # Idle *and* fully paid up is not an early finish, just a
                # punctual one.
                reason = COMPLETION_COMPLETED if elapsed >= target else COMPLETION_FINISHED_EARLY
            elif elapsed >= target:
                if self.metered and not self.seen_running:
                    reason = COMPLETION_NEVER_STARTED
                elif not (self.metered and self.is_running):
                    reason = COMPLETION_COMPLETED
            if reason is not None:
                self.cancel(reason, now)
                changed = True

        if self.on_surplus and self.requested and self.remaining_energy_wh(now) == 0:
            self.cancel(COMPLETION_COMPLETED, now)
            changed = True
        return changed

    # -- payload --------------------------------------------------------------

    # All three of these are EMHASS inputs, and EMHASS 0.18 validates them as
    # non-negative -- one negative entry raises ValueError inside the add-on
    # and fails the whole optimisation with a 500, taking every other load and
    # the battery down with it. They can go negative by a single step without
    # anything being wrong: ``running_since``/``off_since`` come from Home
    # Assistant state-change timestamps, while ``now`` is captured once at the
    # top of a refresh, so a load that switches on *during* the seconds a run
    # takes to build its payload is stamped slightly after that ``now``. Floor
    # division then turns a delta of a few hundred milliseconds the wrong way
    # into -1. Clamping here rather than skewing ``now`` keeps the elapsed
    # helpers honest about what they measured; "0 whole timesteps" is also the
    # true answer in every case this catches.

    def completed_timesteps(self, now: datetime, step_minutes: int) -> int:
        elapsed = self.elapsed_towards_target(now).total_seconds() / 60
        return max(0, int(elapsed // step_minutes))

    def continuous_on_timesteps(self, now: datetime, step_minutes: int) -> int:
        """How many whole timesteps this load has been continuously on.

        0 until a transition is actually observed -- like ``runtime_today``,
        this does not survive a restart, and starting from 0 is the safe
        direction: it under-, never over-, counts an existing on-time streak.
        """
        if self.running_since is None:
            return 0
        return max(0, int((now - self.running_since).total_seconds() / 60 // step_minutes))

    def continuous_off_timesteps(self, now: datetime, step_minutes: int) -> int:
        """How many whole timesteps this load has been continuously off.

        Same restart caveat as :meth:`continuous_on_timesteps`.
        """
        if self.off_since is None:
            return 0
        return max(0, int((now - self.off_since).total_seconds() / 60 // step_minutes))

    def thermal_config(self, current_temperature: float | None) -> ThermalConfig | None:
        """This load's thermal model, or None for an ordinary load.

        Built fresh on every request from the entity-owned live values, the
        same way every other optimiser input is read at request time.
        """
        if not self.is_thermal:
            return None
        return ThermalConfig(
            sense=self.sense,
            heating_rate=self.heating_rate,
            cooling_constant=self.cooling_constant,
            thermal_inertia=self.thermal_inertia,
            comfort_temperature=self.comfort_temperature,
            setback_temperature=self.setback_temperature,
            max_temperature=self.max_temperature,
            comfort_start=self.comfort_start,
            comfort_end=self.comfort_end,
            current_temperature=current_temperature,
        )

    def to_load(
        self,
        now: datetime,
        step_minutes: int,
        current_temperature: float | None = None,
    ) -> DeferrableLoad:
        """Project the live state into what the payload builder consumes."""
        windowed = self.use_time_window
        surplus = self.on_surplus
        budget = self.surplus_budget
        # The ceiling the budget's hours were actually computed against --
        # equal to nominal_power_w for a semi-continuous load, and clamped
        # down to the best surplus this run actually saw for one that may
        # modulate. Sending anything else would let EMHASS ask for power the
        # budget never accounted for. See surplus.allocate.
        nominal_power_w = (
            budget.nominal_w if surplus and not budget.is_empty else self.nominal_power_w
        )

        return DeferrableLoad(
            subentry_id=self.subentry_id,
            name=self.name,
            nominal_power_w=nominal_power_w,
            minimum_power_w=self.minimum_power_w,
            # A surplus load's run time is not a setting; it is however many
            # hours the exported power in the last plan can actually feed.
            operating_hours=budget.hours if surplus else self.operating_hours,
            earliest_start=self.earliest_start if windowed and not surplus else None,
            latest_end=self.latest_end if windowed and not surplus else None,
            # The window the surplus itself occupies, as absolute instants
            # rather than a recurring wall-clock pair: it is derived fresh from
            # this plan and means nothing tomorrow. Clamping to it is what keeps
            # EMHASS from placing the hours at 03:00 -- it has no idea they came
            # from sunshine, only that the load must run somewhere.
            start_at=budget.window_start if surplus else None,
            end_at=budget.window_end if surplus else None,
            # Independent of the window switch: a deadline is a property of one
            # request, the window a standing property of the appliance and the
            # household. Both may apply, and the payload intersects them.
            deadline_at=self.deadline_at,
            semi_continuous=self.semi_continuous,
            single_constant=self.single_constant,
            startup_penalty=self.startup_penalty,
            max_startups=self.max_startups,
            minimum_on_time_minutes=self.minimum_on_time_minutes,
            minimum_off_time_minutes=self.minimum_off_time_minutes,
            current_on_timesteps=self.continuous_on_timesteps(now, step_minutes),
            current_off_timesteps=self.continuous_off_timesteps(now, step_minutes),
            # Enabled/disabled and armed/unarmed are independent gates; both
            # must pass for this load to ask EMHASS for any run time. It is
            # still described in the payload either way, parked at zero hours,
            # so that its deferrable number never moves. A surplus load with
            # nothing to spare is parked for the same reason a disabled one
            # is: sending zero hours *and* a window and a running state is the
            # kind of contradiction EMHASS answers by declaring the whole
            # problem infeasible.
            wants_to_run=self.enabled and self.participates and not (surplus and budget.is_empty),
            # A load that is already running should not be charged a startup
            # penalty again, nor be re-scheduled for work it has done today.
            current_state=self.is_running or self.mode == LOAD_MODE_FORCE_ON,
            current_power_w=nominal_power_w if self.is_running else 0.0,
            # Completed work is measured against an operating-hours target,
            # which a thermal load does not have -- its temperature *is* its
            # state, reported through start_temperature instead.
            #
            # Nor does a surplus load, and here reporting progress would be
            # actively wrong. Its budget is derived from the *forward* surplus,
            # which shrinks on its own as the day's sunny timesteps fall behind
            # the horizon -- so the hours being sent are already "what is left".
            # Letting EMHASS subtract completed timesteps as well double-counts
            # every hour served: a pool that has run two of four surplus hours
            # would be told it needs two more and has already done two, and
            # would stop at noon with half the sun still ahead of it.
            completed_timesteps=(
                0 if self.is_thermal or surplus else self.completed_timesteps(now, step_minutes)
            ),
            thermal=self.thermal_config(current_temperature),
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
        # Remembered from the last coordinator-driven disarm check so the state
        # listener -- which has no access to the config -- judges idle windows
        # against the same timestep the optimisation runs on.
        self._step_minutes = DEFAULT_TIME_STEP

    # -- lifecycle ------------------------------------------------------------

    def sync(self) -> None:
        """Rebuild the registry from the entry's subentries."""
        seen: set[str] = set()
        for subentry_id, subentry in self.entry.subentries.items():
            if subentry.subentry_type not in LOAD_SUBENTRY_TYPES:
                continue
            seen.add(subentry_id)
            data = dict(subentry.data)
            if (existing := self._loads.get(subentry_id)) is None:
                self._loads[subentry_id] = _from_subentry(
                    self.hass, subentry_id, subentry.subentry_type, subentry.title, data
                )
            else:
                _apply_subentry_fields(existing, subentry.subentry_type, subentry.title, data)

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

    def index_of(self, subentry_id: str) -> int | None:
        """This load's ``P_deferrable{k}`` number in EMHASS.

        Every configured load is sent on every run, parked at zero hours when
        it wants nothing (see ``payload._park``), so a load's position in the
        order above *is* its deferrable number -- fixed for as long as the load
        exists, and knowable before any optimisation has run.

        It still moves if a load is added or removed, since the order is by
        name; there is no way around that short of storing an assignment, and a
        load being added is a moment the user is present for.
        """
        for index, load in enumerate(self.all()):
            if load.subentry_id == subentry_id:
                return index
        return None

    @property
    def has_thermal(self) -> bool:
        """Whether any load needs an outdoor temperature to be modelled."""
        return any(load.is_thermal for load in self._loads.values())

    @property
    def has_surplus(self) -> bool:
        return any(load.on_surplus for load in self._loads.values())

    def surplus_indices(self, load_order: list[str]) -> list[int]:
        """Which ``P_deferrable{k}`` columns belong to surplus loads."""
        return [
            index
            for index, subentry_id in enumerate(load_order)
            if (load := self._loads.get(subentry_id)) is not None and load.on_surplus
        ]

    def surplus_series(self, plan: Plan | None, load_order: list[str]) -> Series:
        """Spare PV in ``plan``, before any surplus load consumes it."""
        if plan is None:
            return Series.empty()
        return surplus_series(plan, self.surplus_indices(load_order))

    def apply_surplus(
        self, plan: Plan | None, load_order: list[str], now: datetime, step_minutes: int
    ) -> None:
        """Re-derive every surplus load's budget from the previous plan.

        Called once per run, immediately before the payload reads the loads --
        the budget is an input to *this* request computed from the answer to the
        *last* one, which is what makes this a single solve per cycle rather
        than two (see the module docstring in :mod:`surplus`).

        Everything is recomputed from scratch, including a reset to an empty
        budget for loads that get nothing. Carrying a stale budget forward
        would leave a load asking for hours the sun no longer supports, and the
        one case where that happens constantly is the most dangerous one: no
        plan at all, immediately after a restart, when the honest answer is
        "ask for nothing until we have seen a plan".
        """
        # Priority order, not registry order: lower surplus_priority claims the
        # series first. self.all() is already name-ordered, and sorted() is
        # stable, so loads left at the default priority keep exactly the old
        # tie-break -- this changes nothing until someone sets a priority.
        surplus_loads = sorted(
            (load for load in self.all() if load.on_surplus),
            key=lambda load: load.surplus_priority,
        )
        for load in surplus_loads:
            load.surplus_budget = SurplusBudget()
        if plan is None or not surplus_loads:
            return

        step = timedelta(minutes=step_minutes)
        series = surplus_series(plan, self.surplus_indices(load_order))
        reserved = battery_reserved_series(plan)
        # Only what is still ahead of us. A timestep already elapsed cannot be
        # scheduled into, and counting it would inflate the budget by however
        # much sun has already been and gone.
        #
        # The cutoff is the row whose interval *contains* now, not the first
        # row strictly after it. The previous plan's rows are timestamped
        # against whatever instant *that* run captured as its own now, a few
        # seconds to either side of this run's -- so a strict >= comparison
        # drops the row that actually covers "now" on almost every cycle,
        # pushing every surplus load's window a full step later than the plan
        # itself says it should be, indefinitely (see surplus_loads.md).
        points = list(series)
        cutoff = next((point.time for point in reversed(points) if point.time <= now), now)
        series = Series(point for point in points if point.time >= cutoff)
        reserved = Series(point for point in reserved if point.time >= cutoff)

        specs = [
            SurplusSpec(
                subentry_id=load.subentry_id,
                nominal_w=load.nominal_power_w,
                run_floor_w=load.surplus_run_floor_w,
                semi_continuous=load.semi_continuous,
                headroom_w=load.surplus_headroom_w,
                max_energy_wh=load.remaining_energy_wh(now),
                start_asap=load.start_asap,
            )
            # surplus_loads is already priority-ordered, above.
            for load in surplus_loads
            if load.enabled and load.requested
        ]
        budgets = allocate(series, specs, step, reserved=reserved)
        for load in surplus_loads:
            if (budget := budgets.get(load.subentry_id)) is not None:
                load.surplus_budget = budget

    def to_loads(self, now: datetime, step_minutes: int) -> list[DeferrableLoad]:
        return [
            load.to_load(now, step_minutes, current_temperature=self.room_temperature(load))
            for load in self.all()
        ]

    def room_temperature(self, load: DeferrableRuntime) -> float | None:
        """The thermal load's current temperature, in Celsius, or None.

        Read live on every request so the plan starts from the actual room.
        None (no sensor configured, or it is unavailable) omits
        ``start_temperature`` from the payload and lets EMHASS fall back to its
        own default rather than planning from an invented reading.
        """
        if not load.is_thermal or not load.temperature_sensor:
            return None
        state = self.hass.states.get(load.temperature_sensor)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
            _LOGGER.warning(
                "Temperature sensor %s for %s is unavailable; EMHASS will use "
                "its default start temperature",
                load.temperature_sensor,
                load.name,
            )
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        # EMHASS's thermal model works in Celsius; a Fahrenheit sensor read
        # raw would ask for a 70-degree living room.
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit and unit != UnitOfTemperature.CELSIUS:
            with suppress(HomeAssistantError):
                value = TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
        return value

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

    def check_auto_disarm(self, now: datetime, step_minutes: int = DEFAULT_TIME_STEP) -> None:
        """Clear every request and forced run that has had what it asked for.

        Called once per run for *every* load, which is the whole point: this
        used to ride along inside :meth:`assume_from_plan`, and so inherited
        all three of its guards -- no previous plan, a load with a real
        running_source, a load absent from that plan's load order -- none of
        which has anything to do with whether a request is finished. The only
        other caller is the source listener, and that one fires solely on an
        observed running-to-stopped transition. Between them, a load that had
        a power sensor and never actually ran could not disarm at all: a forced
        run on it stayed armed indefinitely, with no way back to auto short of
        restarting Home Assistant.

        Safe to call unconditionally because the check is idempotent and only
        ever clears -- a request short of its target is left exactly as it was.
        """
        self._step_minutes = step_minutes
        for load in self._loads.values():
            if load.check_auto_disarm(now, step_minutes):
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
                        load.check_auto_disarm(now, self._step_minutes)
                    load.notify()
                elif load.idle_since is not None and load.check_auto_disarm(
                    now, self._step_minutes
                ):
                    # An idle window ends without any transition to fire on: the
                    # appliance stopped long ago and the meter has been
                    # reporting the same nothing ever since. Re-checking on each
                    # reading while idle is what notices the window elapsing,
                    # rather than waiting for the next optimisation to come
                    # round to it.
                    load.notify()

    @callback
    def _async_midnight(self, now: datetime) -> None:
        for load in self._loads.values():
            load.reset_day(now)
            load.notify()
        _LOGGER.debug("Reset deferrable load runtime counters for the new day")


# --- helpers -----------------------------------------------------------------


def _from_subentry(
    hass: HomeAssistant, subentry_id: str, subentry_type: str, title: str, data: dict
) -> DeferrableRuntime:
    load = DeferrableRuntime(subentry_id=subentry_id, name=title)
    _apply_subentry_fields(load, subentry_type, title, data)
    # Seed the entity-owned values; a restored entity overwrites these on start.
    load.nominal_power_w = float(data.get(CONF_NOMINAL_POWER, 0) or 0)
    load.minimum_power_w = float(data.get(CONF_MINIMUM_POWER, 0) or 0)
    load.operating_hours = float(data.get(CONF_OPERATING_HOURS, 0) or 0)
    load.earliest_start = _parse_time(
        hass, data.get(CONF_EARLIEST_START), load=title, field="earliest start"
    )
    load.latest_end = _parse_time(hass, data.get(CONF_LATEST_END), load=title, field="latest end")
    load.use_time_window = load.earliest_start is not None or load.latest_end is not None
    load.semi_continuous = bool(data.get(CONF_SEMI_CONTINUOUS, True))
    load.single_constant = bool(data.get(CONF_SINGLE_CONSTANT, False))
    load.startup_penalty = float(data.get(CONF_STARTUP_PENALTY, 0) or 0)
    load.max_startups = int(data.get(CONF_MAX_STARTUPS, 0) or 0)
    load.minimum_on_time_minutes = float(data.get(CONF_MINIMUM_ON_TIME, 0) or 0)
    load.minimum_off_time_minutes = float(data.get(CONF_MINIMUM_OFF_TIME, 0) or 0)
    load.surplus_headroom_w = float(
        data.get(CONF_SURPLUS_HEADROOM, DEFAULT_SURPLUS_HEADROOM_W) or 0
    )
    load.energy_needed_kwh = float(data.get(CONF_ENERGY_NEEDED, 0) or 0)
    load.surplus_priority = int(data.get(CONF_SURPLUS_PRIORITY, DEFAULT_SURPLUS_PRIORITY) or 0)
    # Asked when the load is added, because it decides which of the fields
    # above are even offered. Entity-owned from then on, like the rest of these
    # -- the restored select overwrites this on start.
    recurrence = data.get(CONF_RECURRENCE)
    load.recurrence = recurrence if recurrence in RECURRENCES else RECURRENCE_DAILY
    if load.is_thermal:
        load.heating_rate = float(data.get(CONF_HEATING_RATE, DEFAULT_HEATING_RATE))
        load.cooling_constant = float(data.get(CONF_COOLING_CONSTANT, DEFAULT_COOLING_CONSTANT))
        load.thermal_inertia = float(data.get(CONF_THERMAL_INERTIA, DEFAULT_THERMAL_INERTIA))
        load.comfort_temperature = float(
            data.get(CONF_COMFORT_TEMPERATURE, DEFAULT_COMFORT_TEMPERATURE)
        )
        load.setback_temperature = float(
            data.get(CONF_SETBACK_TEMPERATURE, DEFAULT_SETBACK_TEMPERATURE)
        )
        load.max_temperature = float(data.get(CONF_MAX_TEMPERATURE, DEFAULT_MAX_TEMPERATURE))
        load.comfort_start = (
            _parse_time(hass, data.get(CONF_COMFORT_START), load=title, field="comfort start")
            or DEFAULT_COMFORT_START
        )
        load.comfort_end = (
            _parse_time(hass, data.get(CONF_COMFORT_END), load=title, field="comfort end")
            or DEFAULT_COMFORT_END
        )
    return load


def _apply_subentry_fields(
    load: DeferrableRuntime, subentry_type: str, title: str, data: dict
) -> None:
    """Refresh the fields the subentry owns, leaving entity-owned ones alone.

    Everything the optimiser is *told* -- powers, hours, window, semi-cont,
    startup limits, comfort band -- is entity-owned so an automation can change
    it without reloading the entry, which is why none of it is refreshed here.
    The subentry's copy is the value a newly created load starts from, and the
    subentry form stops offering it once the load exists.
    """
    load.name = title
    load.load_type = (
        LOAD_TYPE_THERMAL if subentry_type == SUBENTRY_TYPE_THERMAL else LOAD_TYPE_STANDARD
    )
    load.power_sensor = data.get(CONF_POWER_SENSOR) or None
    load.control_entity = supported_control_entity(data.get(CONF_CONTROL_ENTITY))
    load.temperature_sensor = data.get(CONF_TEMPERATURE_SENSOR) or None
    load.sense = data.get(CONF_SENSE) or SENSE_HEAT


def supported_control_entity(entity_id: str | None) -> str | None:
    """The stored control entity, unless it is one we must not drive.

    The form only offers :data:`CONTROL_ENTITY_DOMAINS` now, but subentries
    created while it also offered scripts still hold one. Ignoring it here
    covers both uses in a single place: the executor stops re-firing the
    script on every apply, and ``running_source`` stops reporting a load as
    idle purely because its script is not currently executing. The user is
    told, rather than left wondering -- see ISSUE_SCRIPT_CONTROL_ENTITY.
    """
    if not entity_id:
        return None
    if entity_id.partition(".")[0] not in CONTROL_ENTITY_DOMAINS:
        _LOGGER.warning(
            "Ignoring control entity %s: only %s can be switched on and off to follow a plan",
            entity_id,
            " and ".join(CONTROL_ENTITY_DOMAINS),
        )
        return None
    return entity_id


def _parse_time(hass: HomeAssistant, value, *, load: str, field: str) -> time | None:
    """A stored window/comfort time, or None if it will not parse.

    Unreadable is treated as unset -- the load keeps its default window or
    comfort band -- rather than taking ``async_setup_entry`` down over one
    corrupted subentry. ``load``/``field`` are what turn that into a repair the
    user can act on; see :mod:`.stored_time`.
    """
    return parse_stored_time(hass, value, label=f"{load}: {field}")


def resolve_should_run(mode: str, scheduled: bool) -> bool:
    """Combine the plan's answer with any manual override.

    Only ``force_on`` exists: it can only ask for *more* than the plan already
    accounts for, which EMHASS's own conservatism already tolerates. Forcing a
    load *off* would leave the rest of the plan solved as if it were still
    running, which is why that mode was removed -- a user who wants a load
    left out of planning turns off its Enabled switch instead, which actually
    feeds back into what EMHASS solves.
    """
    if mode == LOAD_MODE_FORCE_ON:
        return True
    return scheduled

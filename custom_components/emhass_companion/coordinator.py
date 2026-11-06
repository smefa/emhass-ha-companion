"""Coordinator that gathers inputs, runs EMHASS, and holds the resulting plan."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from packaging.version import InvalidVersion, Version

from .api import EmhassClient, EmhassConnectionError, EmhassError
from .configuration import EmhassConfig, ProfileSelection
from .const import (
    ACTION_DAYAHEAD,
    ACTION_FORECAST_FIT,
    ACTION_MPC,
    COMPLETION_NEVER_STARTED,
    CONF_GROUP_LOAD_IDS,
    CONF_GROUP_MAX_POWER,
    CONF_GROUP_MUTUAL_EXCLUSION,
    DEFAULT_COST_FUN,
    DEFAULT_MIX_BETA,
    DEFAULT_SURPLUS_THRESHOLD_W,
    DEGRADED_OPTIM_STATUSES,
    DOMAIN,
    EMHASS_CONF_LOAD_FORECAST_METHOD,
    EMHASS_CONF_SENSOR_LOAD,
    EMHASS_CONF_SENSOR_PV,
    EMHASS_CONF_VAR_MODEL,
    END_SOC_OPTIMIZED,
    INFEASIBLE_CAUSE_CODES,
    ISSUE_HEAT_TOPOLOGY_REPLACES,
    ISSUE_LOAD_NEVER_STARTED,
    ISSUE_ML_FORECASTER_NOT_READY,
    ISSUE_OPTIMIZATION_INFEASIBLE,
    ISSUE_PLAN_DEGRADED,
    ISSUE_PLAN_SCHEMA,
    ISSUE_PV_TAIL_SHORT,
    ISSUE_RUN_FAILED,
    ISSUE_SOURCES_BLIND,
    ISSUE_TANK_BEHIND_SCHEDULE,
    LOAD_BOOTSTRAP_LOOKBACK,
    LOAD_FORECAST_METHOD_LIST,
    LOAD_FORECAST_METHOD_MLFORECASTER,
    LOAD_FORECAST_METHOD_TYPICAL,
    MIN_EMHASS_VERSION_CAPACITY_INTERVAL,
    MIN_EMHASS_VERSION_CHARGE_DERATING,
    MIN_EMHASS_VERSION_DEMAND_CHARGE,
    MIN_EMHASS_VERSION_DEMAND_COMPONENTS,
    MIN_EMHASS_VERSION_DEMAND_WINDOW,
    MIN_EMHASS_VERSION_SOC_FINAL_REWARD,
    ML_MIN_HISTORY_DAYS,
    MODE_AUTO,
    PROFILE_KEY_LOAD_SENSOR,
    PROFILE_KEY_PV_SOLCAST,
    PROFILE_KIND_LOAD,
    PROFILE_KIND_PRICE,
    PROFILE_KIND_PV,
    PROFILE_KIND_TEMPERATURE,
    SOLCAST_DAY3_ENTITY,
    SUBENTRY_TYPE_LOAD_GROUP,
    SUPPORTED_PLAN_SCHEMA_MAJOR,
)
from .deferrable import DeferrableRegistry, DeferrableRuntime, state_to_watts
from .effective_settings import ML_MODEL_SETTING_NAMES, ML_SETTING_NAMES, resolve_effective_settings
from .health import SourceHealth
from .infeasibility import Finding, Severity, diagnose, format_power, headline
from .models import (
    ROW_ZERO_FIRST,
    ROW_ZERO_LAST,
    ROW_ZERO_NEAREST,
    DeferrableLoad,
    DeferrableLoadGroup,
    LastRun,
    Plan,
    Point,
    Series,
    ceil_to_step,
    floor_to_step,
    row_zero_start,
)
from .network_calendar import HolidayCache, NetworkCalendar, NetworkCalendarError
from .payload import PayloadInputs, PayloadResult, build_payload
from .peaks import PeakTracker, days_in_current_period, effective_rate_per_kw
from .phase_guard import PhaseGuard
from .price_forecast import (
    PriceForecastClient,
    PriceForecastError,
    fill_with_forecast,
)
from .profiles import (
    Profile,
    ProfileError,
    async_load_profiles,
    async_resolve_series,
    resolve_network,
    resolve_settings,
)
from .smoothing import TimeWeightedAverage
from .surplus import seam_carry
from .terminal import (
    LOAD_SOURCE_LAST_PLAN,
    LOAD_SOURCE_PROFILE,
    EndSocDecision,
    annotate_final_reward,
    decide_end_soc,
)
from .terminal import (
    LOOKAHEAD as END_SOC_LOOKAHEAD,
)
from .util import schema_major, version_at_least
from .water_tank import TankShortfall

_LOGGER = logging.getLogger(__name__)

# How long an auto-triggered fit failure (or a not-enough-history verdict) is
# trusted before checking again -- long enough that a persistently-down EMHASS
# isn't hammered with an expensive fit request every MPC cycle, short enough
# that recorder history crossing ML_MIN_HISTORY_DAYS is noticed the same day.
_ML_FIT_RETRY_COOLDOWN = timedelta(hours=1)
# A fitted model's weights are frozen: predictions take fresh lag values as
# input but never learn from them. Refitting weekly keeps it following the
# season and the household's habits, and matches the weekly cycle the model
# itself learns.
_ML_REFIT_INTERVAL = timedelta(days=7)
_ML_STORE_VERSION = 1
_SOC_DAY_STORE_VERSION = 1
_SETTINGS_REFRESH_DELAY = 3

# How long the coordinator keeps building the load forecast itself after a run
# that left that job to EMHASS failed.
#
# A request carrying no ``load_power_forecast`` is the one shape that sends
# EMHASS back to Home Assistant for a day of history of its own accord (see
# _load_forecast_fallback), and everything about that fetch -- which entities,
# how much history, what it does when they are thin or missing -- lives in
# EMHASS's own persisted configuration, where this integration can neither see
# it nor fix it. When such a run fails there is therefore nothing to diagnose
# from here and no reason to expect the next one to fare better, so the load
# forecast comes back in-house until the cooldown lapses.
#
# An hour is several MPC cycles of running normally on our own series, while
# still re-testing EMHASS's own path often enough that a transient fault costs
# one degraded hour rather than lasting until the next restart. Each retry
# costs exactly one failed run, which is what stops this masking a real
# misconfiguration forever.
_NO_LIST_RETRY_COOLDOWN = timedelta(hours=1)


class WaterTankError(UpdateFailed):
    """Local tank data/configuration failure before contacting the backend."""


@dataclass(slots=True)
class DemandChargeComponentPricing:
    """One demand-charge component's priced rate (or why it isn't priced)."""

    name: str | None
    sheet_rate: float | None
    rate_basis: str
    aggregate: str
    n: int
    effective_rate_per_kw: float | None = None
    windowed: bool = False
    interval_timesteps: int = 1
    reason: str | None = None


@dataclass(slots=True)
class DemandChargePricing:
    """What a network profile's demand charge prices out to right now, and
    why not when it doesn't. See ``EmhassCoordinator.demand_charge_pricing``.

    For K=1 the top-level fields mirror component 0 (existing sensors /
    payload). For K>1, :attr:`components` holds every component and the
    top-level fields still mirror component 0 for singular entity values.
    """

    sheet_rate: float | None
    rate_basis: str
    aggregate: str
    n: int
    effective_rate_per_kw: float | None = None
    """The rate to actually send as ``capacity_cost_per_kw`` for K=1, or
    component 0's rate when K>1. None means no component is being priced
    right now -- see ``reason``."""
    windowed: bool = False
    """Whether component 0 needs ``capacity_charge_window`` alongside it."""
    interval_timesteps: int = 1
    """N for component 0's ``capacity_charge_interval_timesteps``."""
    reason: str | None = None
    components: list[DemandChargeComponentPricing] = field(default_factory=list)

    @property
    def component_count(self) -> int:
        return (
            len(self.components) if self.components else (1 if self.sheet_rate is not None else 0)
        )

    @property
    def effective_rates(self) -> list[float] | None:
        """K effective rates when every component is priced; else None."""
        if not self.components:
            return [self.effective_rate_per_kw] if self.effective_rate_per_kw is not None else None
        rates = [c.effective_rate_per_kw for c in self.components]
        if any(rate is None for rate in rates):
            return None
        return [float(rate) for rate in rates]  # type: ignore[arg-type]

    @property
    def interval_timesteps_list(self) -> list[int]:
        if self.components:
            return [c.interval_timesteps for c in self.components]
        return [self.interval_timesteps]

    @property
    def any_windowed(self) -> bool:
        if self.components:
            return any(c.windowed for c in self.components)
        return self.windowed


@dataclass(slots=True)
class DayRange:
    """Today's planned SOC low and high.

    The number on the status card is the more extreme of two readings, not a
    forecast that once looked likely:

    * **Past** — plan rows whose time has already arrived, latched so a later
      run (whose series no longer looks back at them) cannot forget a peak or
      a trough the day already reached.
    * **Future** — the current series, for the rest of today only. These are
      recomputed every run. A peak the optimiser has since revised away must
      not keep winning just because an earlier plan mentioned it.

    ``low`` / ``high`` are that published answer. ``past_low`` / ``past_high``
    are only the latched half; tomorrow's rows are in neither.
    """

    day: date
    past_low: Point | None = None
    past_high: Point | None = None
    low: Point | None = None
    high: Point | None = None


def _more_extreme(current: Point | None, point: Point, *, high: bool) -> Point:
    """The tighter of the two, keeping ``current`` when the values tie."""
    if current is None:
        return point
    if high:
        return point if point.value > current.value else current
    return point if point.value < current.value else current


def soc_percent_from_state(state: State) -> float | None:
    """A planned-SOC sensor state as a percentage, or None when it is not one."""
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def recorded_soc_points(states: Iterable[State], now: datetime) -> list[Point]:
    """Planned-SOC samples already in force today, from recorder states.

    A state still in force at local midnight has ``last_changed`` yesterday.
    It counts, timestamped at midnight, because that was the level the day
    opened on. Anything after ``now`` is not history.
    """
    start = dt_util.start_of_local_day(dt_util.as_local(now))
    now_utc = dt_util.as_utc(now)
    points: list[Point] = []
    for state in states:
        value = soc_percent_from_state(state)
        if value is None:
            continue
        when = state.last_changed
        if when < start:
            when = start
        if when > now_utc:
            continue
        points.append(Point(dt_util.as_utc(when), value))
    return points


def latch_past_soc(day_range: DayRange, points: Iterable[Point]) -> None:
    """Fold already-elapsed samples into the past latch. Does not publish."""
    for point in points:
        day_range.past_low = _more_extreme(day_range.past_low, point, high=False)
        day_range.past_high = _more_extreme(day_range.past_high, point, high=True)


def publish_soc_day_range(day_range: DayRange, series: Series, now: datetime) -> None:
    """Publish today's low/high from the past latch and this series.

    Rows at or before ``now`` join the past latch: the next run drops them.
    Later rows today count toward this answer only, so a revised forecast
    does not leave a peak or a trough behind. Rows outside the local day,
    including tomorrow's, are ignored.
    """
    start = dt_util.start_of_local_day(dt_util.as_local(now))
    end = start + timedelta(days=1)
    now_utc = dt_util.as_utc(now)
    low = day_range.past_low
    high = day_range.past_high
    for point in series:
        if point.time < start or point.time >= end:
            continue
        if point.time <= now_utc:
            day_range.past_low = _more_extreme(day_range.past_low, point, high=False)
            day_range.past_high = _more_extreme(day_range.past_high, point, high=True)
        low = _more_extreme(low, point, high=False)
        high = _more_extreme(high, point, high=True)
    day_range.low = low
    day_range.high = high


def plan_soc_ahead(series: Series, now: datetime) -> tuple[Point | None, Point | None]:
    """The (low, high) planned level from ``now`` to the end of the plan.

    The counterpart to DayRange: forward-looking only, so a trough or peak the
    plan has already passed does not count. The row in force at ``now`` does,
    so a plan that only falls from here reports the current level as its high.
    Ties keep the earlier row. Cheap enough to call at render time, which is
    what keeps it honest between optimisation runs.
    """
    now_utc = dt_util.as_utc(now)
    in_force: Point | None = None
    ahead: list[Point] = []
    for point in series:
        if point.time <= now_utc:
            in_force = point
        else:
            ahead.append(point)
    if in_force is not None:
        ahead.insert(0, Point(now_utc, in_force.value))
    low: Point | None = None
    high: Point | None = None
    for point in ahead:
        low = _more_extreme(low, point, high=False)
        high = _more_extreme(high, point, high=True)
    return low, high


def _point_to_json(point: Point | None) -> dict[str, Any] | None:
    if point is None:
        return None
    return {"time": point.time.isoformat(), "value": point.value}


def _point_from_json(raw: Any) -> Point | None:
    if not isinstance(raw, dict):
        return None
    when = dt_util.parse_datetime(str(raw.get("time") or ""))
    try:
        value = float(raw["value"])
    except (KeyError, TypeError, ValueError):
        return None
    if when is None:
        return None
    return Point(dt_util.as_utc(when), value)


@dataclass(slots=True)
class EmhassData:
    """Everything the entities render from."""

    plan: Plan | None = None
    last_run: LastRun | None = None
    buy_price: Series = field(default_factory=Series.empty)
    sell_price: Series = field(default_factory=Series.empty)
    pv_forecast: Series = field(default_factory=Series.empty)
    load_forecast: Series = field(default_factory=Series.empty)
    payload: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    defaulted_forecast_methods: frozenset[str] = frozenset()
    load_order: list[str] = field(default_factory=list)
    payload_order: list[str] = field(default_factory=list)
    """Subentry ids in the order of ``payload``'s real loads.

    Equal to ``load_order`` except after an infeasible run, which keeps the plan
    (and so ``load_order``) but reports the rejected request here."""
    extra_row_names: list[str] = field(default_factory=list)
    """Names of the internal rows appended to ``payload`` for later daily windows.

    Describes the request in ``payload`` (diagnostics only). The plan is already
    folded back to ``load_order``'s columns."""
    end_soc: EndSocDecision | None = None
    soc_day_range: DayRange | None = None
    last_action: str | None = None
    last_success: datetime | None = None
    price_source_end: datetime | None = None
    """How far the buy-price *source* reached this run, before ``buy_price``
    was trimmed to the horizon.

    The scheduler fires a day-ahead run when the price series gains a new day
    (``Scheduler._check_price_horizon``), and ``buy_price`` cannot answer that
    question: it is cut at ``horizon_end`` for publication, so once the source
    reaches past the horizon its end saturates there and grows only by one MPC
    interval per run -- never the six hours the trigger looks for. Recording
    the untrimmed end keeps the two concerns separate: ``buy_price`` describes
    what the plan covers, this describes what the market has published."""
    last_infeasibility: list[Finding] = field(default_factory=list)
    """What infeasibility.py found when this run came back infeasible.

    Empty after a feasible run, and also when the checks found nothing or
    could not run. Shown in diagnostics and on the Last request sensor."""

    def deferrable_index(self, subentry_id: str) -> int | None:
        """Position of a load in EMHASS's ``P_deferrable{k}`` numbering."""
        try:
            return self.load_order.index(subentry_id)
        except ValueError:
            return None


class EmhassCoordinator(DataUpdateCoordinator[EmhassData]):
    """Drives EMHASS on demand rather than on a fixed poll.

    ``update_interval`` is None: runs are triggered by the scheduler (day-ahead
    when prices land, MPC on an interval), by buttons, and by services.
    """

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EmhassClient,
        loads: DeferrableRegistry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=None,
        )
        self.client = client
        self.loads = loads
        self.config = EmhassConfig.from_entry(hass, entry)
        self.profiles: dict[str, Profile] = {}
        self.profile_errors: dict[str, str] = {}
        self.data = EmhassData()

        # The selected network (grid operator tariff) profile, resolved once
        # per profile/config reload rather than on every run: resolving it is
        # template rendering, not I/O, but the result is read far more often
        # (every band-sensor tick) than it changes. None when no network
        # profile is configured, or when the one configured no longer resolves
        # -- see _refresh_network_calendar.
        self.network_calendar: NetworkCalendar | None = None
        self.flat_demand_charge_per_kw: float = 0.0
        """The selected network profile's ``flat_demand_charge``, 0 when it has
        none. Sent as ``capacity_cost_per_kw`` unwindowed, on every run."""
        # Per-date red-day answers behind any `exclude_holidays` calendar,
        # shared between the band sensor and the demand-window predicate a
        # peak tracker is built against -- both need the same "is today a red
        # day" fact, and re-resolving it twice would double the
        # workday.check_date traffic for no benefit.
        self.holiday_cache = HolidayCache()
        # Only built when the user entered an eupowerprices.com key and zone.
        self.price_forecast: PriceForecastClient | None = (
            PriceForecastClient(async_get_clientsession(hass), self.config.price_forecast)
            if self.config.price_forecast
            else None
        )
        # Set once, right after the version probe in __init__.async_setup_entry
        # -- the same string _check_version already gates MIN_EMHASS_VERSION
        # against. None only for a coordinator built without going through
        # setup (unit tests), in which case the demand charge gate below is
        # conservative and treats it as unsupported.
        self.backend_version: str | None = None
        self._water_tank_plan_blocked: str | None = None
        # Set once, right after __init__.async_setup_entry builds them (may be
        # empty -- see _build_peak_trackers). Held here, not just on
        # EmhassRuntimeData, because demand_charge_pricing / _build need each
        # tracker's floor on every MPC run. peak_tracker aliases [0].
        self.peak_trackers: list[PeakTracker] = []
        self.peak_tracker: PeakTracker | None = None
        # Set by __init__.async_setup_entry when the grid step has a main fuse
        # and phase readings. Read here for the plan's import limit and by the
        # executor for its charge clamp -- one set of readings for both.
        self.phase_guard: PhaseGuard | None = None

        # Owned by the control switch and the mode select. Held here rather than
        # read back out of the state machine so the executor never has to parse
        # entity states to find out whether it is allowed to act.
        self.control_enabled = False
        self.system_mode = MODE_AUTO
        # Owned by the surplus threshold number. Reporting only: it decides
        # what the hub's surplus sensors call a window, and nothing else. Each
        # load budgets against its own power plus its own headroom instead.
        self.surplus_threshold_w = DEFAULT_SURPLUS_THRESHOLD_W
        # One row of the outgoing plan, kept to bridge the gap between "now"
        # and the next horizon's first row; see surplus.seam_carry. In-memory
        # only -- after a restart the first run simply has nothing to bridge
        # with, the same as the end-SOC anchor above.
        self._surplus_carry: Series = Series.empty()
        # Owned by the mix beta number. Weight given to the live PV/load
        # reading when it is blended into the current naive-mpc-optim forecast
        # step; see payload.build_payload and models.Series.blend_at. Also
        # sent as alpha/beta so EMHASS's own automatic load-forecast
        # correction (fired server-side, independently of blend_at) uses the
        # same weight instead of its hard-coded 50/50 default.
        self.mix_beta = DEFAULT_MIX_BETA
        # Owned by number.PeakTargetNumber. The v0.18.0 fallback ceiling for a
        # windowed demand charge that cannot be safely priced (see
        # demand_charge_pricing) -- kW, seeded from the peak tracker's current
        # aggregate the first time the entity is added, adjustable thereafter.
        # None until that entity exists (no demand charge configured, or the
        # platform has not set up yet), in which case the fallback simply does
        # not apply -- see payload.build_payload's capacity_ceilings.
        self.peak_target_kw: float | None = None
        # Owned by the cost function select. EMHASS's optimisation objective:
        # profit, cost, or self-consumption; see payload.build_payload.
        self.cost_fun = DEFAULT_COST_FUN
        # The last end-SOC decision, fed back into terminal.decide_end_soc as
        # the hysteresis anchor. In-memory only: after a restart the first run
        # simply computes fresh.
        self._end_soc: EndSocDecision | None = None
        # Today's planned SOC low/high -- see DayRange. The past half is
        # persisted and, after a restart, filled back in from the planned-SOC
        # sensor's recorder history so a peak that already happened is not
        # replaced by whatever the forward series still has ahead.
        self._soc_day_range: DayRange | None = None
        # How the backend rounds a launch time to plan row zero. EMHASS
        # defaults to "nearest" and only takes the setting from its add-on
        # config, so it is learned from the plans it returns.
        self._row_zero_mode = ROW_ZERO_NEAREST
        self._soc_history_merged_day: date | None = None
        self._soc_day_store: Store[dict[str, Any]] = Store(
            hass, _SOC_DAY_STORE_VERSION, f"{DOMAIN}_{entry.entry_id}_soc_day"
        )

        self._unsub_tick: Callable[[], None] | None = None
        self._unsub_settings_refresh: Callable[[], None] | None = None
        # Smooths the live PV entity the same way NetHouseLoadSensor smooths
        # its own reading: a single instantaneous sample at MPC-run time can
        # catch the entity mid-swing (a cloud edge, a noisy inverter
        # reading), which blend_at would then treat as the current state of
        # the world for the whole first forecast step.
        self._pv_live_average = TimeWeightedAverage()
        self._unsub_pv_live: Callable[[], None] | None = None

        # Watches every entity the config points at for one that has stopped
        # reporting. Constructed here rather than passed in so that nothing
        # holding a coordinator has to know it exists, but only started by
        # async_start_source_health -- a coordinator built and never started
        # (every unit test) watches nothing.
        self.health = SourceHealth(hass, entry)
        self._unsub_health: Callable[[], None] | None = None

        self._ml_trained_settings: dict[str, Any] | None = None
        self._ml_trained_at: datetime | None = None
        self._ml_last_attempt: datetime | None = None
        self._ml_fit_lock = asyncio.Lock()
        self._settings_export_lock = asyncio.Lock()
        # When a run that left the load forecast to EMHASS last failed, and
        # whether the run currently in flight is one of those. Both are plain
        # attributes rather than anything threaded through _build/_run because
        # _run_lock already serialises runs end to end, so there is only ever
        # one in flight to describe. In-memory only: after a restart the first
        # run tries EMHASS's own path again, which is the right default for a
        # fault that a restart may well have cleared.
        self._no_list_run_failed_at: datetime | None = None
        self._sent_load_list = True
        # One optimisation at a time. Runs arrive from four independent places
        # -- the MPC tick (via async_request_refresh), the day-ahead trigger,
        # the buttons and the services -- and a run is not a pure read: it
        # advances each load's accumulator from the plan currently in hand
        # (assume_from_plan), re-derives the surplus budgets from it, and
        # anchors the end-SOC hysteresis on the previous decision. Two runs
        # overlapping therefore both consume the same previous plan and both
        # mutate the shared registry state, and whichever finishes last
        # publishes over the other regardless of which started first.
        self._run_lock = asyncio.Lock()
        self._ml_store: Store[dict[str, Any]] = Store(
            hass, _ML_STORE_VERSION, f"{DOMAIN}_{entry.entry_id}_ml_fit"
        )

    # -- surplus --------------------------------------------------------------

    def surplus_series(self) -> Series:
        """Spare PV in the current plan, before surplus loads take any.

        The series every surplus sensor renders, and the same one the budgets
        were derived from -- so what the dashboard shows and what the pool was
        actually given can never disagree.

        Fronted by the carried seam row, so the series covers "now" rather
        than starting one timestep into the future. Every surplus sensor
        reads through here for the reason in ``SolarSurplusBase``: one of
        them splicing privately is how they start disagreeing. The window the
        from/until sensors report can therefore open one step in the past --
        which is the honest description of a block already underway, and what
        those sensors showed anyway until the new plan pushed them forward.
        """
        if not self.data:
            return Series.empty()
        series = self.loads.surplus_series(self.data.plan, self.data.load_order)
        if not series:
            return series
        return Series.concat([self._surplus_carry, series])

    # -- lifecycle ------------------------------------------------------------

    async def async_request_settings_refresh(self) -> None:
        """Queue one solve after three seconds without further settings changes."""
        if self._shutdown_requested:
            return
        if self._unsub_settings_refresh is not None:
            self._unsub_settings_refresh()
        self._unsub_settings_refresh = async_call_later(
            self.hass, _SETTINGS_REFRESH_DELAY, self._async_refresh_settings
        )

    async def _async_refresh_settings(self, _now: datetime) -> None:
        """Run with the final settings, without the generic refresh cooldown."""
        self._unsub_settings_refresh = None
        await self.async_refresh()

    async def async_shutdown(self) -> None:
        """Cancel any queued settings solve before shutting down."""
        if self._unsub_settings_refresh is not None:
            self._unsub_settings_refresh()
            self._unsub_settings_refresh = None
        await super().async_shutdown()

    @callback
    def async_start_clock(self) -> None:
        """Re-publish plan-derived entities as the plan's own clock advances.

        Every sensor that answers "right now" -- should_run, scheduled power,
        the planned series -- reads the plan at ``utcnow()``. Those are
        ``CoordinatorEntity``s, so they are only written when an optimisation
        completes; between runs their published state is frozen at whatever
        the answer was at the *previous* run, however far the plan has since
        moved on.

        That is not merely stale, it is self-sustaining: EMHASS aligns row
        zero to the timestep boundary at or before launch, so a freshly
        published plan already covers "now" and then ages one row per timestep
        with nothing to trigger a rewrite. A load whose power sensor
        chatters gets dragged back to life by its own registry notifications;
        one driven by a control entity that rarely changes state has nothing
        to nudge it and stays unknown indefinitely.

        Ticking on the optimisation timestep is the smallest fix that holds:
        one timer for the whole entry, and every plan-derived entity
        recomputes as the row under "now" changes.
        """
        self.async_stop_clock()
        self._unsub_tick = async_track_time_interval(
            self.hass,
            self._async_tick,
            timedelta(minutes=self.config.time_step_minutes),
        )

    @callback
    def async_stop_clock(self) -> None:
        if self._unsub_tick is not None:
            self._unsub_tick()
            self._unsub_tick = None

    @callback
    def async_start_pv_live_tracking(self) -> None:
        """Start smoothing the live PV entity, if one is configured.

        A single sample recorded now (rather than waiting for the entity's
        next state change) means the average is never empty for want of a
        first data point, the same reasoning as NetHouseLoadSensor seeding
        itself in async_added_to_hass.
        """
        self.async_stop_pv_live_tracking()
        entity_id = self.config.pv_live_entity
        if not entity_id:
            return
        self._record_pv_live_sample()
        self._unsub_pv_live = async_track_state_change_event(
            self.hass, [entity_id], self._async_pv_live_changed
        )

    @callback
    def async_stop_pv_live_tracking(self) -> None:
        if self._unsub_pv_live is not None:
            self._unsub_pv_live()
            self._unsub_pv_live = None

    @callback
    def async_start_source_health(self) -> None:
        """Start watching for source entities that have stopped reporting.

        The listener republishes every plan-derived entity and raises or
        clears the repair. Republishing looks heavier than it is -- it is the
        same ``async_update_listeners`` the clock tick already calls every
        timestep -- and it is what carries the news to both places that need
        it: the ``sources_blind`` binary sensor, and ``_async_plan_updated``
        in __init__, which schedules the apply that actually hands the
        inverter back. Routing the stand-down through the existing apply path
        rather than calling the executor from here keeps every write to the
        inverter behind that one lock.
        """
        self._unsub_health = self.health.add_listener(self._async_health_changed)
        self.health.async_start()

    @callback
    def async_stop_source_health(self) -> None:
        self.health.async_stop()
        if self._unsub_health is not None:
            self._unsub_health()
            self._unsub_health = None
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_SOURCES_BLIND)

    @callback
    def _async_health_changed(self) -> None:
        self._track_blind_sources_issue()
        self.async_update_listeners()

    @property
    def soc_final_reward_supported(self) -> bool:
        """Whether the backend accepts ``battery_soc_final_reward_factor``.

        An unknown version counts as unsupported: sending an unknown
        optim_conf key to an older EMHASS is worse than leaving the knob inert.
        """
        return self.backend_version is not None and version_at_least(
            self.backend_version, MIN_EMHASS_VERSION_SOC_FINAL_REWARD
        )

    @property
    def blind_sources(self) -> tuple[str, ...]:
        """Critical readings gone long enough that control must stand down."""
        return self.health.blind_critical

    def _track_blind_sources_issue(self) -> None:
        """Raise or clear the "a source stopped reporting" repair.

        Severity follows the consequence rather than the count: any blind
        source is worth telling the user about, but one of the readings the
        inverter is commanded from being gone means control has just been
        handed back, and that is an error whether it is one entity or twenty.
        """
        blind = self.health.blind
        if not blind:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_SOURCES_BLIND)
            return
        critical = self.health.blind_critical
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_SOURCES_BLIND,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR if critical else ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SOURCES_BLIND,
            translation_placeholders={
                "count": str(len(blind)),
                "minutes": f"{self.health.grace.total_seconds() / 60:g}",
                "entities": "\n".join(f"- `{entity_id}`" for entity_id in blind),
                "consequence": (
                    "The battery has been handed back to the inverter's own "
                    "self-consumption logic, because the readings it would be "
                    "commanded from are among these. Control resumes on its own "
                    "once they report again."
                    if critical
                    else "The plan is still being followed. Each of these falls "
                    "back to a configured default meanwhile, so the optimisation "
                    "is running on assumptions rather than measurements."
                ),
            },
        )

    @callback
    def _async_pv_live_changed(self, event: Event[EventStateChangedData]) -> None:
        self._record_pv_live_sample()

    def _record_pv_live_sample(self) -> None:
        entity_id = self.config.pv_live_entity
        if not entity_id:
            return
        state = self.hass.states.get(entity_id)
        value = (
            None
            if state is None or state.state in ("unknown", "unavailable", "")
            else self._parse_live_power(state, entity_id, "Live PV power")
        )
        self._pv_live_average.record(
            dt_util.utcnow(), value, timedelta(minutes=self.config.time_step_minutes)
        )

    @callback
    def _async_tick(self, _now: datetime) -> None:
        # Not a refresh: no new data is fetched, the same plan is simply read
        # at the new time.
        self.async_update_listeners()

    async def async_load_profiles(self) -> None:
        result = await async_load_profiles(self.hass)
        self.profiles = result.profiles
        self.profile_errors = result.errors
        self._refresh_network_calendar()

    def _refresh_network_calendar(self) -> None:
        """Recompute the resolved network profile after profiles or config change.

        Synchronous: rendering a profile's calendar/band blocks is template
        evaluation against ``options``, not I/O. Only the holiday lookups it
        depends on are async, and those live in ``self.holiday_cache``,
        refreshed separately on every run against whatever dates the horizon
        actually needs -- see ``_build``.
        """
        selection = self.config.network
        profile = self.profiles.get(selection.key) if selection.key else None
        self.flat_demand_charge_per_kw = 0.0
        if profile is None:
            self.network_calendar = None
            return
        try:
            resolved = resolve_network(self.hass, profile, selection.options)
            self.network_calendar = NetworkCalendar.from_resolved(resolved)
            self.flat_demand_charge_per_kw = max(
                0.0, float(resolved.get("flat_demand_charge") or 0.0)
            )
        except (ProfileError, NetworkCalendarError, TypeError, ValueError) as err:
            _LOGGER.warning("Network profile '%s' could not be resolved: %s", selection.key, err)
            self.network_calendar = None
            self.flat_demand_charge_per_kw = 0.0

    def demand_charge_pricing(self, now: datetime) -> DemandChargePricing | None:
        """What to send EMHASS for the selected network profile's demand
        charge right now, and why -- shared by ``_build`` (what is actually
        sent) and the rate diagnostic sensor (what a user is told).

        None when no network profile with a demand charge is selected at
        all. Otherwise always returns a result, with ``effective_rate_per_kw``
        left None and ``reason`` explaining why whenever the peak cannot be
        safely priced yet -- see docs/network_tariffs_plan.md's
        "Version gating" table. Gates, in order:

        * K>1 additionally needs ``MIN_EMHASS_VERSION_DEMAND_COMPONENTS``.
        * The backend must be new enough for ``current_period_peak``.
        * A *restricted* window additionally needs
          ``capacity_charge_window`` (``MIN_EMHASS_VERSION_DEMAND_WINDOW``).
        """
        calendar = self.network_calendar
        if calendar is None or not calendar.demand_charges:
            return None
        charges = calendar.demand_charges
        k = len(charges)

        def _component_base(demand) -> DemandChargeComponentPricing:
            return DemandChargeComponentPricing(
                name=demand.name,
                sheet_rate=demand.rate_per_kw,
                rate_basis=demand.rate_basis,
                aggregate=demand.measure.aggregate,
                n=demand.measure.n,
            )

        components = [_component_base(demand) for demand in charges]
        first = charges[0]
        base = DemandChargePricing(
            sheet_rate=first.rate_per_kw,
            rate_basis=first.rate_basis,
            aggregate=first.measure.aggregate,
            n=first.measure.n,
            components=components,
        )

        if any(demand.rate_per_kw is None for demand in charges):
            reason = "No demand charge rate configured on this profile."
            return replace(
                base,
                reason=reason,
                components=[replace(c, reason=reason) for c in components],
            )

        if k > 1 and (
            self.backend_version is None
            or not version_at_least(self.backend_version, MIN_EMHASS_VERSION_DEMAND_COMPONENTS)
        ):
            reason = (
                f"Needs EMHASS {MIN_EMHASS_VERSION_DEMAND_COMPONENTS}+ for "
                f"{k} demand-charge components; this add-on reports "
                f"{self.backend_version or 'an unknown version'}."
            )
            return replace(
                base,
                reason=reason,
                components=[replace(c, reason=reason) for c in components],
            )

        if self.backend_version is None or not version_at_least(
            self.backend_version, MIN_EMHASS_VERSION_DEMAND_CHARGE
        ):
            reason = (
                f"Needs EMHASS {MIN_EMHASS_VERSION_DEMAND_CHARGE}+; this add-on reports "
                f"{self.backend_version or 'an unknown version'}."
            )
            return replace(
                base,
                reason=reason,
                components=[replace(c, reason=reason) for c in components],
            )

        priced: list[DemandChargeComponentPricing] = []
        shared_reason: str | None = None
        for demand in charges:
            windowed = demand.window is not None and not demand.window.is_unrestricted
            if windowed and (
                self.backend_version is None
                or not version_at_least(self.backend_version, MIN_EMHASS_VERSION_DEMAND_WINDOW)
            ):
                shared_reason = (
                    "This EMHASS release cannot restrict the demand charge to the tariff's own "
                    "window, and pricing it unwindowed would over-shave every hour outside it. "
                    f"Needs EMHASS {MIN_EMHASS_VERSION_DEMAND_WINDOW}+; this add-on reports "
                    f"{self.backend_version or 'an unknown version'}."
                )
                break
            assert demand.rate_per_kw is not None
            rate = effective_rate_per_kw(
                rate_per_kw=demand.rate_per_kw,
                rate_basis=demand.rate_basis,
                aggregate=demand.measure.aggregate,
                n=demand.measure.n,
                days_in_period=days_in_current_period(now),
            )
            interval_timesteps = 1
            if self.backend_version is not None and version_at_least(
                self.backend_version, MIN_EMHASS_VERSION_CAPACITY_INTERVAL
            ):
                interval_timesteps = demand.measure.interval_timesteps(
                    self.config.time_step_minutes
                )
            priced.append(
                replace(
                    _component_base(demand),
                    effective_rate_per_kw=rate,
                    windowed=windowed,
                    interval_timesteps=interval_timesteps,
                )
            )

        if shared_reason is not None:
            return replace(
                base,
                reason=shared_reason,
                components=[replace(c, reason=shared_reason) for c in components],
            )

        first_priced = priced[0]
        return replace(
            base,
            effective_rate_per_kw=first_priced.effective_rate_per_kw,
            windowed=first_priced.windowed,
            interval_timesteps=first_priced.interval_timesteps,
            components=priced,
        )

    def _capacity_interval_history_w(
        self,
        now: datetime,
        step: timedelta,
        interval_timesteps: int,
        peak_tracker: PeakTracker | None = None,
    ) -> list[float]:
        """``capacity_charge_current_interval_history`` for the still-open interval.

        Spreads :attr:`PeakTracker.open_interval_kwh` flat across its ``m``
        elapsed timesteps -- this reproduces the completed-interval average
        EMHASS actually uses exactly, not approximately, because that average
        only ever depends on the history's *sum*. See
        planning/capacity_interval_plan.md's "Key design finding".

        Empty whenever the open interval cannot be trusted on this tick: no
        settle has happened yet (``open_interval_start`` is None), or ``m``
        falls outside ``0..interval_timesteps - 1`` (clock skew, a missed
        rollover). EMHASS then reads t0 as an interval boundary, which is the
        safe fallback -- an over-length vector would make it reject the whole
        payload instead.
        """
        tracker = peak_tracker if peak_tracker is not None else self.peak_tracker
        if interval_timesteps <= 1 or tracker is None:
            return []
        start = tracker.open_interval_start
        if start is None:
            _LOGGER.debug("Capacity interval history: no open interval settled yet")
            return []
        m = int((row_zero_start(now, step, self._row_zero_mode) - start) / step)
        if not 0 <= m < interval_timesteps:
            _LOGGER.debug(
                "Capacity interval history: elapsed timestep %d outside 0..%d, sending none",
                m,
                interval_timesteps - 1,
            )
            return []
        if m == 0:
            return []
        step_hours = step.total_seconds() / 3600
        entry_w = tracker.open_interval_kwh * 1000 / (m * step_hours)
        return [entry_w] * m

    async def async_load_ml_state(self) -> None:
        """Restore successful-fit settings; legacy sensor-only state needs refitting."""
        stored = await self._ml_store.async_load()
        if stored:
            self._ml_trained_settings = stored.get("trained_settings")
            if raw := stored.get("trained_at"):
                self._ml_trained_at = dt_util.parse_datetime(raw)

    async def _async_save_ml_state(self) -> None:
        await self._ml_store.async_save(
            {
                "trained_settings": self._ml_trained_settings,
                "trained_at": self._ml_trained_at.isoformat() if self._ml_trained_at else None,
            }
        )

    async def _ml_refit_due(self) -> bool:
        """Whether the trained model is older than :data:`_ML_REFIT_INTERVAL`.

        State saved before the fit time was recorded has no age to go on, so its
        clock starts now -- one weekly cycle from the first run after upgrading,
        not an immediate refit of a model that may be a day old.
        """
        if self._ml_trained_at is None:
            self._ml_trained_at = dt_util.utcnow()
            await self._async_save_ml_state()
            return False
        return dt_util.utcnow() - self._ml_trained_at >= _ML_REFIT_INTERVAL

    def _ml_model_matches(self, settings: dict[str, Any]) -> bool:
        """Ignore request-grid and weather transport choices that do not train the model."""
        return self._ml_trained_settings is not None and all(
            settings.get(name) == self._ml_trained_settings.get(name)
            for name in ML_MODEL_SETTING_NAMES
        )

    def _forecast_profile_settings(self) -> dict[str, Any]:
        """Use identical profile precedence for training and optimisation."""
        settings: dict[str, Any] = {}
        for selection, kind in (
            (self.config.price, PROFILE_KIND_PRICE),
            (self.config.pv, PROFILE_KIND_PV),
            (self.config.load, PROFILE_KIND_LOAD),
        ):
            settings.update(self._settings(selection, kind))
        if self.loads.needs_outdoor_temperature:
            settings.update(self._settings(self.config.temperature, PROFILE_KIND_TEMPERATURE))
        return settings

    def _effective_ml_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Project the same static ML inputs used by the optimisation payload resolver."""
        config = self.config
        effective = resolve_effective_settings(
            battery=config.battery,
            grid=config.grid,
            hybrid=config.hybrid_inverter,
            time_step_minutes=config.time_step_minutes,
            cost_fun=DEFAULT_COST_FUN,
            row_zero_mode=self._row_zero_mode,
            num_lags=config.dayahead_num_lags,
            profile_settings=settings,
            backend_version=self.backend_version,
        )
        return {
            name: value for name, value in effective.runtime().items() if name in ML_SETTING_NAMES
        }

    def _persistent_baseline(self) -> tuple[dict[str, Any], tuple[str, ...]]:
        """The allowlisted static baseline and the warnings raised resolving it.

        Same inputs and profile precedence as an optimisation request, but only
        the persistent projection is kept: no live inputs, forecasts, grid
        safety limits or load progress can reach it.
        """
        config = self.config
        effective = resolve_effective_settings(
            battery=config.battery,
            grid=config.grid,
            hybrid=config.hybrid_inverter,
            time_step_minutes=config.time_step_minutes,
            cost_fun=self.cost_fun,
            row_zero_mode=self._row_zero_mode,
            num_lags=config.dayahead_num_lags,
            profile_settings=self._forecast_profile_settings(),
            backend_version=self.backend_version,
            send_derating=(
                self.backend_version is not None
                and version_at_least(self.backend_version, MIN_EMHASS_VERSION_CHARGE_DERATING)
            ),
            send_soc_final_reward=self.soc_final_reward_supported,
            # Configured enablement, not whether a series happens to be present.
            use_pv=bool(config.pv.key),
        )
        return effective.persistent(), effective.warnings

    @staticmethod
    def _export_failure(stage: str, err: EmhassError) -> str:
        """Describe a failed export step without quoting the response body."""
        if isinstance(err, EmhassConnectionError):
            return f"could not reach EMHASS to {stage} its configuration"
        status = getattr(err, "status", None)
        return f"EMHASS refused to {stage} its configuration" + (
            f" (HTTP {status})" if status else ""
        )

    async def async_export_settings(self) -> int:
        """Write Companion's static settings into EMHASS's saved configuration.

        Manual only. Fetches the active configuration, overlays the allowlisted
        baseline and saves the whole result, because ``/set-config`` rebuilds
        from EMHASS's defaults and would reset every key left out. Unrelated
        settings, including credentials, therefore round-trip untouched.
        Returns the number of Companion-owned settings written.

        Raises :class:`HomeAssistantError` on any failure; nothing is reported
        as saved unless ``/set-config`` accepted the request. The fetched
        configuration is never logged or echoed in an error -- it holds the
        backend's secrets -- so failures carry the status code, not the body.
        This is :meth:`EmhassClient.async_set_config_merged` unrolled, because
        an empty answer from ``/get-config`` must stop the write rather than
        reset every other setting to EMHASS's defaults.
        """
        async with self._settings_export_lock:
            try:
                baseline, warnings = self._persistent_baseline()
            except ProfileError as err:
                raise HomeAssistantError(f"Settings were not written to EMHASS: {err}") from err
            try:
                current = await self.client.async_get_config()
            except EmhassError as err:
                raise HomeAssistantError(
                    f"Settings were not written to EMHASS: {self._export_failure('read', err)}"
                ) from None
            if not isinstance(current, dict) or not current:
                raise HomeAssistantError(
                    "Settings were not written to EMHASS: it returned no active configuration"
                )
            try:
                await self.client.async_set_config({**current, **baseline})
            except EmhassError as err:
                raise HomeAssistantError(
                    f"Settings were not written to EMHASS: {self._export_failure('save', err)}"
                ) from None
        for warning in warnings:
            _LOGGER.warning("Settings export: %s", warning)
        _LOGGER.info("Wrote %d Companion settings to EMHASS", len(baseline))
        return len(baseline)

    def reload_config(self) -> None:
        self.config = EmhassConfig.from_entry(self.hass, self.config_entry)
        self._refresh_network_calendar()

    def load_forecast_settings(self) -> dict[str, Any]:
        """The ``emhass:`` settings the configured load profile contributes.

        Exposed (unlike the private ``_settings`` used for the other kinds)
        because the "train forecaster" action needs to know what the load profile
        resolves to before any run happens.

        A profile that no longer resolves degrades to ``{}`` here rather than
        raising, unlike the run path: every caller of this runs at setup
        (button platform), where a hard failure would cost the
        whole integration. The next run still fails loudly on the same
        condition -- see :meth:`_settings`.
        """
        try:
            return self._settings(self.config.load, PROFILE_KIND_LOAD)
        except ProfileError as err:
            _LOGGER.warning("Load profile settings unavailable at setup: %s", err)
            return {}

    @property
    def uses_mlforecaster(self) -> bool:
        """Whether the configured load profile trains EMHASS's own ML model.

        Gates the "Train load forecaster" action, which means nothing for the
        "typical"/"naive" methods or for a load profile that supplies its own
        forecast (``forecast_entity``), which never touch mlforecaster at all.
        """
        method = self.load_forecast_settings().get(EMHASS_CONF_LOAD_FORECAST_METHOD)
        return method == LOAD_FORECAST_METHOD_MLFORECASTER

    async def async_ensure_emhass_baseline(self) -> None:
        """Make sure EMHASS has a saved configuration for actions to run against.

        Every ``/action/*`` request fails until EMHASS has a ``params.pkl``,
        which ``/set-config`` creates. So this fetches the active configuration
        and saves it back unchanged: a no-op for a backend that already has
        one, and the minimal initialisation for one that does not.

        It deliberately carries no Companion setting. Timestep, rounding, ML
        and every other static value travel with each request (see
        :mod:`effective_settings`), and the manual export is the only thing
        that writes them to the saved configuration; a backend value the user
        chose is therefore never overwritten by a setup or reload.

        Failure is non-fatal during setup and logged for diagnosis. Runs also
        work against an existing baseline, so this is retried on the next
        reload rather than blocking the integration.
        """
        try:
            await self.client.async_set_config_merged({})
        except EmhassError as err:
            _LOGGER.warning(
                "Could not initialise the EMHASS configuration (%s); continuing "
                "setup. Actions need EMHASS to have a saved configuration -- "
                "reloading the integration will retry it.",
                err,
            )

    async def async_check_heat_topology(self) -> None:
        """Clear the obsolete repair: each request now disables saved topology."""
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_HEAT_TOPOLOGY_REPLACES)

    # -- deferrable loads -----------------------------------------------------

    def deferrable_loads(self, now: datetime) -> list[DeferrableLoad]:
        """Live deferrable loads, projected for a request at ``now``.

        Read from the registry rather than the subentries, because the values a
        user adjusts day to day live in entities; the subentry only seeds them.
        """
        tanks = [load for load in self.loads.all() if load.is_water_tank and load.enabled]
        backend_error = self._water_tank_backend_error() if tanks else None
        if backend_error:
            self._water_tank_plan_blocked = backend_error
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "water_tank_unsupported",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="water_tank_unsupported",
                translation_placeholders={
                    "required": "0.18.8",
                    "version": self.backend_version or "unknown",
                },
            )
            raise WaterTankError(backend_error)
        ir.async_delete_issue(self.hass, DOMAIN, "water_tank_unsupported")
        projected = []
        for load in self.loads.all():
            issue_id = f"water_tank_invalid_{load.subentry_id}"
            try:
                temperature = self.loads.room_temperature(load)
                projected.append(load.to_load(now, self.config.time_step_minutes, temperature))
            except ValueError as err:
                if not load.is_water_tank:
                    raise
                message = f"Water tank {load.name}: {err}"
                self._water_tank_plan_blocked = message
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="water_tank_invalid",
                    translation_placeholders={"name": load.name, "reason": str(err)},
                )
                raise WaterTankError(message) from err
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        return projected

    def _water_tank_backend_error(self) -> str | None:
        try:
            supported = Version(self.backend_version or "") >= Version("0.18.8")
        except InvalidVersion:
            supported = False
        if not supported:
            return (
                "Water tanks require EMHASS 0.18.8 or newer with a known version; "
                f"the backend reports {self.backend_version or 'unknown'}. Upgrade EMHASS and reload."
            )
        return None

    def water_tank_error(self, load: DeferrableRuntime) -> str | None:
        """Fail closed before tank dispatch, including between optimiser runs."""
        if not load.is_water_tank or not load.enabled:
            return None
        if error := self._water_tank_backend_error():
            self._water_tank_plan_blocked = error
            return error
        try:
            load.thermal_config(self.loads.room_temperature(load))
        except ValueError as err:
            self._water_tank_plan_blocked = f"Water tank {load.name}: {err}"
            return self._water_tank_plan_blocked
        return self._water_tank_plan_blocked

    def load_groups(self) -> list[DeferrableLoadGroup]:
        """Configured load groups, read straight from their subentries.

        Unlike loads, a group carries no live/restorable state of its own --
        it is stateless and cheap to rebuild on every call, so it is read
        directly from ``entry.subentries`` rather than mirrored into a
        registry.
        """
        return [
            DeferrableLoadGroup(
                subentry_id=subentry_id,
                name=subentry.title,
                load_subentry_ids=tuple(subentry.data.get(CONF_GROUP_LOAD_IDS, ())),
                max_power_w=subentry.data.get(CONF_GROUP_MAX_POWER),
                mutual_exclusion=bool(subentry.data.get(CONF_GROUP_MUTUAL_EXCLUSION, False)),
            )
            for subentry_id, subentry in self.config_entry.subentries.items()
            if subentry.subentry_type == SUBENTRY_TYPE_LOAD_GROUP
        ]

    # -- running --------------------------------------------------------------

    async def _async_update_data(self) -> EmhassData:
        return await self.async_run(ACTION_MPC, notify=False)

    async def async_run(self, action: str, *, notify: bool = True) -> EmhassData:
        """Gather inputs, run one EMHASS action, and store the result.

        Serialised against every other run through ``_run_lock``: a second
        caller waits for the one in flight rather than racing it. The publish
        is inside the lock as well, so the plan that is announced is always
        the one from the run that finished last, not whichever run's
        ``async_set_updated_data`` happened to be scheduled last.
        """
        async with self._run_lock:
            return await self._async_run(action, notify=notify)

    def _note_failed_run(self) -> None:
        """Record a failure that EMHASS's own load retrieval could be behind.

        Only a run that sent no load series of its own qualifies: those are
        the ones where EMHASS fetches a day of history itself, on entities and
        settings this integration does not control. A run that carried its own
        series had no such fetch to fail, so whatever went wrong there is not
        something falling back could fix -- and arming the cooldown for it
        would quietly disable EMHASS's forecaster over an unrelated fault.
        """
        if not self._sent_load_list:
            self._no_list_run_failed_at = dt_util.utcnow()

    async def _async_run(self, action: str, *, notify: bool) -> EmhassData:
        try:
            data = await self._run(action)
        except WaterTankError as err:
            self._track_run_failed_issue(True, action, str(err))
            raise
        except ProfileError as err:
            # Deliberately not counted against EMHASS's own load forecast
            # below: a data source that could not be read is this side's
            # problem, and the request never reached EMHASS at all.
            self._track_run_failed_issue(True, action, str(err))
            raise UpdateFailed(f"Data source error: {err}") from err
        except EmhassError as err:
            self._note_failed_run()
            self._track_run_failed_issue(True, action, str(err))
            raise UpdateFailed(f"EMHASS error: {err}") from err
        except UpdateFailed as err:
            # Raised directly by _run() when EMHASS answers 200 but flags the
            # run itself as failed (last_run.status == "error"), rather than
            # rejecting the request outright.
            self._note_failed_run()
            self._track_run_failed_issue(True, action, str(err))
            raise

        self._track_run_failed_issue(False, action, "")
        # Last moment the outgoing plan is still self.data, whichever way this
        # run publishes: async_set_updated_data below, or the coordinator
        # storing what _async_update_data returns.
        self._surplus_carry = seam_carry(
            self.surplus_series(),
            self.loads.surplus_series(data.plan, data.load_order),
        )
        if notify:
            self.async_set_updated_data(data)
        return data

    def _learn_row_zero_mode(
        self, inputs: PayloadInputs, plan: Plan, sent_at: datetime | None = None
    ) -> None:
        """Work out how EMHASS rounds a launch time onto row zero.

        Only a run launched where the rounding modes disagree says anything:
        one in the second half of a step separates ``nearest`` from ``first``.
        Otherwise the current belief stands.
        """
        if not plan.rows:
            return
        step = timedelta(minutes=inputs.time_step_minutes)
        modes = (ROW_ZERO_NEAREST, ROW_ZERO_FIRST, ROW_ZERO_LAST)
        candidates = {mode: row_zero_start(inputs.now, step, mode) for mode in modes}
        if len(set(candidates.values())) == 1:
            return
        # Input gathering takes time, so EMHASS read its clock somewhere between
        # the two instants. If they round differently the observation is
        # ambiguous and teaches nothing.
        if sent_at is not None and any(
            row_zero_start(sent_at, step, mode) != candidates[mode] for mode in modes
        ):
            return
        matches = [mode for mode, start in candidates.items() if start == plan.rows[0].timestamp]
        if not matches or self._row_zero_mode in matches:
            return
        # A run just past the half-way point cannot tell "nearest" from "last".
        learned = ROW_ZERO_NEAREST if ROW_ZERO_NEAREST in matches else matches[0]
        _LOGGER.info("EMHASS rounds plan row zero '%s' (was '%s')", learned, self._row_zero_mode)
        self._row_zero_mode = learned

    async def _run(self, action: str) -> EmhassData:
        inputs, built = await self._build(action)
        self._track_tank_behind_issues(built.tank_shortfalls)
        if (entity_id := self._unreadable_load_sensor(action, built.payload)) is not None:
            raise UpdateFailed(
                f"Load sensor {entity_id} is unavailable, so EMHASS has nothing "
                "to build a load forecast from. Check the integration that "
                "provides it."
            )
        sent_at = dt_util.utcnow()
        last_run, plan = await self.client.async_optimize(action, built.payload)
        if plan is not None and last_run.ok and not last_run.infeasible:
            self._learn_row_zero_mode(inputs, plan, sent_at)

        findings: list[Finding] = []
        load_order = built.load_order
        last_success = dt_util.utcnow() if last_run.ok else None
        if last_run.status == "error":
            raise UpdateFailed(f"EMHASS reported an error: {last_run.error_message or 'unknown'}")
        if last_run.infeasible:
            # A perfectly successful HTTP call can still carry an unusable plan.
            # Surfacing it keeps the executor from acting on nonsense.
            _LOGGER.warning(
                "EMHASS could not find a feasible solution for %s; keeping the previous plan",
                action,
            )
            previous = self.data
            plan = previous.plan if previous else None
            # The kept plan keeps its own age and its own deferrable numbering.
            # EMHASS still says status "ok" for an infeasible solve, so stamping
            # it fresh would let a run of failures hold an old plan executable
            # indefinitely, past the staleness watchdog that exists to stop
            # exactly that. And its P_deferrable{k} columns are numbered by the
            # request that produced them: read through this request's order, a
            # load added or removed since would switch on from its neighbour's
            # schedule.
            last_success = previous.last_success if previous else None
            load_order = previous.load_order if previous else built.load_order
            findings = self._diagnose_infeasible(built, action, inputs.now)
            self._track_infeasible_issue(True, action, findings)
        else:
            self._track_infeasible_issue(False, action)
            if last_run.status == "no-run":
                _LOGGER.warning(
                    "EMHASS reports no completed run after %s; the plan may be missing",
                    action,
                )

        if plan is not None and not self._schema_supported(plan):
            plan = None
        if plan is not None and built.extra_rows and not last_run.infeasible:
            # Success branch only: a kept plan is already folded and numbered
            # for its own request, so folding it again would corrupt it.
            folded = plan.folded(len(built.load_order), [row.parent for row in built.extra_rows])
            if folded is None:
                _LOGGER.warning(
                    "EMHASS returned a %s plan without the extra rows requested for "
                    "later daily windows; ignoring it",
                    action,
                )
            plan = folded
        self._track_degraded_plan_issue(action, plan)
        if (
            plan is not None
            and last_run.ok
            and not last_run.infeasible
            and plan.optim_status not in DEGRADED_OPTIM_STATUSES
        ):
            self._water_tank_plan_blocked = None

        # Trimmed to the horizon that was actually requested: profiles fetch
        # whatever their source publishes (a day-ahead tariff commonly hands
        # back tomorrow's prices too), which is longer than what got solved
        # whenever the horizon is shorter than a day. Left untrimmed, the
        # price sensors run past every plan-derived one (p_pv, p_load,
        # p_grid, p_batt all come from opt_res, capped at horizon_steps rows),
        # which is what stretched the dashboard cards' time window out past
        # where the plan itself stops.
        #
        # Cut on the timestep boundary at or before the run rather than on the
        # wall-clock instant it happened to start: cutting at `now` (a few
        # seconds later) left the price series starting one whole timestep
        # after the plan did, and the price sensors reading "unknown" for the
        # entire timestep in progress -- every run, indefinitely, since the
        # next run arrives before the gap closes. Not row zero: that can be the
        # next boundary, which would drop the price in force right now.
        step = timedelta(minutes=inputs.time_step_minutes)
        window_start = floor_to_step(inputs.now, step)
        horizon_end = window_start + step * inputs.horizon_steps

        if plan is not None:
            await self._prepare_soc_day_range(inputs.now)
            if self._soc_day_range is not None:
                publish_soc_day_range(self._soc_day_range, plan.series("soc_percent"), inputs.now)
                await self._save_soc_day_range()

        return EmhassData(
            plan=plan,
            last_run=last_run,
            # ...and window_covering, not window, so that a price series coarser
            # than the timestep (hourly prices are still the norm in most
            # markets) keeps the point in force at the boundary instead of
            # dropping it for having begun earlier.
            buy_price=inputs.buy_price.window_covering(window_start, horizon_end)
            if inputs.buy_price
            else Series.empty(),
            sell_price=inputs.sell_price.window_covering(window_start, horizon_end)
            if inputs.sell_price
            else Series.empty(),
            pv_forecast=inputs.pv or Series.empty(),
            load_forecast=inputs.load or Series.empty(),
            payload=built.payload,
            warnings=built.warnings,
            defaulted_forecast_methods=built.defaulted_forecast_methods,
            load_order=load_order,
            payload_order=built.load_order,
            extra_row_names=[row.name for row in built.extra_rows],
            end_soc=self._end_soc,
            soc_day_range=self._soc_day_range,
            last_action=action,
            # Before the trim above, deliberately -- see the field's docstring.
            price_source_end=inputs.price_source_end
            or (inputs.buy_price.end if inputs.buy_price else None),
            # Only a genuinely successful solve counts. "no-run" trips the
            # staleness watchdog outright; infeasible leaves it counting down
            # from the kept plan's own solve (above). Either way the executor
            # stops acting on a plan that is no longer being renewed.
            last_success=last_success,
            last_infeasibility=findings,
        )

    async def _prepare_soc_day_range(self, now: datetime) -> None:
        """Make sure today's past latch exists before this run publishes.

        Restored from disk when the day matches, then filled from recorder
        history once per local day. History is what puts back a peak from
        earlier today after a restart: the forward series no longer contains
        it, and the previous process's memory is gone.
        """
        today = dt_util.as_local(now).date()
        if self._soc_day_range is None or self._soc_day_range.day != today:
            self._soc_day_range = await self._load_soc_day_range(today) or DayRange(day=today)
            self._soc_history_merged_day = None
        if self._soc_history_merged_day != today and await self._merge_recorded_soc(now):
            self._soc_history_merged_day = today

    async def _load_soc_day_range(self, today: date) -> DayRange | None:
        try:
            stored = await self._soc_day_store.async_load()
        except Exception:  # a missing store must not block the run
            _LOGGER.debug("Could not load the SOC day range", exc_info=True)
            return None
        if not isinstance(stored, dict) or stored.get("day") != today.isoformat():
            return None
        return DayRange(
            day=today,
            past_low=_point_from_json(stored.get("past_low")),
            past_high=_point_from_json(stored.get("past_high")),
        )

    def _planned_soc_entity_id(self) -> str | None:
        """The planned battery-level sensor, looked up so a rename still hits."""
        return er.async_get(self.hass).async_get_entity_id(
            "sensor", DOMAIN, f"{self.config_entry.entry_id}_battery_soc"
        )

    async def _merge_recorded_soc(self, now: datetime) -> bool:
        """Fold today's recorded planned SOC into the past latch.

        Returns whether history was actually consulted. False leaves the next
        run to try again -- the sensor may not be registered on the first
        cycle, and a recorder blip should not drop the rest of the day.
        """
        if self._soc_day_range is None:
            return False
        entity_id = self._planned_soc_entity_id()
        if entity_id is None:
            return False
        start = dt_util.start_of_local_day(dt_util.as_local(now))
        try:
            instance = get_instance(self.hass)
        except (KeyError, RuntimeError):
            return False

        def read() -> list[Point]:
            found = history.state_changes_during_period(
                self.hass,
                start,
                dt_util.as_utc(now),
                entity_id,
                True,  # no_attributes -- only the percentage is used
                False,  # descending
                None,  # limit
                True,  # include_start_time_state
            )
            return recorded_soc_points(found.get(entity_id, []), now)

        try:
            points = await instance.async_add_executor_job(read)
        except Exception:  # a history probe must never break a run
            _LOGGER.debug("Could not read today's planned SOC history", exc_info=True)
            return False
        latch_past_soc(self._soc_day_range, points)
        return True

    async def _save_soc_day_range(self) -> None:
        day_range = self._soc_day_range
        if day_range is None:
            return
        try:
            await self._soc_day_store.async_save(
                {
                    "day": day_range.day.isoformat(),
                    "past_low": _point_to_json(day_range.past_low),
                    "past_high": _point_to_json(day_range.past_high),
                }
            )
        except Exception:  # losing the latch is recoverable from history
            _LOGGER.debug("Could not store the SOC day range", exc_info=True)

    def _schema_supported(self, plan: Plan) -> bool:
        """Refuse a plan written to a schema major we do not understand.

        A major bump means a column was renamed or removed, or a unit or sign
        convention changed. Parsing it anyway would not raise -- it would
        quietly produce a plan with, say, an inverted battery sign, and the
        executor would act on it. Discarding the plan makes it stale instead,
        which the watchdog already handles safely.
        """
        major = schema_major(plan.schema_version)
        if major is None or major == SUPPORTED_PLAN_SCHEMA_MAJOR:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_PLAN_SCHEMA)
            return True

        _LOGGER.error(
            "EMHASS returned plan schema %s; this integration understands major "
            "%s. Ignoring the plan rather than risking a misread of it.",
            plan.schema_version,
            SUPPORTED_PLAN_SCHEMA_MAJOR,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_PLAN_SCHEMA,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_PLAN_SCHEMA,
            translation_placeholders={
                "found": str(plan.schema_version),
                "supported": str(SUPPORTED_PLAN_SCHEMA_MAJOR),
            },
        )
        return False

    def _diagnose_infeasible(
        self, built: PayloadResult, action: str, now: datetime
    ) -> list[Finding]:
        """Run infeasibility.py's checks on the request EMHASS just refused.

        Only ever called for an infeasible run, so it costs nothing on a
        normal one. A few loops over the horizon, cheap enough to run inline.
        Every finding goes to the log, where the repair issue points; the
        issue itself shows only the headline. A failure here must never cost
        the run, so it falls back to no findings and the generic issue text.
        """
        try:
            names = [
                subentry.title
                if (subentry := self.config_entry.subentries.get(subentry_id))
                else f"Deferrable {k}"
                for k, subentry_id in enumerate(built.load_order)
            ]
            names.extend(row.name for row in built.extra_rows)
            findings = diagnose(built.payload, load_names=names, warnings=built.warnings, now=now)
        except Exception:  # a diagnosis must never break a run
            _LOGGER.debug("Could not diagnose the infeasible %s run", action, exc_info=True)
            return []
        for finding in findings:
            _LOGGER.warning(
                "%s infeasible, %s: %s. %s",
                action,
                finding.severity.value,
                finding.title,
                finding.detail,
            )
        return findings

    def _track_infeasible_issue(
        self, infeasible: bool, action: str, findings: list[Finding] | None = None
    ) -> None:
        """Raise or clear the whole-integration infeasible-run repair.

        EMHASS reports infeasibility for the problem as a whole, with no hint
        at which load caused it, so this is one issue per integration rather
        than per load (contrast :meth:`LoadNumber._check_thermal_reachability`,
        which can name the offending load and so gets its own issue each).

        When infeasibility.py names a cause, the issue switches to that cause's
        own translation under the same issue id. Re-creating the issue updates
        it in place, so a changed cause replaces the old text and nothing is
        left behind to clear separately.
        """
        if not infeasible:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_OPTIMIZATION_INFEASIBLE)
            return
        translation_key = ISSUE_OPTIMIZATION_INFEASIBLE
        placeholders = {"action": action}
        top = headline(findings or [])
        if top is not None and top.code in INFEASIBLE_CAUSE_CODES:
            translation_key = f"{ISSUE_OPTIMIZATION_INFEASIBLE}_{top.code}"
            critical = sum(1 for f in findings or [] if f.severity is Severity.CRITICAL)
            placeholders = {**top.placeholders, "action": action, "total": str(critical)}
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_OPTIMIZATION_INFEASIBLE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=translation_key,
            translation_placeholders=placeholders,
        )

    def _track_tank_behind_issues(self, shortfalls: dict[str, TankShortfall]) -> None:
        """Raise or clear the per-tank "cannot reach its target in time" warning.

        The request already carries a floor lowered to what the heater can
        reach (``WaterTankConfig.to_emhass``), so the run goes ahead; this only
        says the schedule is being missed. One issue per tank, cleared by the
        first request in which its schedule is reachable again.
        """
        tanks = [load for load in self.loads.all() if load.is_water_tank]
        # A deleted tank is never visited below, so sweep its leftover warning.
        prefix = f"{ISSUE_TANK_BEHIND_SCHEDULE}_"
        current = {f"{prefix}{load.subentry_id}" for load in tanks}
        for domain, issue_id in list(ir.async_get(self.hass).issues):
            if domain == DOMAIN and issue_id.startswith(prefix) and issue_id not in current:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        for load in tanks:
            issue_id = f"{prefix}{load.subentry_id}"
            shortfall = shortfalls.get(load.subentry_id)
            if shortfall is None:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                continue
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_TANK_BEHIND_SCHEDULE,
                translation_placeholders={
                    "name": load.name,
                    "current": f"{shortfall.current:.1f}",
                    "required": f"{shortfall.required:.1f}",
                    "achievable": f"{shortfall.achievable:.1f}",
                    "time": shortfall.moment.strftime("%H:%M"),
                    "power": format_power(shortfall.power_w),
                },
            )

    def _track_never_started_issues(self) -> None:
        """Raise or clear the per-load "it never started" repair.

        One issue per load, like :meth:`LoadNumber._check_thermal_reachability`:
        only that appliance is affected, and the fix is its own -- a dishwasher
        whose own start button was never pressed, a plug that does not resume
        the program on power-up, a breaker left off.

        Read off ``last_completion_reason`` rather than raised from inside
        ``check_auto_disarm``, so that the runtime stays free of Home Assistant
        plumbing and the issue survives being recomputed every cycle: it is
        cleared by the next run that ends any other way, which is the only
        evidence that whatever was wrong has been dealt with.
        """
        for load in self.loads.all():
            issue_id = f"{ISSUE_LOAD_NEVER_STARTED}_{load.subentry_id}"
            if load.last_completion_reason != COMPLETION_NEVER_STARTED:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                continue
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_LOAD_NEVER_STARTED,
                translation_placeholders={
                    "name": load.name,
                    "hours": f"{load.operating_hours:g}",
                },
            )

    def _track_run_failed_issue(self, failed: bool, action: str, message: str) -> None:
        """Raise or clear the whole-integration run-failed repair.

        Covers a run that errored outright -- EMHASS unreachable, it rejected
        the request, or raised internally -- as distinct from
        :meth:`_track_infeasible_issue`, where EMHASS answered normally but
        found no usable plan. Without this, such a failure was only a
        DataUpdateCoordinator warning in the log, easy to miss until stale
        data or a stuck plan made it obvious some time later.
        """
        if not failed:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_RUN_FAILED)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_RUN_FAILED,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_RUN_FAILED,
            translation_placeholders={"action": action, "error": message},
        )

    def _track_degraded_plan_issue(self, action: str, plan: Plan | None) -> None:
        """Raise or clear the repair for a plan EMHASS solved only in part.

        EMHASS answers a time-limited solve and a relaxed fallback with status
        "ok", so nothing else would notice them. The repair says why the
        executor is not acting on the plan, and clears on the next full solve.
        """
        status = plan.optim_status if plan is not None else None
        if status is None or status not in DEGRADED_OPTIM_STATUSES:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_PLAN_DEGRADED)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_PLAN_DEGRADED,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PLAN_DEGRADED,
            translation_placeholders={"action": action, "status": status},
        )

    def _track_pv_tail_issue(
        self, end_soc: EndSocDecision, pv: Series | None, horizon_end: datetime
    ) -> None:
        """Raise or clear the "add Solcast day 3" nudge.

        Only when the fix is genuinely one click away: Optimized mode is on,
        the PV forecast runs out within half a day past the horizon, the
        selected PV profile is Solcast, and its day-3 sensor exists but is not
        in the profile's entity list. Everything else -- other profiles, no
        such sensor -- just gets the honest ``pv_tail: assumed zero``
        annotation on the End SOC target sensor. Never a substitute for the
        heuristic's own degradation, which is safe (darkness is assumed, so
        the target only errs high).
        """
        selection = self.config.pv
        actionable = (
            end_soc.details.get("mode") == END_SOC_OPTIMIZED
            and "fallback" not in end_soc.details
            and (pv is None or not pv.covers(horizon_end + timedelta(hours=12)))
            and selection.key == PROFILE_KEY_PV_SOLCAST
            and self.hass.states.get(SOLCAST_DAY3_ENTITY) is not None
            and SOLCAST_DAY3_ENTITY not in (selection.options.get("entities") or [])
        )
        if not actionable:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_PV_TAIL_SHORT)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_PV_TAIL_SHORT,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PV_TAIL_SHORT,
            translation_placeholders={"entity": SOLCAST_DAY3_ENTITY},
        )

    def _load_for_terminal(self, load: Series) -> tuple[Series | None, str]:
        """The load series terminal.decide_end_soc should reason about.

        Most load profiles hand the companion a real forecast, used as-is.
        Settings-only ones ("House load sensor" among them) hand EMHASS the
        forecasting job instead and return nothing here -- EMHASS still
        computes a real forecast, it just never reaches the companion,
        except baked into the ``p_load`` column of whatever it last actually
        solved. Borrowing that column from the previous plan gives Optimized
        mode a genuine (if one-cycle-stale) household shape to work with,
        instead of falling back to same-as-start on every single run.

        The borrowed series' own timestamps land within the *current* run's
        horizon (last cycle's horizon, shifted by roughly one MPC interval),
        not in the tail past it -- but that is exactly what
        ``_optimized``'s existing "look 24h back" fallback already expects,
        so no further handling is needed once a series reaches it.
        """
        if load:
            return load, LOAD_SOURCE_PROFILE
        if self.data is not None and self.data.plan is not None:
            borrowed = self.data.plan.series("p_load")
            if borrowed:
                return borrowed, LOAD_SOURCE_LAST_PLAN
        return None, LOAD_SOURCE_PROFILE

    async def _build(self, action: str) -> tuple[PayloadInputs, PayloadResult]:
        config = self.config
        now = dt_util.utcnow()
        step = timedelta(minutes=config.time_step_minutes)
        horizon_end = now + step * config.horizon_steps
        spot, pv, load = await asyncio.gather(
            self._series(config.price, PROFILE_KIND_PRICE),
            self._series(config.pv, PROFILE_KIND_PV),
            self._series(config.load, PROFILE_KIND_LOAD),
        )

        # Only fetched once a thermal load exists: the outdoor forecast is what
        # the thermal model cools towards, and means nothing without one.
        outdoor = Series.empty()
        if self.loads.needs_outdoor_temperature and config.temperature:
            outdoor = await self._series(config.temperature, PROFILE_KIND_TEMPERATURE)

        buy = sell = None
        network_warnings: list[str] = []
        price_warnings: list[str] = []
        price_source_end: datetime | None = None
        # Past the horizon too: the end-SOC lookahead reads the day beyond it.
        fill_until = horizon_end + END_SOC_LOOKAHEAD
        if spot and self.price_forecast is not None and not spot.covers(fill_until):
            spot, price_source_end, forecast_warning = await self._fill_spot_from_forecast(
                spot, now, fill_until
            )
            if forecast_warning:
                price_warnings.append(forecast_warning)
        if spot:
            buy, sell = config.tariff.compose(self.hass, spot)
            if self.network_calendar is not None and buy:
                # Bands add the network operator's own energy-fee structure on
                # top of the supplier tariff `Tariff.compose` just produced --
                # same Series in, same Series out, so nothing downstream has to
                # know a network profile exists. Holiday dates are resolved
                # for exactly the dates this horizon touches (two or three,
                # normally), not the whole calendar year.
                dates = self.network_calendar.dates_in(buy)
                entity_id = next(iter(self.network_calendar.holiday_entities()), None)
                network_warnings = await self.holiday_cache.async_ensure(
                    self.hass, entity_id, dates
                )
                buy, sell = self.network_calendar.apply_bands(buy, sell, self.holiday_cache)

        settings = self._forecast_profile_settings()
        if EMHASS_CONF_LOAD_FORECAST_METHOD in settings:
            method, load_bootstrap = await self._resolve_load_forecast_method(
                settings, now, horizon_end
            )
            settings[EMHASS_CONF_LOAD_FORECAST_METHOD] = method
            if load_bootstrap is not None:
                load = load_bootstrap

        # EMHASS skips its own history fetch only when the load series is a list
        # and so is the weather forecast (a PV series was sent) -- or PV is off.
        pv_used = bool(config.pv.key) and settings.get("set_use_pv", True) is not False
        if action == ACTION_MPC and pv_used and (not load or not pv):
            load = (
                await self._ensure_history_fetchable(settings, now, horizon_end, bool(load)) or load
            )

        # Whether this request will carry a load series at all -- read off the
        # same emptiness test payload.build_payload uses to decide whether to
        # emit ``load_power_forecast``, so the two can never disagree about
        # what was actually sent. A profile that supplies its own series
        # (forecast from an entity) counts here exactly like a bootstrap one:
        # what matters to EMHASS is only whether the key is present.
        self._sent_load_list = bool(load)

        # Sourceless loads (no power sensor, no control entity) get their
        # accumulator advanced from the *previous* run's plan before it is
        # replaced -- the only chance to see it, and the reason this must run
        # before deferrable_loads() below reads completed_timesteps out of it.
        if self.data is not None:
            self.loads.assume_from_plan(self.data.plan, self.data.load_order, now)
        # Which loads that same plan already commits to at this instant -- read
        # before it is replaced, for the same reason, and the only thing that
        # lets the payload distinguish a block this integration scheduled from
        # an appliance that started on its own. Unconditional: with no previous
        # data every load must be reset to uncommitted, not left as it was.
        self.loads.adopt_plan_commitments(
            self.data.plan if self.data else None,
            self.data.load_order if self.data else [],
            now,
            untrusted=self.plan_is_stale or self.plan_is_degraded,
        )
        # Every load, every cycle. Deliberately outside the branch above and
        # not folded back into assume_from_plan: a request or forced run that
        # has had what it asked for must clear itself whether or not there is a
        # previous plan, and whether or not this load is one of the sourceless
        # ones that plan could speak for. Ordered between the two calls because
        # it reads the accumulator assume_from_plan just advanced, and writes
        # the armed flag apply_surplus below reads back.
        self.loads.check_auto_disarm(now, config.time_step_minutes)
        self._track_never_started_issues()
        # Read before apply_surplus rather than beside decide_end_soc below:
        # the night-cover ceiling is sized against how full the battery already
        # is, so the budget cannot be derived without it. A pure state read
        # with nothing else riding on where it happens.
        soc_init = self._read_soc()
        if self.data is not None:
            # Re-derive each surplus load's hours and window from the spare PV
            # the previous plan predicted. Must run *after*
            # assume_from_plan (whose accumulator feeds the energy cap) and
            # before deferrable_loads() below reads the budget back out.
            self.loads.apply_surplus(
                self.data.plan,
                self.data.load_order,
                now,
                config.time_step_minutes,
                battery=config.battery,
                hybrid=config.hybrid_inverter,
                soc_now=soc_init,
            )
            # Same lagged pairing, right beside it: latch or release each
            # flagged load's held battery-lockout window from the same
            # previous plan, before deferrable_loads() below projects it.
            self.loads.apply_battery_lockout(
                self.data.plan, self.data.load_order, now, config.time_step_minutes
            )
        end_soc: EndSocDecision | None = None
        if config.battery.enabled and soc_init is not None:
            load_for_terminal, load_source = self._load_for_terminal(load)
            end_soc = decide_end_soc(
                mode=config.battery.end_soc_mode,
                soc_init=soc_init,
                battery=config.battery,
                now=now,
                horizon_end=horizon_end,
                step=step,
                pv=pv or None,
                load=load_for_terminal,
                load_source=load_source,
                buy_price=buy,
                # Both sides of the meter: the shipping rule only lets the
                # night cover go early when selling it beats buying it back,
                # which needs the export price and the export limit.
                sell_price=sell,
                grid=config.grid,
                previous=self._end_soc,
                price_real_end=price_source_end,
            )
            self._track_pv_tail_issue(end_soc, pv or None, horizon_end)
            annotate_final_reward(
                end_soc,
                config.battery.soc_final_reward_factor,
                sent=self.soc_final_reward_supported,
                min_version=MIN_EMHASS_VERSION_SOC_FINAL_REWARD,
            )
        self._end_soc = end_soc

        demand_pricing = self.demand_charge_pricing(now)
        current_period_peak_w: float | list[float] | None = None
        capacity_interval_timesteps: int | list[int] = (
            demand_pricing.interval_timesteps if demand_pricing else 1
        )
        capacity_interval_history_w: list[float] | list[list[float]] | None = None
        rates = demand_pricing.effective_rates if demand_pricing else None
        trackers = self.peak_trackers or (
            [self.peak_tracker] if self.peak_tracker is not None else []
        )
        if demand_pricing is not None and rates is not None and action == ACTION_MPC and trackers:
            # Watts, matching every other power figure this integration sends
            # -- current_period_peak is MPC-only in EMHASS (see
            # docs/network_tariffs_plan.md, "Day-ahead versus MPC"), so a
            # day-ahead run gets no floor and no memory either way.
            peaks_w = [tracker.floor_kw * 1000 for tracker in trackers[: len(rates)]]
            # Pad / truncate to K if trackers and rates somehow diverge.
            while len(peaks_w) < len(rates):
                peaks_w.append(0.0)
            peaks_w = peaks_w[: len(rates)]
            ns = demand_pricing.interval_timesteps_list
            histories = [
                self._capacity_interval_history_w(
                    now,
                    step,
                    ns[i] if i < len(ns) else 1,
                    trackers[i] if i < len(trackers) else None,
                )
                for i in range(len(rates))
            ]
            if len(rates) == 1:
                current_period_peak_w = peaks_w[0]
                capacity_interval_timesteps = ns[0] if ns else 1
                capacity_interval_history_w = histories[0]
            else:
                current_period_peak_w = peaks_w
                capacity_interval_timesteps = ns[0] if ns and len(set(ns)) == 1 else ns
                capacity_interval_history_w = histories

        # Mechanism 3 (docs/network_tariffs_plan.md): the windowed hard cap.
        # Gated on the same version as the priced peak -- maximum_power_from_grid
        # as a *list* is verified from v0.18.0 on, same as current_period_peak,
        # and an older backend would either reject it or (per
        # _prepare_power_limit_array) silently collapse a wrong-length list to
        # its first element repeated, which is worse than not sending one.
        calendar = self.network_calendar
        capacity_limit_w: float | None = None
        capacity_limit_window: Callable[[datetime], bool] | None = None
        demand_fallback_ceiling_w: float | None = None
        demand_window: Callable[[datetime], bool] | None = None
        demand_charge_window: (
            Callable[[datetime], bool] | list[Callable[[datetime], bool]] | None
        ) = None
        array_capable = self.backend_version is not None and version_at_least(
            self.backend_version, MIN_EMHASS_VERSION_DEMAND_CHARGE
        )
        if array_capable and calendar is not None:
            if (
                calendar.capacity_limit is not None
                and calendar.capacity_limit.ceiling_kw is not None
            ):
                # An explicit subscribed-tier ceiling, independent of whether
                # the demand charge itself can be priced.
                capacity_limit_w = calendar.capacity_limit.ceiling_kw * 1000

                def capacity_limit_window(when: datetime) -> bool:
                    return calendar.in_capacity_window(when, self.holiday_cache)

            if (
                demand_pricing is not None
                and demand_pricing.sheet_rate is not None
                and demand_pricing.effective_rate_per_kw is None
                and self.peak_target_kw is not None
                and calendar.demand_charges
            ):
                # The peak cannot be safely priced right now (old backend, or
                # a real window this backend cannot mask) but a rate is
                # configured, so a windowed demand charge is still enforced --
                # as a ceiling instead of a price. See number.PeakTargetNumber.
                # For K>1 the fallback window is the union of every component.
                demand_fallback_ceiling_w = self.peak_target_kw * 1000

                def demand_window(when: datetime) -> bool:
                    return calendar.in_any_demand_window(when, self.holiday_cache)

            if (
                demand_pricing is not None
                and rates is not None
                and demand_pricing.any_windowed
                and calendar.demand_charges
            ):
                # The peak *is* being priced, and at least one component window
                # is restricted, so the mask must go with it -- see "The window
                # mask" in docs/network_tariffs_plan.md. demand_pricing already
                # confirmed the backend supports it before setting windowed.
                if len(calendar.demand_charges) == 1:

                    def demand_charge_window(when: datetime) -> bool:
                        return calendar.in_demand_window(when, self.holiday_cache)

                else:
                    demand_charge_window = [
                        (
                            lambda when, *, _i=index: calendar.in_demand_window(
                                when, self.holiday_cache, index=_i
                            )
                        )
                        for index in range(len(calendar.demand_charges))
                    ]

        inputs = PayloadInputs(
            action=action,
            now=now,
            backend_version=self.backend_version,
            use_pv=bool(config.pv.key),
            num_lags=config.dayahead_num_lags,
            row_zero_mode=self._row_zero_mode,
            time_step_minutes=config.time_step_minutes,
            horizon_steps=config.horizon_steps,
            battery=config.battery,
            grid=config.grid,
            hybrid_inverter=config.hybrid_inverter,
            loads=self.deferrable_loads(now),
            load_groups=self.load_groups(),
            pv=pv or None,
            load=load or None,
            buy_price=buy,
            sell_price=sell,
            price_source_end=price_source_end,
            outdoor_temperature=outdoor or None,
            soc_init=soc_init,
            soc_final=end_soc.soc if end_soc else None,
            pv_live_w=self._read_pv_live(),
            load_live_w=self._read_load_live(),
            grid_import_limit_w=self._grid_import_limit_w(config),
            grid_export_limit_w=self._read_grid_limit(config.grid.export_limit_entity, "export"),
            mix_beta=self.mix_beta,
            cost_fun=self.cost_fun,
            extra_settings=settings,
            network_demand_charge_configured=demand_pricing is not None,
            flat_demand_charge_per_kw=self.flat_demand_charge_per_kw,
            demand_charge_rate_per_kw=(
                rates if rates is not None and len(rates) > 1 else (rates[0] if rates else None)
            ),
            current_period_peak_w=current_period_peak_w,
            capacity_interval_timesteps=capacity_interval_timesteps,
            capacity_interval_history_w=capacity_interval_history_w,
            capacity_limit_w=capacity_limit_w,
            capacity_limit_window=capacity_limit_window,
            demand_fallback_ceiling_w=demand_fallback_ceiling_w,
            demand_window=demand_window,
            demand_charge_window=demand_charge_window,
            send_charge_power_derating=(
                self.backend_version is not None
                and version_at_least(self.backend_version, MIN_EMHASS_VERSION_CHARGE_DERATING)
            ),
            send_soc_final_reward=self.soc_final_reward_supported,
        )
        built = build_payload(inputs)
        for series, method, label in (
            (buy, "load_cost_forecast_method", "buy price"),
            (sell, "production_price_forecast_method", "sell price"),
        ):
            if not series and method in built.defaulted_forecast_methods:
                message = (
                    f"No configured {label} forecast is available; using Companion's "
                    "fallback tariff. Configure a price source before relying on this plan."
                )
                built.warnings.append(message)
                _LOGGER.warning(message)
        if network_warnings or price_warnings:
            built.warnings = [*price_warnings, *network_warnings, *built.warnings]
        return inputs, built

    # -- input gathering ------------------------------------------------------

    async def _fill_spot_from_forecast(
        self, spot: Series, now: datetime, until: datetime
    ) -> tuple[Series, datetime | None, str | None]:
        """Stretch ``spot`` to the horizon with the price forecast, if it can.

        Returns ``(series, real_end, warning)``. ``real_end`` is where the real
        source stopped, set only when a forecast was actually used, so the
        price-horizon observation keeps measuring the source and not the fill.
        Any failure leaves ``spot`` untouched: the payload builder then repeats
        the previous day as it always did. Warn only when published price
        intervals do not cover the next 24 hours, not for lookahead-only fills.
        """
        assert self.price_forecast is not None
        real_until = spot.end + (spot.step() or timedelta())
        warn = real_until < now + timedelta(hours=24)
        try:
            forecast = await self.price_forecast.async_get(now)
            fill = fill_with_forecast(spot, forecast, until)
        except PriceForecastError as err:
            return spot, None, f"Price forecast unavailable: {err}." if warn else None
        if not warn:
            return fill.series, spot.end, None
        source = (
            f"{fill.forecast_weight:.0%} forecast, {1 - fill.forecast_weight:.0%} previous day"
            if fill.forecast_weight > 0
            else "previous-day prices"
        )
        return (
            fill.series,
            spot.end,
            f"Estimated prices from {dt_util.as_local(fill.forecast_from):%d %b %H:%M %Z}: {source}.",
        )

    async def _series(self, selection: ProfileSelection, kind: str) -> Series:
        profile = self._profile(selection, kind)
        if profile is None or not profile.produces_series:
            return Series.empty()
        return await async_resolve_series(self.hass, profile, selection.options)

    def _settings(self, selection: ProfileSelection, kind: str) -> dict[str, Any]:
        """The ``emhass:`` settings one configured profile contributes.

        Routed through :meth:`_profile` for the same reason :meth:`_series` is:
        a settings-only profile that no longer resolves (deleted, renamed)
        would otherwise contribute nothing *silently*, and the run would go out
        with EMHASS falling back to its own persisted config. Failing here means
        ``_async_run``'s ProfileError handler names the missing profile.
        """
        profile = self._profile(selection, kind)
        if profile is None:
            return {}
        return resolve_settings(self.hass, profile, selection.options)

    def _profile(self, selection: ProfileSelection, kind: str) -> Profile | None:
        if not selection.key:
            return None
        profile = self.profiles.get(selection.key)
        if profile is None:
            raise ProfileError(
                f"The configured {kind} profile '{selection.key}' is no longer "
                "available. Reconfigure the integration to choose another."
            )
        return profile

    @property
    def soc_percent(self) -> float | None:
        """Battery state of charge as a percentage, for inverter templates."""
        fraction = self._read_soc()
        return None if fraction is None else fraction * 100

    def _read_soc(self) -> float | None:
        """Current battery state of charge, as a fraction.

        Home Assistant reports SOC as a percentage; EMHASS wants a fraction.
        Converting here, once, is what keeps the two conventions from being
        mixed up downstream.
        """
        if not self.config.battery.enabled or not self.config.soc_entity:
            return None
        state = self.hass.states.get(self.config.soc_entity)
        if state is None or state.state in ("unknown", "unavailable", ""):
            _LOGGER.warning(
                "Battery SOC entity %s is unavailable; EMHASS will fall back to "
                "its configured initial SOC",
                self.config.soc_entity,
            )
            return None
        try:
            return min(max(float(state.state) / 100.0, 0.0), 1.0)
        except ValueError:
            _LOGGER.warning(
                "Battery SOC entity %s has a non-numeric state: %s",
                self.config.soc_entity,
                state.state,
            )
            return None

    def _unreadable_load_sensor(self, action: str, payload: dict[str, Any]) -> str | None:
        """The load sensor EMHASS could not read this run, or None if it can.

        Every load method but ``list`` leaves EMHASS to fetch
        ``sensor_power_load_no_var_loads`` itself, and an MPC run blends that
        reading into its own first forecast step (see ``build_payload``). With
        the entity unavailable that blend is NaN, which EMHASS does not guard:
        it fails inside its forecaster with "cannot convert float NaN to
        integer" and answers 500 with an HTML error page naming neither the
        sensor nor the reason. The entity is ours to name, so the run stops
        here instead. The plan in hand is kept either way -- the difference is
        that this way the repair says which sensor to go and fix.

        Checked against the built payload rather than the configured profile
        so that what is verified is exactly what would have been sent.
        """
        if action != ACTION_MPC:
            return None
        if payload.get(EMHASS_CONF_LOAD_FORECAST_METHOD) == LOAD_FORECAST_METHOD_LIST:
            return None
        entity_id = payload.get(EMHASS_CONF_SENSOR_LOAD)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return str(entity_id)
        return None

    def _grid_import_limit_w(self, config: EmhassConfig) -> float | None:
        """The live import limit: the phase guard's, a limit sensor's, or both.

        Both may be configured -- someone moving off a hand-written template
        may keep it for a while -- and then the lower one wins, since each is
        only ever allowed to make the plan more careful. None from both leaves
        the static limit in charge, as before.
        """
        candidates = [
            self._read_grid_limit(config.grid.import_limit_entity, "import"),
            self.phase_guard.import_limit_w if self.phase_guard is not None else None,
        ]
        readings = [value for value in candidates if value is not None]
        return min(readings) if readings else None

    def _read_grid_limit(self, entity_id: str | None, direction: str) -> float | None:
        """A live grid import/export limit, in watts, or None to use the static one.

        Read fresh rather than averaged like ``_read_pv_live``: the sensors
        this accepts are commonly templates over a house meter, and smoothing
        one that already smooths itself would only add lag to a limit whose
        whole point is to track the connection right now. Anyone feeding it a
        noisy raw reading is better served by a statistics helper on their
        side, where the window can match their own fuse's thermal behaviour.

        Every failure resolves to None, which ``payload.resolve_grid_limit``
        turns back into the configured static limit. A sensor that breaks must
        not be able to stop the plan.
        """
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            _LOGGER.warning(
                "Grid %s limit entity %s is unavailable; falling back to the "
                "configured static limit",
                direction,
                entity_id,
            )
            return None
        try:
            value = float(state.state)
        except ValueError:
            _LOGGER.warning(
                "Grid %s limit entity %s has a non-numeric state: %s",
                direction,
                entity_id,
                state.state,
            )
            return None
        if value < 0:
            # Both limits are magnitudes in EMHASS. A negative reading means
            # the sensor is signed the other way round, and passing it through
            # would clamp the limit to zero on every run.
            _LOGGER.warning(
                "Grid %s limit entity %s reads a negative power (%s W); it "
                "should report the limit as a positive number of watts",
                direction,
                entity_id,
                value,
            )
            return None
        return value

    def _read_pv_live(self) -> float | None:
        """PV power for blending into the MPC forecast, in watts.

        Averaged over the trailing ``time_step_minutes`` (see
        ``async_start_pv_live_tracking``) rather than read fresh here: a
        single sample taken at MPC-run time can catch the entity mid-swing
        (a cloud edge, a noisy inverter reading), which blend_at would then
        treat as the current state of the world for the whole first forecast
        step.
        """
        if not self.config.pv_live_entity:
            return None
        average = self._pv_live_average.average(
            dt_util.utcnow(), timedelta(minutes=self.config.time_step_minutes)
        )
        if average is None:
            _LOGGER.warning(
                "Live PV power entity %s is unavailable; the MPC forecast will "
                "not be blended with a live value this run",
                self.config.pv_live_entity,
            )
        return average

    def _read_load_live(self) -> float | None:
        """Current house load power, in watts, for blending into the MPC forecast.

        Only available through the "House load sensor" load profile
        (``load/sensor``) -- that is the one profile that already asks for a
        live load-power sensor (to build EMHASS's own history-based forecast
        from), so this reads the same entity rather than asking the user to
        pick it a second time. Any other load profile has no live sensor to
        read.
        """
        if self.config.load.key != PROFILE_KEY_LOAD_SENSOR:
            return None
        entity_id = self.config.load.options.get("entity")
        if not entity_id:
            return None
        return self._read_live_power(entity_id, "Live load power")

    def _read_live_power(self, entity_id: str, label: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            _LOGGER.warning(
                "%s entity %s is unavailable; the MPC forecast will not be "
                "blended with a live value this run",
                label,
                entity_id,
            )
            return None
        return self._parse_live_power(state, entity_id, label)

    def _parse_live_power(self, state: State, entity_id: str, label: str) -> float | None:
        try:
            return float(state.state)
        except ValueError:
            _LOGGER.warning(
                "%s entity %s has a non-numeric state: %s",
                label,
                entity_id,
                state.state,
            )
            return None

    # -- convenience ----------------------------------------------------------

    async def async_run_dayahead(self) -> None:
        await self.async_run(ACTION_DAYAHEAD)

    async def async_run_mpc(self) -> None:
        await self.async_run(ACTION_MPC)

    async def async_run_forecast_fit(self) -> None:
        """Train EMHASS's mlforecaster model on the configured load sensor's history.

        Deliberately bypasses :meth:`async_run`: a fit produces no plan and no
        ``last-run`` outcome worth storing in ``self.data``, it just trains and
        saves a model file. Fit and implicit prediction use the shared resolver,
        independently of EMHASS's saved configuration.
        """
        async with self._ml_fit_lock:
            try:
                await self._async_fit_forecast(self._forecast_profile_settings())
            except ProfileError as err:
                raise HomeAssistantError(f"Forecast model was not trained: {err}") from err

    async def _async_fit_forecast(self, settings: dict[str, Any]) -> None:
        runtime_params = self._effective_ml_settings(
            settings | {EMHASS_CONF_LOAD_FORECAST_METHOD: LOAD_FORECAST_METHOD_MLFORECASTER}
        )
        if not runtime_params.get(EMHASS_CONF_SENSOR_LOAD) or not runtime_params.get(
            EMHASS_CONF_VAR_MODEL
        ):
            raise EmhassError("Select a load sensor before training the load forecaster")
        self._ml_trained_settings = None
        self._ml_trained_at = None
        await self._async_save_ml_state()
        await self.client.async_run_action(ACTION_FORECAST_FIT, runtime_params)
        self._ml_trained_settings = runtime_params
        self._ml_trained_at = dt_util.utcnow()
        await self._async_save_ml_state()
        self._track_ml_not_ready_issue(False)

    async def _has_history_since(self, entity_id: str, lookback: timedelta) -> bool:
        """Whether ``entity_id``'s recorder history reaches back ``lookback``.

        Mirrors exactly what EMHASS's own forecast-model-fit (for
        :data:`ML_MIN_HISTORY_DAYS`) would see when it pulls the same sensor's
        history through this same Home Assistant's history API -- so this
        never judges "ready" when the fit would not be, or vice versa (a purge
        policy shorter than the lookback would make them fail the same way
        regardless of what this reports).
        """
        try:
            instance = get_instance(self.hass)
        except (KeyError, RuntimeError):
            return False
        cutoff = dt_util.utcnow() - lookback
        try:
            states = await instance.async_add_executor_job(
                history.state_changes_during_period,
                self.hass,
                cutoff,
                cutoff + timedelta(minutes=1),
                entity_id,
                True,  # no_attributes
                False,  # descending
                1,  # limit
                True,  # include_start_time_state
            )
        except Exception:  # a history probe must never break a run
            _LOGGER.debug("Could not check recorder history for %s", entity_id, exc_info=True)
            return False
        return bool(states.get(entity_id))

    async def _has_enough_history(self, entity_id: str) -> bool:
        """Whether ``entity_id`` has :data:`ML_MIN_HISTORY_DAYS` of recorder history."""
        return await self._has_history_since(entity_id, timedelta(days=ML_MIN_HISTORY_DAYS))

    async def _recent_load_series(self, entity_id: str, now: datetime) -> Series:
        """``entity_id``'s own recorded power over the last :data:`LOAD_BOOTSTRAP_LOOKBACK`.

        Real measured watts, however little of it exists -- the raw material
        :meth:`_load_forecast_fallback` repeats forward to bootstrap a load
        forecast until a trained mlforecaster takes over.

        Averaged onto the optimisation timestep before it is returned, so the
        series is sized by the horizon rather than by how chattily the sensor
        reports (see :meth:`Series.resampled`).
        """
        try:
            instance = get_instance(self.hass)
        except (KeyError, RuntimeError):
            return Series.empty()
        cutoff = now - LOAD_BOOTSTRAP_LOOKBACK
        step = timedelta(minutes=self.config.time_step_minutes)

        def read() -> Series:
            """Fetch and reduce within the one executor job.

            The reduction belongs on this side of the thread boundary every
            bit as much as the fetch does. Two days of a house power sensor
            reporting every few seconds is tens of thousands of points, and
            building a Series from them -- let alone walking one forward a
            timestep at a time in extended_with_previous_day -- is long
            enough on a small host to cost Home Assistant its responsiveness
            on every single optimisation cycle.
            """
            states = history.state_changes_during_period(
                self.hass,
                cutoff,
                now,
                entity_id,
                False,  # no_attributes -- state_to_watts needs the unit
                False,  # descending
                None,  # limit
                True,  # include_start_time_state
            )
            points = [
                Point(state.last_changed, watts)
                for state in states.get(entity_id, [])
                if (watts := state_to_watts(state)) is not None
            ]
            return Series(points).resampled(step)

        try:
            return await instance.async_add_executor_job(read)
        except Exception:  # a history probe must never break a run
            _LOGGER.debug("Could not fetch recorder history for %s", entity_id, exc_info=True)
            return Series.empty()

    async def _load_forecast_fallback(
        self, sensor: str | None, now: datetime, horizon_end: datetime
    ) -> tuple[str, Series | None]:
        """What to actually send instead of "mlforecaster" for this run.

        Always a series this integration builds itself: whatever real readings
        the sensor already has, repeated a day at a time (:meth:`Series.
        extended_with_previous_day`) to cover the horizon, or -- for a sensor
        with no history at all yet -- its single current live reading held
        flat across it. Supplying a load series at all makes EMHASS use it
        verbatim regardless of the method named here (see
        payload.build_payload), so the method returned alongside it is
        informational only.

        Deliberately *not* EMHASS's own "naive" method, at any amount of
        history. Naive means sending no series at all, and a request carrying
        no ``load_power_forecast`` is precisely the one shape that makes
        ``naive-mpc-optim`` fetch a day of history from Home Assistant itself
        (EMHASS skips that fetch only when the load forecast is a list --
        ``_get_naive_mpc_history`` in its command_line.py). What that fetch
        asks for, and how it copes when the answer is thin, is decided by
        EMHASS's own persisted configuration, which this integration neither
        controls nor can inspect -- and a sensor young enough to still be on
        this fallback is exactly the one likeliest to come back too sparse to
        resample onto a full horizon.

        So the rung buys nothing and risks the run: "naive" is EMHASS
        repeating the last recorded day of the same sensor, out of the same
        recorder :meth:`_recent_load_series` reads, which is what is computed
        here anyway. Doing it in-house keeps the request self-contained all
        the way up to a trained mlforecaster -- and a fallback that needs the
        rest of the system to be healthy is not a fallback.

        Falls all the way back to EMHASS's own "typical" only when there is
        no sensor configured, or the sensor has never reported a usable
        reading at all -- there is nothing left to build a series from.
        """
        if not sensor:
            return LOAD_FORECAST_METHOD_TYPICAL, None

        step = timedelta(minutes=self.config.time_step_minutes)
        recent = await self._recent_load_series(sensor, now)
        if not recent:
            state = self.hass.states.get(sensor)
            live = state_to_watts(state) if state else None
            if live is None:
                return LOAD_FORECAST_METHOD_TYPICAL, None
            # Onto the same grid as the recorder path, so that a sensor with
            # no history yet does not start the horizon at an arbitrary
            # fraction of a second past the timestep boundary.
            recent = Series([Point(now, live)]).resampled(step)
        return LOAD_FORECAST_METHOD_LIST, recent.extended_with_previous_day(
            ceil_to_step(horizon_end, step), step
        )

    async def _resolve_load_forecast_method(
        self, settings: dict[str, Any], now: datetime, horizon_end: datetime
    ) -> tuple[str, Series | None]:
        """The load forecast method (and optional bootstrap series) to send this run.

        Passes ``settings[EMHASS_CONF_LOAD_FORECAST_METHOD]`` through unchanged
        (with no bootstrap series) unless it asks for mlforecaster and that has
        not been confirmed trained with the current effective settings.
        Sensor, timestep, lag, model and preprocessing changes require a new
        fit: runtime overrides cannot change an already trained model file.

        Falls back to :meth:`_load_forecast_fallback` until a fit against the
        current sensor actually succeeds, auto-triggering that fit itself the
        first time enough recorder history exists rather than waiting on the
        "Train load forecaster" action to be called by hand. A sensor with
        less than :data:`ML_MIN_HISTORY_DAYS` of history (most commonly a
        sensor "Create a house load sensor" only just built) is left alone
        until it has enough -- there is nothing to train on yet.

        A trained model is refitted every :data:`_ML_REFIT_INTERVAL`, because a
        fitted model never learns from the data it later predicts on. A failed
        refit does not keep the old model: it falls back and retries as for a
        first fit.

        Also falls back, whatever the configured method, for
        :data:`_NO_LIST_RETRY_COOLDOWN` after a run that left the forecast to
        EMHASS failed -- see :meth:`_note_failed_run`. That check comes first
        because it is not about mlforecaster in particular: "typical" and
        "naive" send no series either, and fail the same way for the same
        reason.
        """
        if (
            self._no_list_run_failed_at is not None
            and now - self._no_list_run_failed_at < _NO_LIST_RETRY_COOLDOWN
        ):
            return await self._load_forecast_fallback(
                settings.get(EMHASS_CONF_SENSOR_LOAD), now, horizon_end
            )

        method = settings.get(EMHASS_CONF_LOAD_FORECAST_METHOD)
        if method != LOAD_FORECAST_METHOD_MLFORECASTER:
            return method, None

        effective = self._effective_ml_settings(settings)
        sensor = effective.get(EMHASS_CONF_SENSOR_LOAD)
        if sensor and self._ml_model_matches(effective):
            if (
                await self._ml_refit_due()
                and not self._ml_fit_lock.locked()
                and not self._ml_attempt_cooling_down(now)
            ):
                async with self._ml_fit_lock:
                    if await self._has_enough_history(effective[EMHASS_CONF_VAR_MODEL]):
                        if not await self._async_auto_fit(settings, sensor, now):
                            return await self._load_forecast_fallback(sensor, now, horizon_end)
                    else:
                        # A fit would fail, and clearing the working model first
                        # would then leave nothing -- e.g. after recorder
                        # retention was cut. Keep the old one, look again later.
                        self._ml_last_attempt = now
                if not self._ml_model_matches(effective):
                    return await self._load_forecast_fallback(sensor, now, horizon_end)
            self._track_ml_not_ready_issue(False)
            return method, None
        if not sensor:
            return await self._load_forecast_fallback(sensor, now, horizon_end)

        self._track_ml_not_ready_issue(True)
        if self._ml_fit_lock.locked() or self._ml_attempt_cooling_down(now):
            return await self._load_forecast_fallback(sensor, now, horizon_end)

        async with self._ml_fit_lock:
            if not await self._has_enough_history(effective[EMHASS_CONF_VAR_MODEL]):
                self._track_ml_not_ready_issue(True)
                return await self._load_forecast_fallback(sensor, now, horizon_end)

            if not await self._async_auto_fit(settings, sensor, now):
                return await self._load_forecast_fallback(sensor, now, horizon_end)

        if not self._ml_model_matches(effective):
            return await self._load_forecast_fallback(sensor, now, horizon_end)
        return method, None

    async def _ensure_history_fetchable(
        self,
        settings: dict[str, Any],
        now: datetime,
        horizon_end: datetime,
        has_load_series: bool = False,
    ) -> Series | None:
        """Make the history fetch of a series-less MPC run succeed, or avoid it.

        With no load series in the request, ``naive-mpc-optim`` fetches recent
        history for the load sensor *and*, when PV is used, for
        ``sensor_power_photovoltaics`` -- and skips every day on which either
        has none. That setting otherwise comes from EMHASS's saved
        configuration, where it is the stock ``sensor.power_photovoltaics``
        unless someone renamed it, so the run died with "No data was retrieved
        for any day". It surfaced when the load forecaster finished training,
        the first time a request carried no series. The PV sensor is named
        here: the profile's own, else the live PV entity. With neither, nothing
        can satisfy the fetch, so the in-house series is returned to send instead
        (which only avoids the fetch when a PV series is sent as well; with native
        PV forecasting EMHASS fetches regardless, and the run is left to report it).
        """
        if settings.get(EMHASS_CONF_SENSOR_PV):
            return None
        if self.config.pv_live_entity:
            settings[EMHASS_CONF_SENSOR_PV] = self.config.pv_live_entity
            return None
        sensor = settings.get(EMHASS_CONF_SENSOR_LOAD)
        if not sensor or has_load_series:
            return None
        _LOGGER.warning(
            "No live PV sensor is configured, so EMHASS cannot fetch the history it "
            "needs without a load series; sending the integration's own load series"
        )
        method, series = await self._load_forecast_fallback(sensor, now, horizon_end)
        settings[EMHASS_CONF_LOAD_FORECAST_METHOD] = method
        return series

    def _ml_attempt_cooling_down(self, now: datetime) -> bool:
        return (
            self._ml_last_attempt is not None
            and now - self._ml_last_attempt < _ML_FIT_RETRY_COOLDOWN
        )

    async def _async_auto_fit(self, settings: dict[str, Any], sensor: str, now: datetime) -> bool:
        """Fit on the caller's behalf; the caller holds ``_ml_fit_lock``.

        Returns whether it succeeded. A failure is logged, not raised, so the
        run carries on with the in-house fallback forecast.
        """
        self._ml_last_attempt = now
        try:
            await self._async_fit_forecast(settings)
        except EmhassError as err:
            _LOGGER.warning(
                "Auto-training the load forecaster for %s failed (%s); "
                "falling back for this run instead",
                sensor,
                err,
            )
            return False
        return True

    def _track_ml_not_ready_issue(self, not_ready: bool) -> None:
        """Raise or clear the "mlforecaster not trained yet" repair.

        Informational, not an error: runs keep succeeding on the "naive"/
        bootstrap fallback in the meantime (see _load_forecast_fallback), and
        this clears itself the moment a fit (auto- or button-triggered)
        against the current sensor succeeds.
        """
        if not not_ready:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_ML_FORECASTER_NOT_READY)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_ML_FORECASTER_NOT_READY,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_ML_FORECASTER_NOT_READY,
            translation_placeholders={"days": str(ML_MIN_HISTORY_DAYS)},
        )

    @property
    def plan_is_stale(self) -> bool:
        if not self.data or self.data.last_success is None:
            return True
        return dt_util.utcnow() - self.data.last_success > self.config.stale_after

    @property
    def plan_is_degraded(self) -> bool:
        """Whether the plan on hand is a fallback solve the executor must not act on."""
        plan = self.data.plan if self.data else None
        return plan is not None and plan.optim_status in DEGRADED_OPTIM_STATUSES

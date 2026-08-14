"""Coordinator that gathers inputs, runs EMHASS, and holds the resulting plan."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EmhassClient, EmhassError
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
    DOMAIN,
    EMHASS_CONF_LOAD_FORECAST_METHOD,
    EMHASS_CONF_NUM_LAGS,
    EMHASS_CONF_SENSOR_LOAD,
    EMHASS_CONF_TIME_STEP,
    EMHASS_CONF_VAR_MODEL,
    END_SOC_OPTIMIZED,
    ISSUE_LOAD_NEVER_STARTED,
    ISSUE_ML_FORECASTER_NOT_READY,
    ISSUE_OPTIMIZATION_INFEASIBLE,
    ISSUE_PLAN_SCHEMA,
    ISSUE_PV_TAIL_SHORT,
    ISSUE_RUN_FAILED,
    ISSUE_SOURCES_BLIND,
    LOAD_BOOTSTRAP_LOOKBACK,
    LOAD_FORECAST_METHOD_LIST,
    LOAD_FORECAST_METHOD_MLFORECASTER,
    LOAD_FORECAST_METHOD_TYPICAL,
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
from .deferrable import DeferrableRegistry, state_to_watts
from .health import SourceHealth
from .models import (
    DeferrableLoad,
    DeferrableLoadGroup,
    LastRun,
    Plan,
    Point,
    Series,
    ceil_to_step,
    floor_to_step,
)
from .network_calendar import HolidayCache, NetworkCalendar, NetworkCalendarError
from .payload import PayloadInputs, PayloadResult, build_payload
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
    decide_end_soc,
)
from .util import schema_major

_LOGGER = logging.getLogger(__name__)

# How long an auto-triggered fit failure (or a not-enough-history verdict) is
# trusted before checking again -- long enough that a persistently-down EMHASS
# isn't hammered with an expensive fit request every MPC cycle, short enough
# that recorder history crossing ML_MIN_HISTORY_DAYS is noticed the same day.
_ML_FIT_RETRY_COOLDOWN = timedelta(hours=1)
_ML_STORE_VERSION = 1

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
    load_order: list[str] = field(default_factory=list)
    end_soc: EndSocDecision | None = None
    last_action: str | None = None
    last_success: datetime | None = None

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
        # Per-date red-day answers behind any `exclude_holidays` calendar,
        # shared between the band sensor and the demand-window predicate a
        # peak tracker is built against -- both need the same "is today a red
        # day" fact, and re-resolving it twice would double the
        # workday.check_date traffic for no benefit.
        self.holiday_cache = HolidayCache()

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
        # Owned by the cost function select. EMHASS's optimisation objective:
        # profit, cost, or self-consumption; see payload.build_payload.
        self.cost_fun = DEFAULT_COST_FUN
        # The last end-SOC decision, fed back into terminal.decide_end_soc as
        # the hysteresis anchor. In-memory only: after a restart the first run
        # simply computes fresh.
        self._end_soc: EndSocDecision | None = None

        self._unsub_tick: Callable[[], None] | None = None
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

        # Sensor mlforecaster has actually been confirmed trained against,
        # persisted so a restart does not throw away a working model and
        # retrain for nothing. None until a fit succeeds or async_load_ml_state
        # restores it; a mismatch against the currently configured sensor is
        # exactly what triggers an auto-fit -- see _resolve_load_forecast_method.
        self._ml_trained_sensor: str | None = None
        self._ml_last_attempt: datetime | None = None
        self._ml_fit_lock = asyncio.Lock()
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

    @callback
    def async_start_clock(self) -> None:
        """Re-publish plan-derived entities as the plan's own clock advances.

        Every sensor that answers "right now" -- should_run, scheduled power,
        the planned series -- reads the plan at ``utcnow()``. Those are
        ``CoordinatorEntity``s, so they are only written when an optimisation
        completes; between runs their published state is frozen at whatever
        the answer was at the *previous* run, however far the plan has since
        moved on.

        That is not merely stale, it is self-sustaining: EMHASS starts its
        horizon at the next timestep boundary after launch, so at the instant
        of every run "now" sits before row zero. A load whose power sensor
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
        if profile is None:
            self.network_calendar = None
            return
        try:
            resolved = resolve_network(self.hass, profile, selection.options)
            self.network_calendar = NetworkCalendar.from_resolved(resolved)
        except (ProfileError, NetworkCalendarError) as err:
            _LOGGER.warning("Network profile '%s' could not be resolved: %s", selection.key, err)
            self.network_calendar = None

    async def async_load_ml_state(self) -> None:
        """Restore which sensor mlforecaster was last confirmed trained against."""
        stored = await self._ml_store.async_load()
        if stored:
            self._ml_trained_sensor = stored.get("trained_sensor")

    async def _async_save_ml_state(self) -> None:
        await self._ml_store.async_save({"trained_sensor": self._ml_trained_sensor})

    def reload_config(self) -> None:
        self.config = EmhassConfig.from_entry(self.hass, self.config_entry)
        self._refresh_network_calendar()

    def load_forecast_settings(self) -> dict[str, Any]:
        """The ``emhass:`` settings the configured load profile contributes.

        Exposed (unlike the private ``_settings`` used for the other kinds)
        because both the "train forecaster" button and the config sync step
        need to know what the load profile resolves to before any run happens.

        A profile that no longer resolves degrades to ``{}`` here rather than
        raising, unlike the run path: every caller of this runs at setup
        (button platform, config sync), where a hard failure would cost the
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

        Gates both the "Train load forecaster" button and the fit-relevant
        settings in :meth:`async_sync_emhass_config` -- neither means anything
        for the "typical"/"naive" methods or for a load profile that supplies
        its own forecast (``forecast_entity``), which never touch mlforecaster
        at all.
        """
        method = self.load_forecast_settings().get(EMHASS_CONF_LOAD_FORECAST_METHOD)
        return method == LOAD_FORECAST_METHOD_MLFORECASTER

    async def async_sync_emhass_config(self) -> None:
        """Push the settings EMHASS must have before any run.

        EMHASS persists its configuration independently of the runtime
        parameters a run sends, and ``/set-config`` is not additive (see
        :meth:`EmhassClient.async_set_config_merged`) -- so rather than trying
        to detect what drifted, this always asserts the complete, currently
        true picture, the same way ``payload.py`` always sends every setting
        rather than only what changed.

        Two things specifically must never be left to drift:

        - ``optimization_time_step`` must match what every run actually sends
          (``time_step_minutes``); a mismatch is a hard crash inside EMHASS's
          skforecast layer when a trained model is asked to predict at a
          different frequency than it was fit at, not a soft error.
        - When the load profile trains mlforecaster, EMHASS's own
          ``sensor_power_load_no_var_loads`` (``retrieve_hass_conf``) and
          ``var_model`` (``optim_conf``) are two independently persisted
          copies of the same sensor; EMHASS's ``_get_ml_param()`` falls back
          to whichever ``var_model`` is currently on disk regardless of a
          per-request ``sensor_power_load_no_var_loads`` override, so the two
          must always be pushed together. ``num_lags`` must cover a full
          day-ahead call in one predict, or EMHASS returns a clean but fatal
          "unable to obtain N lags_opt values" for every day-ahead run.

        Non-fatal on failure: EMHASS being briefly unreachable at setup should
        not cost the whole integration, only leave the next optimisation or
        fit to fail loudly (and diagnosably) instead.
        """
        patch: dict[str, Any] = {EMHASS_CONF_TIME_STEP: self.config.time_step_minutes}

        settings = self.load_forecast_settings()
        if settings.get(EMHASS_CONF_LOAD_FORECAST_METHOD) == LOAD_FORECAST_METHOD_MLFORECASTER:
            sensor = settings.get(EMHASS_CONF_SENSOR_LOAD)
            if sensor:
                patch[EMHASS_CONF_SENSOR_LOAD] = sensor
                patch[EMHASS_CONF_VAR_MODEL] = sensor
                patch[EMHASS_CONF_NUM_LAGS] = self.config.dayahead_num_lags

        try:
            await self.client.async_set_config_merged(patch)
        except EmhassError as err:
            _LOGGER.warning(
                "Could not sync configuration to EMHASS (%s); continuing setup. "
                "The next optimisation or forecaster fit may fail until this "
                "succeeds -- reloading the integration will retry it.",
                err,
            )

    # -- deferrable loads -----------------------------------------------------

    def deferrable_loads(self, now: datetime) -> list[DeferrableLoad]:
        """Live deferrable loads, projected for a request at ``now``.

        Read from the registry rather than the subentries, because the values a
        user adjusts day to day live in entities; the subentry only seeds them.
        """
        return self.loads.to_loads(now, self.config.time_step_minutes)

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

    async def _run(self, action: str) -> EmhassData:
        inputs, built = await self._build(action)
        if (entity_id := self._unreadable_load_sensor(action, built.payload)) is not None:
            raise UpdateFailed(
                f"Load sensor {entity_id} is unavailable, so EMHASS has nothing "
                "to build a load forecast from. Check the integration that "
                "provides it."
            )
        last_run, plan = await self.client.async_optimize(action, built.payload)

        if last_run.status == "error":
            raise UpdateFailed(f"EMHASS reported an error: {last_run.error_message or 'unknown'}")
        if last_run.infeasible:
            # A perfectly successful HTTP call can still carry an unusable plan.
            # Surfacing it keeps the executor from acting on nonsense.
            _LOGGER.warning(
                "EMHASS could not find a feasible solution for %s; keeping the previous plan",
                action,
            )
            plan = self.data.plan if self.data else None
            self._track_infeasible_issue(True, action)
        else:
            self._track_infeasible_issue(False, action)
            if last_run.status == "no-run":
                _LOGGER.warning(
                    "EMHASS reports no completed run after %s; the plan may be missing",
                    action,
                )

        if plan is not None and not self._schema_supported(plan):
            plan = None

        # Trimmed to the horizon that was actually requested: profiles fetch
        # whatever their source publishes (a day-ahead tariff commonly hands
        # back tomorrow's prices too), which is longer than what got solved
        # whenever the horizon is shorter than a day. Left untrimmed, the
        # price sensors run past every plan-derived one (p_pv, p_load,
        # p_grid, p_batt all come from opt_res, capped at horizon_steps rows),
        # which is what stretched the dashboard cards' time window out past
        # where the plan itself stops.
        #
        # Cut on the timestep boundary rather than on the wall-clock instant the
        # run happened to start: EMHASS aligns row zero of its plan to that
        # boundary, so cutting at `now` (a few seconds later) left the price
        # series starting one whole timestep after the plan did, and the price
        # sensors reading "unknown" for the entire timestep in progress -- every
        # run, indefinitely, since the next run arrives before the gap closes.
        step = timedelta(minutes=inputs.time_step_minutes)
        window_start = floor_to_step(inputs.now, step)
        horizon_end = window_start + step * inputs.horizon_steps

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
            load_order=built.load_order,
            end_soc=self._end_soc,
            last_action=action,
            # Only a genuinely successful solve counts. "no-run" and infeasible
            # both leave the staleness watchdog tripped, which is what stops an
            # executor from acting on a plan that was never actually produced.
            last_success=dt_util.utcnow() if last_run.ok else None,
        )

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

    def _track_infeasible_issue(self, infeasible: bool, action: str) -> None:
        """Raise or clear the whole-integration infeasible-run repair.

        EMHASS reports infeasibility for the problem as a whole, with no hint
        at which load caused it, so this is one issue per integration rather
        than per load (contrast :meth:`LoadNumber._check_thermal_reachability`,
        which can name the offending load and so gets its own issue each).
        """
        if not infeasible:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_OPTIMIZATION_INFEASIBLE)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_OPTIMIZATION_INFEASIBLE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_OPTIMIZATION_INFEASIBLE,
            translation_placeholders={"action": action},
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
        if self.loads.has_thermal and config.temperature:
            outdoor = await self._series(config.temperature, PROFILE_KIND_TEMPERATURE)

        buy = sell = None
        network_warnings: list[str] = []
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

        settings: dict[str, Any] = {}
        for selection, kind in (
            (config.price, PROFILE_KIND_PRICE),
            (config.pv, PROFILE_KIND_PV),
            (config.load, PROFILE_KIND_LOAD),
        ):
            settings.update(self._settings(selection, kind))
        if self.loads.has_thermal:
            # A temperature profile may contribute settings instead of a series
            # (EMHASS built-in lets EMHASS fetch Open-Meteo itself).
            settings.update(self._settings(config.temperature, PROFILE_KIND_TEMPERATURE))
        if EMHASS_CONF_LOAD_FORECAST_METHOD in settings:
            method, load_bootstrap = await self._resolve_load_forecast_method(
                settings, now, horizon_end
            )
            settings[EMHASS_CONF_LOAD_FORECAST_METHOD] = method
            if load_bootstrap is not None:
                load = load_bootstrap

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
        # Every load, every cycle. Deliberately outside the branch above and
        # not folded back into assume_from_plan: a request or forced run that
        # has had what it asked for must clear itself whether or not there is a
        # previous plan, and whether or not this load is one of the sourceless
        # ones that plan could speak for. Ordered between the two calls because
        # it reads the accumulator assume_from_plan just advanced, and writes
        # the armed flag apply_surplus below reads back.
        self.loads.check_auto_disarm(now, config.time_step_minutes)
        self._track_never_started_issues()
        if self.data is not None:
            # Re-derive each surplus load's hours and window from the spare PV
            # the previous plan predicted. Must run *after*
            # assume_from_plan (whose accumulator feeds the energy cap) and
            # before deferrable_loads() below reads the budget back out.
            self.loads.apply_surplus(
                self.data.plan, self.data.load_order, now, config.time_step_minutes
            )
        soc_init = self._read_soc()
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
            )
            self._track_pv_tail_issue(end_soc, pv or None, horizon_end)
        self._end_soc = end_soc

        inputs = PayloadInputs(
            action=action,
            now=now,
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
            outdoor_temperature=outdoor or None,
            soc_init=soc_init,
            soc_final=end_soc.soc if end_soc else None,
            pv_live_w=self._read_pv_live(),
            load_live_w=self._read_load_live(),
            grid_import_limit_w=self._read_grid_limit(config.grid.import_limit_entity, "import"),
            grid_export_limit_w=self._read_grid_limit(config.grid.export_limit_entity, "export"),
            mix_beta=self.mix_beta,
            cost_fun=self.cost_fun,
            extra_settings=settings,
        )
        built = build_payload(inputs)
        if network_warnings:
            built.warnings = [*network_warnings, *built.warnings]
        return inputs, built

    # -- input gathering ------------------------------------------------------

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
        with EMHASS falling back to its own persisted config -- precisely what
        :meth:`async_sync_emhass_config` exists to prevent. Failing here means
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
        saves a model file. Runtime parameters are sent explicitly rather than
        relying on the persisted config, for the same reason
        :meth:`async_sync_emhass_config` pushes them proactively -- so this
        works correctly even if that sync has not run yet or failed.
        """
        settings = self.load_forecast_settings()
        runtime_params: dict[str, Any] = {
            EMHASS_CONF_LOAD_FORECAST_METHOD: LOAD_FORECAST_METHOD_MLFORECASTER,
        }
        sensor = settings.get(EMHASS_CONF_SENSOR_LOAD)
        if sensor:
            runtime_params[EMHASS_CONF_SENSOR_LOAD] = sensor
        await self.client.async_run_action(ACTION_FORECAST_FIT, runtime_params)
        if sensor:
            self._ml_trained_sensor = sensor
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
        not been confirmed trained for the *currently* configured sensor.
        Enabling mlforecaster for the first time, or changing which sensor
        feeds it, otherwise means the very next optimisation is a
        guaranteed-failing predict against a stale or entirely missing model
        file (EMHASS's ``_get_ml_param()`` falls back to whatever
        ``var_model`` its last fit used, regardless of what this run asks for
        -- see :meth:`async_sync_emhass_config`).

        Falls back to :meth:`_load_forecast_fallback` until a fit against the
        current sensor actually succeeds, auto-triggering that fit itself the
        first time enough recorder history exists rather than waiting on the
        "Train load forecaster" button to be pressed by hand. A sensor with
        less than :data:`ML_MIN_HISTORY_DAYS` of history (most commonly a
        sensor "Create a house load sensor" only just built) is left alone
        until it has enough -- there is nothing to train on yet.

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

        sensor = settings.get(EMHASS_CONF_SENSOR_LOAD)
        if sensor and sensor == self._ml_trained_sensor:
            self._track_ml_not_ready_issue(False)
            return method, None
        if not sensor:
            return await self._load_forecast_fallback(sensor, now, horizon_end)

        if self._ml_fit_lock.locked() or (
            self._ml_last_attempt is not None
            and now - self._ml_last_attempt < _ML_FIT_RETRY_COOLDOWN
        ):
            return await self._load_forecast_fallback(sensor, now, horizon_end)

        async with self._ml_fit_lock:
            if not await self._has_enough_history(sensor):
                self._track_ml_not_ready_issue(True)
                return await self._load_forecast_fallback(sensor, now, horizon_end)

            self._ml_last_attempt = now
            try:
                await self.async_run_forecast_fit()
            except EmhassError as err:
                _LOGGER.warning(
                    "Auto-training the load forecaster for %s failed (%s); "
                    "falling back for this run instead",
                    sensor,
                    err,
                )
                return await self._load_forecast_fallback(sensor, now, horizon_end)

        return method, None

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

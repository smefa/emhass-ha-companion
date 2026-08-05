"""Coordinator that gathers inputs, runs EMHASS, and holds the resulting plan."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EmhassClient, EmhassError
from .configuration import EmhassConfig, ProfileSelection
from .const import (
    ACTION_DAYAHEAD,
    ACTION_FORECAST_FIT,
    ACTION_MPC,
    CONF_GROUP_LOAD_IDS,
    CONF_GROUP_MAX_POWER,
    CONF_GROUP_MUTUAL_EXCLUSION,
    DEFAULT_MIX_BETA,
    DEFAULT_SURPLUS_THRESHOLD_W,
    DOMAIN,
    EMHASS_CONF_LOAD_FORECAST_METHOD,
    EMHASS_CONF_NUM_LAGS,
    EMHASS_CONF_SENSOR_LOAD,
    EMHASS_CONF_TIME_STEP,
    EMHASS_CONF_VAR_MODEL,
    ISSUE_OPTIMIZATION_INFEASIBLE,
    ISSUE_PLAN_SCHEMA,
    ISSUE_RUN_FAILED,
    LOAD_FORECAST_METHOD_MLFORECASTER,
    MODE_AUTO,
    PROFILE_KEY_LOAD_SENSOR,
    PROFILE_KIND_LOAD,
    PROFILE_KIND_PRICE,
    PROFILE_KIND_PV,
    PROFILE_KIND_TEMPERATURE,
    SUBENTRY_TYPE_LOAD_GROUP,
    SUPPORTED_PLAN_SCHEMA_MAJOR,
)
from .deferrable import DeferrableRegistry
from .models import DeferrableLoad, DeferrableLoadGroup, LastRun, Plan, Series
from .payload import PayloadInputs, PayloadResult, build_payload
from .profiles import (
    Profile,
    ProfileError,
    async_load_profiles,
    async_resolve_series,
    resolve_settings,
)
from .util import schema_major

_LOGGER = logging.getLogger(__name__)


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

        # Owned by the control switch and the mode select. Held here rather than
        # read back out of the state machine so the executor never has to parse
        # entity states to find out whether it is allowed to act.
        self.control_enabled = False
        self.system_mode = MODE_AUTO
        # Owned by the surplus threshold number. Reporting only: it decides
        # what the hub's surplus sensors call a window, and nothing else. Each
        # load budgets against its own power plus its own headroom instead.
        self.surplus_threshold_w = DEFAULT_SURPLUS_THRESHOLD_W
        # Owned by the mix beta number. Weight given to the live PV/load
        # reading when it is blended into the first naive-mpc-optim forecast
        # step; see payload.build_payload and models.Series.blend_first.
        self.mix_beta = DEFAULT_MIX_BETA

        self._unsub_tick: Callable[[], None] | None = None

    # -- surplus --------------------------------------------------------------

    def surplus_series(self) -> Series:
        """Spare PV in the current plan, before surplus loads take any.

        The series every surplus sensor renders, and the same one the budgets
        were derived from -- so what the dashboard shows and what the pool was
        actually given can never disagree.
        """
        if not self.data:
            return Series.empty()
        return self.loads.surplus_series(self.data.plan, self.data.load_order)

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
    def _async_tick(self, _now: datetime) -> None:
        # Not a refresh: no new data is fetched, the same plan is simply read
        # at the new time.
        self.async_update_listeners()

    async def async_load_profiles(self) -> None:
        result = await async_load_profiles(self.hass)
        self.profiles = result.profiles
        self.profile_errors = result.errors

    def reload_config(self) -> None:
        self.config = EmhassConfig.from_entry(self.hass, self.config_entry)

    def load_forecast_settings(self) -> dict[str, Any]:
        """The ``emhass:`` settings the configured load profile contributes.

        Exposed (unlike the private ``_settings`` used for the other kinds)
        because both the "train forecaster" button and the config sync step
        need to know what the load profile resolves to before any run happens.
        """
        return self._settings(self.config.load)

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
        """Gather inputs, run one EMHASS action, and store the result."""
        try:
            data = await self._run(action)
        except ProfileError as err:
            self._track_run_failed_issue(True, action, str(err))
            raise UpdateFailed(f"Data source error: {err}") from err
        except EmhassError as err:
            self._track_run_failed_issue(True, action, str(err))
            raise UpdateFailed(f"EMHASS error: {err}") from err
        except UpdateFailed as err:
            # Raised directly by _run() when EMHASS answers 200 but flags the
            # run itself as failed (last_run.status == "error"), rather than
            # rejecting the request outright.
            self._track_run_failed_issue(True, action, str(err))
            raise

        self._track_run_failed_issue(False, action, "")
        if notify:
            self.async_set_updated_data(data)
        return data

    async def _run(self, action: str) -> EmhassData:
        inputs, built = await self._build(action)
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

        return EmhassData(
            plan=plan,
            last_run=last_run,
            buy_price=inputs.buy_price or Series.empty(),
            sell_price=inputs.sell_price or Series.empty(),
            pv_forecast=inputs.pv or Series.empty(),
            load_forecast=inputs.load or Series.empty(),
            payload=built.payload,
            warnings=built.warnings,
            load_order=built.load_order,
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

    async def _build(self, action: str) -> tuple[PayloadInputs, PayloadResult]:
        config = self.config
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
        if spot:
            buy, sell = config.tariff.compose(self.hass, spot)

        settings: dict[str, Any] = {}
        for selection in (config.price, config.pv, config.load):
            settings.update(self._settings(selection))
        if self.loads.has_thermal:
            # A temperature profile may contribute settings instead of a series
            # (EMHASS built-in lets EMHASS fetch Open-Meteo itself).
            settings.update(self._settings(config.temperature))

        now = dt_util.utcnow()
        # Sourceless loads (no power sensor, no control entity) get their
        # accumulator advanced from the *previous* run's plan before it is
        # replaced -- the only chance to see it, and the reason this must run
        # before deferrable_loads() below reads completed_timesteps out of it.
        if self.data is not None:
            self.loads.assume_from_plan(self.data.plan, self.data.load_order, now)
            # Re-derive each surplus load's hours and window from the spare PV
            # the previous plan predicted. Must run *after*
            # assume_from_plan (whose accumulator feeds the energy cap) and
            # before deferrable_loads() below reads the budget back out.
            self.loads.apply_surplus(
                self.data.plan, self.data.load_order, now, config.time_step_minutes
            )
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
            soc_init=self._read_soc(),
            pv_live_w=self._read_pv_live(),
            load_live_w=self._read_load_live(),
            mix_beta=self.mix_beta,
            extra_settings=settings,
        )
        return inputs, build_payload(inputs)

    # -- input gathering ------------------------------------------------------

    async def _series(self, selection: ProfileSelection, kind: str) -> Series:
        profile = self._profile(selection, kind)
        if profile is None or not profile.produces_series:
            return Series.empty()
        return await async_resolve_series(self.hass, profile, selection.options)

    def _settings(self, selection: ProfileSelection) -> dict[str, Any]:
        if (profile := self.profiles.get(selection.key or "")) is None:
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

    def _read_pv_live(self) -> float | None:
        """Current PV power, in watts, for blending into the MPC forecast."""
        if not self.config.pv_live_entity:
            return None
        return self._read_live_power(self.config.pv_live_entity, "Live PV power")

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
        if sensor := settings.get(EMHASS_CONF_SENSOR_LOAD):
            runtime_params[EMHASS_CONF_SENSOR_LOAD] = sensor
        await self.client.async_run_action(ACTION_FORECAST_FIT, runtime_params)

    @property
    def plan_is_stale(self) -> bool:
        if not self.data or self.data.last_success is None:
            return True
        return dt_util.utcnow() - self.data.last_success > self.config.stale_after

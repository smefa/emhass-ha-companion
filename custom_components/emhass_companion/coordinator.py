"""Coordinator that gathers inputs, runs EMHASS, and holds the resulting plan."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EmhassClient, EmhassError
from .configuration import EmhassConfig, ProfileSelection
from .const import (
    ACTION_DAYAHEAD,
    ACTION_MPC,
    DOMAIN,
    ISSUE_PLAN_SCHEMA,
    MODE_AUTO,
    PROFILE_KIND_LOAD,
    PROFILE_KIND_PRICE,
    PROFILE_KIND_PV,
    SUPPORTED_PLAN_SCHEMA_MAJOR,
)
from .deferrable import DeferrableRegistry
from .models import DeferrableLoad, LastRun, Plan, Series
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
        self.config = EmhassConfig.from_entry(entry)
        self.profiles: dict[str, Profile] = {}
        self.profile_errors: dict[str, str] = {}
        self.data = EmhassData()

        # Owned by the control switch and the mode select. Held here rather than
        # read back out of the state machine so the executor never has to parse
        # entity states to find out whether it is allowed to act.
        self.control_enabled = False
        self.system_mode = MODE_AUTO

    # -- lifecycle ------------------------------------------------------------

    async def async_load_profiles(self) -> None:
        result = await async_load_profiles(self.hass)
        self.profiles = result.profiles
        self.profile_errors = result.errors

    def reload_config(self) -> None:
        self.config = EmhassConfig.from_entry(self.config_entry)

    # -- deferrable loads -----------------------------------------------------

    def deferrable_loads(self, now: datetime) -> list[DeferrableLoad]:
        """Live deferrable loads, projected for a request at ``now``.

        Read from the registry rather than the subentries, because the values a
        user adjusts day to day live in entities; the subentry only seeds them.
        """
        return self.loads.to_loads(now, self.config.time_step_minutes)

    # -- running --------------------------------------------------------------

    async def _async_update_data(self) -> EmhassData:
        return await self.async_run(ACTION_MPC, notify=False)

    async def async_run(self, action: str, *, notify: bool = True) -> EmhassData:
        """Gather inputs, run one EMHASS action, and store the result."""
        try:
            data = await self._run(action)
        except ProfileError as err:
            raise UpdateFailed(f"Data source error: {err}") from err
        except EmhassError as err:
            raise UpdateFailed(f"EMHASS error: {err}") from err

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
        elif last_run.status == "no-run":
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

    async def _build(self, action: str) -> tuple[PayloadInputs, PayloadResult]:
        config = self.config
        spot, pv, load = await asyncio.gather(
            self._series(config.price, PROFILE_KIND_PRICE),
            self._series(config.pv, PROFILE_KIND_PV),
            self._series(config.load, PROFILE_KIND_LOAD),
        )

        buy = sell = None
        if spot:
            buy, sell = config.tariff.compose(self.hass, spot)

        settings: dict[str, Any] = {}
        for selection in (config.price, config.pv, config.load):
            settings.update(self._settings(selection))

        now = dt_util.utcnow()
        inputs = PayloadInputs(
            action=action,
            now=now,
            time_step_minutes=config.time_step_minutes,
            horizon_steps=config.horizon_steps,
            battery=config.battery,
            grid=config.grid,
            loads=self.deferrable_loads(now),
            pv=pv or None,
            load=load or None,
            buy_price=buy,
            sell_price=sell,
            soc_init=self._read_soc(),
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

    # -- convenience ----------------------------------------------------------

    async def async_run_dayahead(self) -> None:
        await self.async_run(ACTION_DAYAHEAD)

    async def async_run_mpc(self) -> None:
        await self.async_run(ACTION_MPC)

    @property
    def plan_is_stale(self) -> bool:
        if not self.data or self.data.last_success is None:
            return True
        return dt_util.utcnow() - self.data.last_success > self.config.stale_after

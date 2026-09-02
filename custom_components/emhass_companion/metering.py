"""Reading the house's own meters, and keeping the day's ledger.

Copyright (C) 2026 Tomas Smedberg.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. It is distributed without any warranty; see the LICENSE file at
the repository root for the full terms.

The Home Assistant half of the savings feature: subscriptions, unit handling,
persistence and the midnight rollover. All the money lives in :mod:`savings`,
which knows nothing about any of this.

Settling happens on **every state change of every source**, not on a timer.
Two things depend on that resolution:

* ``max(net, 0)`` is only exact when the interval is short. Over a
  quarter-hour bucket, a house that imported for five minutes and exported for
  ten nets out to a single direction and the other side's money vanishes.
* Prices are piecewise constant per hour, so multiplying an interval's kWh by
  the price at that instant *is* the exact integral -- as long as intervals are
  short against an hour.

Writing four sensor states on every meter update would be a different problem,
so publication is throttled separately (see ``_PUBLISH_INTERVAL``); the ledger
underneath is always current.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter

from .configuration import EmhassConfig
from .const import (
    CONF_BATTERY_CHARGE_ENERGY_ENTITY,
    CONF_BATTERY_DISCHARGE_ENERGY_ENTITY,
    CONF_GRID_EXPORT_ENERGY_ENTITY,
    CONF_GRID_IMPORT_ENERGY_ENTITY,
    CONF_METERING,
    CONF_METERING_ENABLED,
    CONF_PV_ENERGY_ENTITY,
    DOMAIN,
)
from .savings import Forecast, IntervalEnergy, Ledger, Prices, forecast_costs

_LOGGER = logging.getLogger(__name__)

_STORE_VERSION = 1

_PUBLISH_INTERVAL = timedelta(seconds=60)
"""How often the sensors are allowed to be rewritten.

The ledger updates on every meter tick; only publication is rate-limited.
Sixty seconds keeps the number feeling live without turning four monetary
sensors into a few hundred thousand recorder rows a day -- and a further
"did anything actually move" check below suppresses the quiet hours entirely.
"""

_PUBLISH_THRESHOLD = 0.005
"""Smallest change in any published figure worth a state write. Below the
second decimal every sensor here displays at, so nothing visible is lost, and
it is what stops a sleeping house writing an identical number all night."""

_SAVE_DELAY = 120
"""Seconds to coalesce ledger writes over. The ledger is rebuildable from the
meters within a day, so losing the last two minutes of it to a hard power cut
is not worth writing ``.storage`` on every meter tick."""

_RESTORE_MAX_GAP = timedelta(minutes=15)
"""How long a downtime may be before meter movement across it is disowned.

Under this, the energy is credited at the price in force when Home Assistant
came back -- a small error over a short gap, and much better than losing it.
Over it, there is no honest price to use (the gap likely spans an hour
boundary, possibly several), so the energy is counted into ``unpriced_kwh``
and left uncosted rather than priced at whatever the tariff happens to say now.
"""


def _as_float(state: State | None) -> float | None:
    """A numeric state, or None for anything that is not one."""
    if state is None or state.state in ("unknown", "unavailable", "", "none", "None"):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _energy_kwh(state: State | None) -> float | None:
    """A meter reading normalised to kWh.

    Unit conversion rather than an assumption: ``total_increasing`` energy
    sensors are published in Wh, kWh and MWh by different integrations, and a
    Wh meter read as kWh is a thousandfold error in the user's favour, which is
    exactly the kind of wrong nobody reports. A sensor carrying no unit at all
    is taken at face value as kWh -- the overwhelmingly common case for a
    hand-built template sensor, and the only guess available.
    """
    if (value := _as_float(state)) is None or state is None:
        return None
    unit = state.attributes.get("unit_of_measurement")
    if unit in (None, "", UnitOfEnergy.KILO_WATT_HOUR):
        return value
    try:
        return EnergyConverter.convert(value, unit, UnitOfEnergy.KILO_WATT_HOUR)
    except (ValueError, TypeError, HomeAssistantError):
        return None


def _power_kw(state: State | None) -> float | None:
    """A power reading normalised to kW, sign preserved.

    An unlabelled power sensor is assumed to be watts, the reverse of the
    energy default above and for the same reason: it is what these sensors
    overwhelmingly are.
    """
    if (value := _as_float(state)) is None or state is None:
        return None
    unit = state.attributes.get("unit_of_measurement")
    if unit in (None, ""):
        return value / 1000.0
    if unit == UnitOfPower.KILO_WATT:
        return value
    try:
        return PowerConverter.convert(value, unit, UnitOfPower.KILO_WATT)
    except (ValueError, TypeError, HomeAssistantError):
        return None


class Meter:
    """One quantity, read from either an energy counter or a power sensor.

    Both shapes are supported because both are what people have. An energy
    counter is strictly better -- the device integrated it at its own full
    resolution and we only difference it -- but plenty of houses only expose
    instantaneous power, and integrating that here is far better than refusing
    to compute anything.
    """

    def __init__(self, entity_id: str, *, kind: str, invert: bool = False) -> None:
        self.entity_id = entity_id
        self.kind = kind
        self.invert = invert
        self._last_value: float | None = None
        self._last_time: datetime | None = None

    # -- energy counters ------------------------------------------------------

    def _take_energy_from(self, state: State | None, now: datetime) -> float:
        reading = _energy_kwh(state)
        if reading is None:
            # Unavailable is not zero. Holding the previous reading means the
            # energy that flowed while the meter was out is picked up on the
            # next good sample instead of being lost -- and, because the same
            # branch also refuses to advance ``_last_time``, without pretending
            # it all happened in one instant.
            return 0.0
        previous = self._last_value
        self._last_value = reading
        self._last_time = now
        if previous is None:
            # First sample: the counter's absolute value tells us nothing about
            # what moved. Baseline it and start measuring from here.
            return 0.0
        if reading < previous:
            # A reset. ``total_increasing`` sensors are allowed to do this, and
            # the daily meters most inverters publish do it every midnight by
            # design; the post-reset reading is the energy since the reset.
            return max(reading, 0.0)
        return reading - previous

    def _take_energy(self, hass: HomeAssistant, now: datetime) -> float:
        return self._take_energy_from(hass.states.get(self.entity_id), now)

    # -- power sensors --------------------------------------------------------

    def _take_power_from(self, state: State | None, now: datetime) -> float:
        reading = _power_kw(state)
        previous, since = self._last_value, self._last_time
        self._last_value = reading
        self._last_time = now
        if previous is None or since is None:
            return 0.0
        hours = (now - since).total_seconds() / 3600
        if hours <= 0:
            return 0.0
        if hours > _RESTORE_MAX_GAP.total_seconds() / 3600:
            # A gap this long is a restart or an unavailable sensor, not a
            # steady load. Holding the last power across it would invent
            # kilowatt-hours out of a sensor that was not reporting.
            return 0.0
        return previous * hours

    def _take_power(self, hass: HomeAssistant, now: datetime) -> float:
        return self._take_power_from(hass.states.get(self.entity_id), now)

    # -- shared ---------------------------------------------------------------

    def take_from(self, state: State | None, now: datetime) -> float:
        """:meth:`take`, given an already-known ``State`` instead of a live
        lookup.

        The seam a historical replay walks: the same reset/unit-conversion
        rules, driven from a recorder sample instead of ``hass.states.get``.
        """
        value = (
            self._take_energy_from(state, now)
            if self.kind == "energy"
            else self._take_power_from(state, now)
        )
        return -value if self.invert else value

    def take_signed_from(self, state: State | None, now: datetime) -> tuple[float, float]:
        """:meth:`take_signed`, given an already-known ``State``."""
        value = self.take_from(state, now)
        return max(value, 0.0), max(-value, 0.0)

    def take(self, hass: HomeAssistant, now: datetime) -> float:
        """Energy since the previous take, in kWh, and advance the state."""
        return self.take_from(hass.states.get(self.entity_id), now)

    def take_signed(self, hass: HomeAssistant, now: datetime) -> tuple[float, float]:
        """A signed source split into its two directions, ``(positive, negative)``.

        One sensor, two quantities: a grid meter reporting import as positive
        and export as negative, or a battery power sensor doing the same for
        discharge and charge. Splitting here rather than at the call site keeps
        the sign convention in one place -- and left-Riemann integration means
        the sign is constant across the sub-interval, so the split is exact.
        """
        return self.take_signed_from(hass.states.get(self.entity_id), now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "kind": self.kind,
            "last_value": self._last_value,
            "last_time": self._last_time.isoformat() if self._last_time else None,
        }

    @classmethod
    def seeded(
        cls,
        entity_id: str,
        *,
        kind: str,
        invert: bool = False,
        last_value: float | None,
        last_time: datetime | None,
    ) -> Meter:
        """A meter pre-loaded with a known reading.

        For a historical replay to resume from a gap's own starting point
        rather than baselining blind on its first sample, the way a fresh
        live :class:`Meter` always does.
        """
        meter = cls(entity_id, kind=kind, invert=invert)
        meter._last_value = last_value
        meter._last_time = last_time
        return meter

    def pending_gap(self, data: dict[str, Any] | None, *, now: datetime) -> datetime | None:
        """The stored ``last_time``, if :meth:`restore` would disown it right
        now -- else ``None``.

        Read-only mirror of the same gap test :meth:`restore` applies, for a
        caller that needs to know *which* meters have a gap worth
        reconstructing from history before :meth:`restore` mutates anything.
        """
        if not data or data.get("entity_id") != self.entity_id or data.get("kind") != self.kind:
            return None
        raw_time = data.get("last_time")
        when = dt_util.parse_datetime(raw_time) if isinstance(raw_time, str) else None
        if when is None or now - when > _RESTORE_MAX_GAP:
            return when
        return None

    def restore(self, hass: HomeAssistant, data: dict[str, Any] | None, *, now: datetime) -> float:
        """Re-adopt a stored reading, or disown it if too much time has passed.

        Returns the kWh that moved during the downtime and could *not* be
        priced, for the caller to count into ``unpriced_kwh``. Zero when the
        gap was short enough to price at the current tariff, or when nothing
        was restored.

        The entity id is checked as well as the timestamp: a user who repoints
        this quantity at a different meter has a stored reading that is a
        perfectly plausible number from an entirely different counter, and
        differencing across that would book the gap between two unrelated
        meters as an hour's consumption.
        """
        if not data or data.get("entity_id") != self.entity_id or data.get("kind") != self.kind:
            return 0.0
        raw_time = data.get("last_time")
        when = dt_util.parse_datetime(raw_time) if isinstance(raw_time, str) else None
        if when is None or now - when > _RESTORE_MAX_GAP:
            # Too long ago to price. For an energy counter the movement across
            # the gap is still knowable, so it is reported as unpriced rather
            # than dropped silently; for a power sensor there is nothing to
            # report, only a baseline to reset.
            missed = 0.0
            stored = data.get("last_value")
            if self.kind == "energy" and stored is not None:
                current = _energy_kwh(hass.states.get(self.entity_id))
                if current is not None and current >= stored:
                    missed = current - stored
            self._last_value = None
            self._last_time = None
            return missed
        self._last_value = data.get("last_value")
        self._last_time = when
        return 0.0


@dataclass(slots=True)
class MeterSpec:
    """Which of the user's entities answers for which quantity.

    Built by :func:`build_meters` from the configuration. Every field is
    optional except the grid pair, because everything downstream degrades
    gracefully: no PV meter means no solar/battery split, no battery meters
    means no battery component, and the actual cost is computable from the
    grid alone.
    """

    grid_import: Meter | None = None
    grid_export: Meter | None = None
    pv: Meter | None = None
    battery_charge: Meter | None = None
    battery_discharge: Meter | None = None
    battery_power: Meter | None = None
    house_load: Meter | None = None

    @property
    def usable(self) -> bool:
        """Whether the grid side can be settled at all.

        The one hard requirement. Without it there is no actual cost, and
        without an actual cost there is nothing to compare a baseline against.

        Deliberately a *counter* pair with no power-sensor fallback. Anyone
        whose meter only reports instantaneous grid power has already had to
        build a Riemann-sum helper to use Home Assistant's own Energy
        dashboard, so offering a second, worse integration of the same signal
        here would add a field, a code path and a way to be quietly less
        accurate, for a house that does not exist.
        """
        return self.grid_import is not None and self.grid_export is not None

    @property
    def has_solar(self) -> bool:
        return self.pv is not None

    @property
    def has_battery(self) -> bool:
        return self.battery_power is not None or (
            self.battery_charge is not None and self.battery_discharge is not None
        )

    def entities(self) -> list[str]:
        return sorted(
            {
                meter.entity_id
                for meter in (
                    self.grid_import,
                    self.grid_export,
                    self.pv,
                    self.battery_charge,
                    self.battery_discharge,
                    self.battery_power,
                    self.house_load,
                )
                if meter is not None
            }
        )

    def describe(self) -> dict[str, str]:
        """Which entity and which shape answered for each quantity.

        Published as a sensor attribute. A savings figure nobody can trace back
        to its inputs is a figure nobody will believe, and the difference
        between a metered and an integrated source is exactly the difference
        between "this is exact" and "this is as good as the sensor's update
        rate".
        """
        named = {
            "grid_import": self.grid_import,
            "grid_export": self.grid_export,
            "solar": self.pv,
            "battery_charge": self.battery_charge,
            "battery_discharge": self.battery_discharge,
            "battery_power": self.battery_power,
            "house_load": self.house_load,
        }
        return {
            name: f"{meter.entity_id} ({meter.kind})"
            for name, meter in named.items()
            if meter is not None
        }


def build_meters(config: EmhassConfig, options: dict[str, Any] | None) -> MeterSpec:
    """Resolve the configuration into one meter per quantity.

    Two passes, in priority order. First the meters the user picked on the cost
    tracking screen, which are energy counters and therefore exact. Then, for
    any quantity still unanswered, the power sensors the entry already knows
    about for entirely different reasons -- the live PV feed used to correct
    the first forecast timestep, the whole-house sensor behind the net load
    sensor, the battery power sensor the dashboard cards draw.

    That second pass is what makes the feature work out of the box on a house
    that has never opened the new screen. Integrating a power sensor is less
    accurate than differencing a counter, which is why it is second and why
    :meth:`MeterSpec.describe` publishes which one actually answered.

    The grid is deliberately *not* given a fallback: no counter pair means no
    cost tracking. Deriving grid flow from load minus PV minus battery would
    produce a plausible number carrying three sensors' worth of error, on the
    one quantity that has to be right.

    House load has no field of its own on that screen either. The entry
    already asks for a whole-house power sensor for the load forecast, and the
    energy balance covers the houses that have none -- so a sixth question
    would only be asked of people who could already be answered.
    """
    metering = (options or {}).get(CONF_METERING) or {}
    if not metering.get(CONF_METERING_ENABLED, bool(metering)):
        # Off, or never set up. The forecast sensors still work -- they price
        # the plan, not the meters -- so this returns an empty spec rather than
        # nothing at all.
        return MeterSpec()

    def _energy(key: str) -> Meter | None:
        entity_id = metering.get(key)
        return Meter(entity_id, kind="energy") if entity_id else None

    spec = MeterSpec(
        grid_import=_energy(CONF_GRID_IMPORT_ENERGY_ENTITY),
        grid_export=_energy(CONF_GRID_EXPORT_ENERGY_ENTITY),
        pv=_energy(CONF_PV_ENERGY_ENTITY),
        battery_charge=_energy(CONF_BATTERY_CHARGE_ENERGY_ENTITY),
        battery_discharge=_energy(CONF_BATTERY_DISCHARGE_ENERGY_ENTITY),
    )

    if spec.pv is None and config.pv_live_entity:
        spec.pv = Meter(config.pv_live_entity, kind="power")
    if config.house_load_total_entity:
        spec.house_load = Meter(config.house_load_total_entity, kind="power")
    if (
        spec.battery_charge is None
        and spec.battery_discharge is None
        and config.battery_power_entity
    ):
        # Normalised to EMHASS's convention here and nowhere else: positive is
        # discharge. ``battery_power_invert`` exists precisely because there is
        # no way to tell which way round a battery power sensor is from the
        # sensor itself.
        spec.battery_power = Meter(
            config.battery_power_entity, kind="power", invert=config.battery_power_invert
        )
    return spec


def read_meters(
    meters: MeterSpec, now: datetime, state_of: Callable[[str], State | None]
) -> IntervalEnergy:
    """Take every configured meter once, in the shape :mod:`savings` settles.

    ``state_of`` is the only difference between a live settle and a
    historical replay: ``hass.states.get`` for one, a hold-last recorder
    lookup for the other. Everything else -- which meters are read, how a
    signed battery-power sensor is split, house_load's None-when-unconfigured
    fallback -- lives here once, shared by both.
    """
    imported = (
        meters.grid_import.take_from(state_of(meters.grid_import.entity_id), now)
        if meters.grid_import
        else 0.0
    )
    exported = (
        meters.grid_export.take_from(state_of(meters.grid_export.entity_id), now)
        if meters.grid_export
        else 0.0
    )

    if meters.battery_power is not None:
        # EMHASS's convention throughout this integration: positive is
        # discharge. ``battery_power_invert`` has already been folded into
        # the meter, so the sign here is always the same way round.
        discharge, charge = meters.battery_power.take_signed_from(
            state_of(meters.battery_power.entity_id), now
        )
    else:
        charge = (
            meters.battery_charge.take_from(state_of(meters.battery_charge.entity_id), now)
            if meters.battery_charge
            else 0.0
        )
        discharge = (
            meters.battery_discharge.take_from(state_of(meters.battery_discharge.entity_id), now)
            if meters.battery_discharge
            else 0.0
        )

    return IntervalEnergy(
        imported=imported,
        exported=exported,
        pv=meters.pv.take_from(state_of(meters.pv.entity_id), now) if meters.pv else 0.0,
        battery_charge=charge,
        battery_discharge=discharge,
        house_load=(
            meters.house_load.take_from(state_of(meters.house_load.entity_id), now)
            if meters.house_load
            else None
        ),
    )


@dataclass(frozen=True, slots=True)
class GapSeed:
    """One meter's own starting point for a historical replay.

    Mirrors the arguments to :meth:`Meter.seeded` exactly, because that is
    what a replay builds each of its scratch meters from -- never the live
    tracker's own :class:`Meter` objects.
    """

    entity_id: str
    kind: str
    invert: bool
    last_value: float | None
    last_time: datetime


@dataclass(frozen=True, slots=True)
class PendingGapReplay:
    """A restart gap noted at load time, waiting for real prices to replay
    against.

    Captured once in :meth:`SavingsTracker.async_load`, in memory only --
    never persisted, so a second restart before replay runs simply leaves
    today's standing write-off in place rather than compounding anything.
    """

    window_start: datetime
    window_end: datetime
    missed_kwh: float
    seeds: dict[str, GapSeed]


class SavingsTracker:
    """Keeps the day's ledger current and answers for the forecast.

    Owned by the config entry for its lifetime. The sensors subscribe to it
    rather than to the coordinator, because a meter tick is not an optimisation
    and waiting for the next solve to publish a cost would leave the numbers a
    quarter of an hour stale for no reason.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        meters: MeterSpec,
        *,
        soc_entity: str | None,
        capacity_kwh: float | None,
        prices: Callable[[datetime], Prices | None],
        plan_forecast: Callable[[datetime, timedelta], Forecast | None],
        add_price_listener: Callable[[CALLBACK_TYPE], CALLBACK_TYPE],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.meters = meters
        self._soc_entity = soc_entity
        self._capacity_kwh = capacity_kwh
        self._prices = prices
        self._plan_forecast = plan_forecast
        self._add_price_listener = add_price_listener

        self.ledger = Ledger(day=_local_day(dt_util.utcnow()))
        self._store: Store[dict[str, Any]] = Store(
            hass, _STORE_VERSION, f"{DOMAIN}_{entry.entry_id}_savings"
        )
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._last_published: datetime | None = None
        self._last_snapshot: tuple[float, ...] = ()
        self._pending_gap: PendingGapReplay | None = None
        self._gap_replay_unsub: CALLBACK_TYPE | None = None

    # -- lifecycle ------------------------------------------------------------

    async def async_load(self) -> None:
        """Restore the ledger and the meter baselines from the last run."""
        stored = await self._store.async_load()
        now = dt_util.utcnow()
        if not stored:
            self._seed_storage(now)
            return

        ledger = Ledger.from_dict(stored.get("ledger"))
        if ledger.day != _local_day(now):
            # Home Assistant was down over midnight. The old day is closed as
            # it stood -- it is already in Home Assistant's own statistics --
            # and today starts clean rather than inheriting yesterday's totals.
            # The battery's cost account is the one thing that does carry over:
            # it describes energy that is still physically in the battery, and
            # dropping it would make the first discharge after any overnight
            # restart look free.
            ledger = Ledger(
                day=_local_day(now),
                battery_book_kwh=ledger.battery_book_kwh,
                battery_book_value=ledger.battery_book_value,
                battery_book_value_start=ledger.battery_book_value,
            )
        self.ledger = ledger

        meter_state = stored.get("meters") or {}

        # One pass to see which meters have a gap worth reconstructing, before
        # the restore loop below mutates any of them. A gap is only replayed
        # as a whole: one meter with a fresh baseline (an entity reconfigured
        # mid-gap, say) means mixing a replayed meter with one still carrying
        # a live baseline, which is worse than leaving the lot unpriced.
        gap_seeds: dict[str, GapSeed] = {}
        all_gapped = True
        for name, meter in self._named_meters():
            data = meter_state.get(name)
            when = meter.pending_gap(data, now=now)
            if when is None:
                all_gapped = False
                continue
            gap_seeds[name] = GapSeed(
                entity_id=meter.entity_id,
                kind=meter.kind,
                invert=meter.invert,
                last_value=data.get("last_value"),
                last_time=when,
            )

        unpriced = 0.0
        for name, meter in self._named_meters():
            unpriced += meter.restore(self.hass, meter_state.get(name), now=now)
        if unpriced:
            _LOGGER.debug("Disowning %.3f kWh of meter movement across downtime", unpriced)
            self.ledger.unpriced_kwh += unpriced

        if all_gapped and gap_seeds:
            # Clamped to today: the ledger above already closed out any day
            # this gap crossed into, so there is nothing to replay it against.
            window_start = max(
                min(seed.last_time for seed in gap_seeds.values()),
                dt_util.start_of_local_day(now),
            )
            self._pending_gap = PendingGapReplay(
                window_start=window_start,
                window_end=now,
                missed_kwh=unpriced,
                seeds=gap_seeds,
            )
            self._gap_replay_unsub = self._add_price_listener(self._on_coordinator_updated)
            # Covers a reload where prices are already loaded -- otherwise
            # replay would wait for the next coordinator refresh for no
            # reason, even though the data it needs is already sitting there.
            self._on_coordinator_updated()

        self._seed_storage(now)

    def _seed_storage(self, now: datetime) -> None:
        """Fill in the battery's stored energy for whichever ends are missing."""
        stored = self._stored_kwh()
        if stored is None:
            return
        if self.ledger.stored_start_kwh is None:
            self.ledger.stored_start_kwh = stored
        self.ledger.stored_now_kwh = stored

    @callback
    def async_start(self) -> None:
        """Subscribe to the meters and to midnight."""
        self.async_stop()
        entities = self.meters.entities()
        if self._soc_entity:
            entities = sorted({*entities, self._soc_entity})
        if entities:
            self._unsubs.append(
                async_track_state_change_event(self.hass, entities, self._async_source_changed)
            )
        # A house can be quiet enough that no meter reports for minutes at a
        # time, and the day must still turn over on the hour it is supposed to.
        # Five seconds past, not on the stroke: the inverter's own daily meters
        # reset at midnight too, and letting theirs land first means the reset
        # is attributed to the day that is ending rather than the one starting.
        self._unsubs.append(
            async_track_time_change(self.hass, self._async_midnight, hour=0, minute=0, second=5)
        )
        # One immediate settle so a restart's baselines are taken now rather
        # than whenever the first meter next happens to move.
        self._settle(dt_util.utcnow())

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    async def async_shutdown(self) -> None:
        """Flush the ledger before the entry goes away."""
        self.async_stop()
        if self._gap_replay_unsub is not None:
            self._gap_replay_unsub()
            self._gap_replay_unsub = None
        self._settle(dt_util.utcnow())
        await self._store.async_save(self._data_to_save())

    @callback
    def async_add_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    # -- gap replay -------------------------------------------------------------

    @callback
    def _on_coordinator_updated(self) -> None:
        """Fire once real prices cover the pending gap, then unsubscribe.

        Registered with ``add_price_listener`` only while ``_pending_gap`` is
        set, and also invoked once directly at the end of ``async_load`` --
        the same callback either way, since a reload can already have prices
        loaded and there is no reason to wait for the next coordinator tick.
        """
        pending = self._pending_gap
        if pending is None or self._prices(pending.window_end) is None:
            return
        if self._gap_replay_unsub is not None:
            self._gap_replay_unsub()
            self._gap_replay_unsub = None
        self.entry.async_create_background_task(
            self.hass, self._async_run_gap_replay(pending), "emhass_gap_replay", eager_start=False
        )

    async def _async_run_gap_replay(self, pending: PendingGapReplay) -> None:
        """Run the replay and fold its outcome back into the tracker.

        Imports :mod:`metering_replay` locally -- that module imports back
        from here at its own top level, so a module-level import here would
        be circular.
        """
        from . import metering_replay

        try:
            replayed = await metering_replay.async_replay_gap(
                self.hass, self.meters, self._prices, self.ledger, pending
            )
        except Exception:  # a failed replay must not corrupt the ledger or wedge the entry
            _LOGGER.exception("Replaying a restart gap failed; the write-off stands")
            replayed = False

        # Either way, this gap is resolved: on success it is now priced, and
        # on failure or an exception the existing write-off already stands
        # and there is nothing left to retry against.
        self._pending_gap = None
        if replayed:
            self._store.async_delay_save(self._data_to_save, _SAVE_DELAY)
            self._publish(force=True)

    # -- accumulation ---------------------------------------------------------

    @callback
    def _async_source_changed(self, _event: Event[EventStateChangedData]) -> None:
        self._settle(dt_util.utcnow())

    @callback
    def _async_midnight(self, now: datetime) -> None:
        self._settle(now)
        self._publish(force=True)

    def _settle(self, now: datetime) -> None:
        """Fold everything that moved since the last settle into the ledger."""
        self._roll_over(now)

        # Read before recording, not after: the cost account opens itself on
        # the first priced interval and values whatever is already in the
        # battery at that moment, so it has to know how full the battery is
        # before that interval is folded in.
        if (stored := self._stored_kwh()) is not None:
            self.ledger.stored_now_kwh = stored
            if self.ledger.stored_start_kwh is None:
                self.ledger.stored_start_kwh = stored

        self.ledger.record(self._read_energy(now), self._prices(now))

        self._store.async_delay_save(self._data_to_save, _SAVE_DELAY)
        self._publish()

    def _read_energy(self, now: datetime) -> IntervalEnergy:
        """Take every meter once, in the shape :mod:`savings` settles."""
        return read_meters(self.meters, now, self.hass.states.get)

    def _roll_over(self, now: datetime) -> None:
        """Start a fresh ledger when the local date has moved on.

        Checked on every settle rather than trusting the midnight timer alone:
        a restart at 00:05 would otherwise merge two days into one, and a
        machine that was asleep through midnight would never fire the timer at
        all.
        """
        today = _local_day(now)
        if self.ledger.day == today:
            return
        stored = self._stored_kwh()
        _LOGGER.debug(
            "Closing savings ledger for %s: %.2f saved on %.2f actual cost",
            self.ledger.day,
            self.ledger.total_savings,
            self.ledger.actual_cost,
        )
        # Yesterday's closing charge is today's opening one -- the battery does
        # not empty at midnight. The cost account crosses the boundary intact
        # too, reconciled first against what the SOC sensor says is actually in
        # there: that hand-over is the whole reason a cycle split across
        # midnight adds up, since today's discharge is debited at what
        # yesterday paid for it.
        self.ledger.reconcile_storage(stored)
        self.ledger = Ledger(
            day=today,
            stored_start_kwh=stored,
            stored_now_kwh=stored,
            battery_book_kwh=self.ledger.battery_book_kwh,
            battery_book_value=self.ledger.battery_book_value,
            battery_book_value_start=self.ledger.battery_book_value,
        )

    def _stored_kwh(self) -> float | None:
        """Energy sitting in the battery right now, from SOC and capacity."""
        if not self._soc_entity or not self._capacity_kwh:
            return None
        soc = _as_float(self.hass.states.get(self._soc_entity))
        if soc is None:
            return None
        return max(min(soc, 100.0), 0.0) / 100.0 * self._capacity_kwh

    # -- publication ----------------------------------------------------------

    def _publish(self, *, force: bool = False) -> None:
        """Tell the sensors, if enough has changed to be worth a state write."""
        now = dt_util.utcnow()
        if not force:
            if self._last_published is not None and now - self._last_published < _PUBLISH_INTERVAL:
                return
            snapshot = self._snapshot()
            if self._last_snapshot and all(
                abs(new - old) < _PUBLISH_THRESHOLD
                for new, old in zip(snapshot, self._last_snapshot, strict=True)
            ):
                # Nothing a user could see has moved. Skipping the write keeps
                # a sleeping house out of the recorder entirely.
                self._last_published = now
                return
            self._last_snapshot = snapshot
        else:
            self._last_snapshot = self._snapshot()
        self._last_published = now
        for listener in list(self._listeners):
            listener()

    def _snapshot(self) -> tuple[float, ...]:
        return (
            self.ledger.actual_cost,
            self.ledger.total_savings,
            self.ledger.solar_savings,
            self.ledger.battery_savings,
        )

    # -- forecast -------------------------------------------------------------

    def forecast(self, window: timedelta = timedelta(hours=24)) -> Forecast | None:
        """The same three worlds, priced over the plan instead of the meters."""
        return self._plan_forecast(dt_util.utcnow(), window)

    # -- persistence ----------------------------------------------------------

    def _named_meters(self) -> list[tuple[str, Meter]]:
        return [
            (name, meter)
            for name, meter in (
                ("grid_import", self.meters.grid_import),
                ("grid_export", self.meters.grid_export),
                ("pv", self.meters.pv),
                ("battery_charge", self.meters.battery_charge),
                ("battery_discharge", self.meters.battery_discharge),
                ("battery_power", self.meters.battery_power),
                ("house_load", self.meters.house_load),
            )
            if meter is not None
        ]

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        return {
            "ledger": self.ledger.as_dict(),
            "meters": {name: meter.as_dict() for name, meter in self._named_meters()},
        }


def _local_day(when: datetime) -> str:
    """The local calendar date ``when`` falls on, as an ISO string.

    Local, not UTC: a user's "today" is their own midnight, and in most of
    Europe a UTC day boundary would cut the cheapest hours of the night in
    half -- which is precisely the part of the day this feature exists to
    measure.
    """
    return dt_util.as_local(when).date().isoformat()


def build_forecast_callable(coordinator: Any) -> Callable[[datetime, timedelta], Forecast | None]:
    """Bind :func:`savings.forecast_costs` to a coordinator's current data.

    A closure rather than a coordinator reference on the tracker, so the
    tracker stays testable without one and the coupling runs one way only.
    """

    def _forecast(now: datetime, window: timedelta) -> Forecast | None:
        data = coordinator.data
        if data is None:
            return None
        battery = coordinator.config.battery
        capacity_kwh = (battery.capacity_wh or 0.0) / 1000.0 if battery.enabled else None
        soc = coordinator.soc_percent
        stored = None if soc is None or not capacity_kwh else soc / 100.0 * capacity_kwh
        return forecast_costs(
            data.plan,
            start=now,
            window=window,
            buy=data.buy_price,
            sell=data.sell_price,
            stored_now_kwh=stored,
            capacity_kwh=capacity_kwh,
        )

    return _forecast


def build_prices_callable(coordinator: Any) -> Callable[[datetime], Prices | None]:
    """Bind the composed buy/sell series to a lookup by instant.

    Returns None whenever either series cannot answer -- before the first
    successful run of a restart, or in a hole in the price data. The ledger
    treats that as "count the energy, do not cost it", which is what makes an
    incomplete day visibly incomplete.
    """

    def _prices(now: datetime) -> Prices | None:
        data = coordinator.data
        if data is None:
            return None
        buy = data.buy_price.value_at(now) if data.buy_price else None
        if buy is None:
            return None
        sell = data.sell_price.value_at(now) if data.sell_price else None
        return Prices(buy=float(buy), sell=float(sell or 0.0))

    return _prices


async def async_energy_dashboard_defaults(hass: HomeAssistant) -> dict[str, str]:
    """Pre-fill the cost tracking form from Home Assistant's Energy dashboard.

    Anyone who can answer these questions has almost certainly answered them
    once already, in Settings > Dashboards > Energy, and asking a second time
    for the same four entity ids is how a setup screen earns a reputation for
    being tedious. These are suggestions only -- the form still shows them and
    the user still confirms.

    Deliberately tolerant. The energy component may not be set up, its
    preferences may be absent, and its stored schema has changed shape more
    than once (the legacy ``flow_from``/``flow_to`` lists against the current
    flat ``stat_energy_from``/``stat_energy_to``); none of that is worth an
    error on a screen whose whole purpose is to save the user some typing, so
    anything unrecognised simply yields no suggestion.
    """
    try:
        from homeassistant.components.energy import data as energy_data

        manager = await energy_data.async_get_manager(hass)
    except Exception:  # see docstring: a suggestion is never worth an error
        _LOGGER.debug("No Energy dashboard preferences to pre-fill from", exc_info=True)
        return {}

    prefs = getattr(manager, "data", None) or {}
    defaults: dict[str, str] = {}
    for source in prefs.get("energy_sources") or []:
        kind = source.get("type")
        if kind == "grid":
            # Legacy shape first: when both are present the flat keys are the
            # migrated truth, so taking them second lets them win.
            for flow in source.get("flow_from") or []:
                defaults.setdefault(CONF_GRID_IMPORT_ENERGY_ENTITY, flow.get("stat_energy_from"))
            for flow in source.get("flow_to") or []:
                defaults.setdefault(CONF_GRID_EXPORT_ENERGY_ENTITY, flow.get("stat_energy_to"))
            if stat := source.get("stat_energy_from"):
                defaults[CONF_GRID_IMPORT_ENERGY_ENTITY] = stat
            if stat := source.get("stat_energy_to"):
                defaults[CONF_GRID_EXPORT_ENERGY_ENTITY] = stat
        elif kind == "solar":
            if stat := source.get("stat_energy_from"):
                defaults[CONF_PV_ENERGY_ENTITY] = stat
        elif kind == "battery":
            # The Energy dashboard's naming is from the *house's* point of
            # view: energy "from" the battery is discharge, energy "to" it is
            # charge. Getting these the wrong way round would invert the whole
            # battery component, so it is worth spelling out.
            if stat := source.get("stat_energy_from"):
                defaults[CONF_BATTERY_DISCHARGE_ENERGY_ENTITY] = stat
            if stat := source.get("stat_energy_to"):
                defaults[CONF_BATTERY_CHARGE_ENERGY_ENTITY] = stat

    # Statistic ids that are not entity ids (external statistics, which the
    # dashboard also accepts) cannot be read from the state machine, so they
    # are no use as a suggestion here.
    return {
        key: value
        for key, value in defaults.items()
        if value and hass.states.get(value) is not None
    }

"""Typed access to the config entry's stored options.

Connection details live in ``entry.data``; everything the user can retune lives
in ``entry.options``. Reading them through this module keeps the raw dictionary
shape in one place instead of spread across every platform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time, timedelta
import math
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_POWER_INVERT,
    CONF_DAYAHEAD_FALLBACK_TIME,
    CONF_HORIZON_HOURS,
    CONF_HOUSE_LOAD_TOTAL_ENTITY,
    CONF_INVERTER,
    CONF_LOAD,
    CONF_MPC_INTERVAL,
    CONF_NETWORK,
    CONF_PRICE,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    CONF_PV,
    CONF_PV_ENTITY,
    CONF_SOC_ENTITY,
    CONF_TEMPERATURE,
    CONF_TIME_STEP,
    CONF_URL,
    DEFAULT_DAYAHEAD_FALLBACK_TIME,
    DEFAULT_HORIZON_HOURS,
    DEFAULT_MPC_INTERVAL,
    DEFAULT_TIME_STEP,
    DOMAIN,
    NET_HOUSE_LOAD_KEY,
    PROFILE_KEY_LOAD_SENSOR,
    STALE_PLAN_FACTOR,
)
from .models import BatteryConfig, GridConfig, HybridInverterConfig
from .stored_time import parse_stored_time
from .tariff import Tariff


@dataclass(slots=True)
class ProfileSelection:
    """A chosen profile plus the answers to its options."""

    key: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ProfileSelection:
        data = data or {}
        return cls(
            key=data.get(CONF_PROFILE),
            options=data.get(CONF_PROFILE_OPTIONS) or {},
        )

    def __bool__(self) -> bool:
        return self.key is not None


def _resolve_net_house_load_entity(
    hass: HomeAssistant, entry: ConfigEntry, load: ProfileSelection
) -> None:
    """Point the load/sensor profile at the sensor the "create one" flow built.

    Its entity id is never stored in options: with ``has_entity_name`` naming,
    it isn't known until Home Assistant actually assigns it (and could change
    if the object id it wants collides with something else), so it is
    resolved fresh from the registry on every load instead of trusted from a
    string written once during setup. Left untouched if the entity has not
    been registered yet -- the moment this integration's own sensor platform
    has run once, which everything that reads ``load.options["entity"]``
    tolerates the same as any other momentarily-missing entity.
    """
    unique_id = f"{entry.entry_id}_{NET_HOUSE_LOAD_KEY}"
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)
    if entity_id:
        load.options = {**load.options, "entity": entity_id}


@dataclass(slots=True)
class EmhassConfig:
    """The full user configuration for one EMHASS Companion entry."""

    url: str
    time_step_minutes: int = DEFAULT_TIME_STEP
    mpc_interval_minutes: int = DEFAULT_MPC_INTERVAL
    horizon_hours: int = DEFAULT_HORIZON_HOURS
    dayahead_fallback_time: time = field(
        default_factory=lambda: time.fromisoformat(DEFAULT_DAYAHEAD_FALLBACK_TIME)
    )
    price: ProfileSelection = field(default_factory=ProfileSelection)
    pv: ProfileSelection = field(default_factory=ProfileSelection)
    load: ProfileSelection = field(default_factory=ProfileSelection)
    inverter: ProfileSelection = field(default_factory=ProfileSelection)
    # Outdoor temperature forecast, consulted only once a thermal load exists.
    temperature: ProfileSelection = field(default_factory=ProfileSelection)
    # The grid operator's own tariff structure -- energy bands and/or a demand
    # charge -- layered on top of whatever `tariff` composes from the supplier's
    # spot price. Optional: an entry with none selected behaves exactly as it
    # did before this existed. See docs/network_tariffs_plan.md.
    network: ProfileSelection = field(default_factory=ProfileSelection)
    tariff: Tariff = field(default_factory=lambda: Tariff.from_dict({}))
    battery: BatteryConfig = field(default_factory=BatteryConfig)
    grid: GridConfig = field(default_factory=GridConfig)
    hybrid_inverter: HybridInverterConfig = field(default_factory=HybridInverterConfig)
    soc_entity: str | None = None
    # Read by nothing here: it is published for the cards, which draw measured
    # battery power against the planned figure. See CONF_BATTERY_POWER_ENTITY.
    battery_power_entity: str | None = None
    battery_power_invert: bool = False
    pv_live_entity: str | None = None
    house_load_total_entity: str | None = None

    @classmethod
    def from_entry(cls, hass: HomeAssistant, entry: ConfigEntry) -> EmhassConfig:
        options = entry.options or {}
        raw_time = options.get(CONF_DAYAHEAD_FALLBACK_TIME, DEFAULT_DAYAHEAD_FALLBACK_TIME)
        # Never raises: a stored time this cannot read costs the user that one
        # setting and a repair issue, not the whole setup. See stored_time.
        fallback_time = parse_stored_time(
            hass,
            raw_time,
            label="Day-ahead fallback time",
            default=time.fromisoformat(DEFAULT_DAYAHEAD_FALLBACK_TIME),
        ) or time.fromisoformat(DEFAULT_DAYAHEAD_FALLBACK_TIME)
        load = ProfileSelection.from_dict(options.get(CONF_LOAD))
        house_load_total_entity = options.get(CONF_HOUSE_LOAD_TOTAL_ENTITY)
        if house_load_total_entity and load.key == PROFILE_KEY_LOAD_SENSOR:
            _resolve_net_house_load_entity(hass, entry, load)
        return cls(
            url=entry.data[CONF_URL],
            time_step_minutes=int(options.get(CONF_TIME_STEP, DEFAULT_TIME_STEP)),
            mpc_interval_minutes=int(options.get(CONF_MPC_INTERVAL, DEFAULT_MPC_INTERVAL)),
            horizon_hours=int(options.get(CONF_HORIZON_HOURS, DEFAULT_HORIZON_HOURS)),
            dayahead_fallback_time=fallback_time,
            price=ProfileSelection.from_dict(options.get(CONF_PRICE)),
            pv=ProfileSelection.from_dict(options.get(CONF_PV)),
            load=load,
            inverter=ProfileSelection.from_dict(options.get(CONF_INVERTER)),
            temperature=ProfileSelection.from_dict(options.get(CONF_TEMPERATURE)),
            network=ProfileSelection.from_dict(options.get(CONF_NETWORK)),
            tariff=Tariff.from_dict(options.get("tariff")),
            battery=BatteryConfig.from_dict(options.get("battery")),
            grid=GridConfig.from_dict(options.get("grid")),
            # Collected from the same form/options blob as battery -- a
            # shared inverter throughput cap is meaningless without a battery
            # to share it with.
            hybrid_inverter=HybridInverterConfig.from_dict(options.get("battery")),
            soc_entity=options.get(CONF_SOC_ENTITY),
            battery_power_entity=options.get(CONF_BATTERY_POWER_ENTITY),
            battery_power_invert=bool(options.get(CONF_BATTERY_POWER_INVERT)),
            house_load_total_entity=house_load_total_entity,
            pv_live_entity=options.get(CONF_PV_ENTITY),
        )

    @property
    def horizon_steps(self) -> int:
        """Horizon expressed in timesteps, which is what EMHASS wants."""
        return max(1, round(self.horizon_hours * 60 / self.time_step_minutes))

    @property
    def dayahead_num_lags(self) -> int:
        """Predict steps mlforecaster must produce in one day-ahead call.

        Mirrors how ``payload.py`` derives ``delta_forecast_daily`` and how
        EMHASS's own ``forecast.py`` then builds ``forecast_dates`` from it:
        the day-ahead action always rounds the horizon *up* to a whole number
        of days, so the actual number of steps EMHASS asks the trained model
        for can exceed ``horizon_steps`` whenever ``horizon_hours`` is not a
        multiple of 24 -- e.g. a 30-hour horizon at 15-minute resolution needs
        96 * 2 = 192 steps, not the 120 ``horizon_steps`` would suggest.
        A non-tuned EMHASS forecaster can only ever produce ``num_lags`` steps
        per predict call (``machine_learning_forecaster.py``'s
        ``predict()``: ``steps = self.lags_opt if self.is_tuned else
        self.num_lags``), so ``num_lags`` must be at least this value.
        """
        hours = self.horizon_steps * self.time_step_minutes / 60
        delta_forecast_days = max(1, math.ceil(hours / 24))
        steps_per_day = round(24 * 60 / self.time_step_minutes)
        return delta_forecast_days * steps_per_day

    @property
    def mpc_interval(self) -> timedelta:
        return timedelta(minutes=self.mpc_interval_minutes)

    @property
    def stale_after(self) -> timedelta:
        """How old a plan may get before it must not be acted on."""
        return self.mpc_interval * STALE_PLAN_FACTOR

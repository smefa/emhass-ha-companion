"""Typed access to the config entry's stored options.

Connection details live in ``entry.data``; everything the user can retune lives
in ``entry.options``. Reading them through this module keeps the raw dictionary
shape in one place instead of spread across every platform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DAYAHEAD_FALLBACK_TIME,
    CONF_HORIZON_HOURS,
    CONF_LOAD,
    CONF_MPC_INTERVAL,
    CONF_PRICE,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    CONF_PV,
    CONF_SOC_ENTITY,
    CONF_TIME_STEP,
    CONF_URL,
    DEFAULT_DAYAHEAD_FALLBACK_TIME,
    DEFAULT_HORIZON_HOURS,
    DEFAULT_MPC_INTERVAL,
    DEFAULT_TIME_STEP,
    STALE_PLAN_FACTOR,
)
from .models import BatteryConfig, GridConfig
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
    tariff: Tariff = field(default_factory=lambda: Tariff.from_dict({}))
    battery: BatteryConfig = field(default_factory=BatteryConfig)
    grid: GridConfig = field(default_factory=GridConfig)
    soc_entity: str | None = None

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> EmhassConfig:
        options = entry.options or {}
        raw_time = options.get(CONF_DAYAHEAD_FALLBACK_TIME, DEFAULT_DAYAHEAD_FALLBACK_TIME)
        return cls(
            url=entry.data[CONF_URL],
            time_step_minutes=int(options.get(CONF_TIME_STEP, DEFAULT_TIME_STEP)),
            mpc_interval_minutes=int(options.get(CONF_MPC_INTERVAL, DEFAULT_MPC_INTERVAL)),
            horizon_hours=int(options.get(CONF_HORIZON_HOURS, DEFAULT_HORIZON_HOURS)),
            dayahead_fallback_time=time.fromisoformat(str(raw_time)),
            price=ProfileSelection.from_dict(options.get(CONF_PRICE)),
            pv=ProfileSelection.from_dict(options.get(CONF_PV)),
            load=ProfileSelection.from_dict(options.get(CONF_LOAD)),
            tariff=Tariff.from_dict(options.get("tariff")),
            battery=BatteryConfig.from_dict(options.get("battery")),
            grid=GridConfig.from_dict(options.get("grid")),
            soc_entity=options.get(CONF_SOC_ENTITY),
        )

    @property
    def horizon_steps(self) -> int:
        """Horizon expressed in timesteps, which is what EMHASS wants."""
        return max(1, round(self.horizon_hours * 60 / self.time_step_minutes))

    @property
    def mpc_interval(self) -> timedelta:
        return timedelta(minutes=self.mpc_interval_minutes)

    @property
    def stale_after(self) -> timedelta:
        """How old a plan may get before it must not be acted on."""
        return self.mpc_interval * STALE_PLAN_FACTOR

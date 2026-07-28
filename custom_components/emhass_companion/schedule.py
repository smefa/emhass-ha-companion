"""Decides when EMHASS runs.

Two cadences: a frequent model-predictive correction, and a day-ahead plan
rebuilt when tomorrow's prices become available.

The day-ahead trigger is deliberately *not* a fixed clock time. Markets publish
at different hours, publication slips, and a user who picks the wrong hour
silently optimises against yesterday's prices for a day before noticing. Instead
the scheduler watches the price series itself and reacts when it grows -- which
works for any price source without the scheduler knowing anything about it. A
daily fallback time remains, for sources whose horizon never extends.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .coordinator import EmhassCoordinator

_LOGGER = logging.getLogger(__name__)

# How much further the price horizon must reach before it counts as "a new day
# of prices arrived" rather than the horizon simply rolling forward.
PRICE_EXTENSION_THRESHOLD = timedelta(hours=6)

# Guards against a source that republishes a slightly longer horizon repeatedly.
MIN_DAYAHEAD_INTERVAL = timedelta(hours=1)


class Scheduler:
    """Owns the timers for one config entry."""

    def __init__(self, hass: HomeAssistant, coordinator: EmhassCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self._unsubscribes: list[Callable[[], None]] = []
        self._price_horizon: datetime | None = None
        self._last_dayahead: datetime | None = None

    def async_start(self) -> None:
        config = self.coordinator.config

        self._unsubscribes.append(
            async_track_time_interval(self.hass, self._async_mpc_tick, config.mpc_interval)
        )

        fallback = config.dayahead_fallback_time
        self._unsubscribes.append(
            async_track_time_change(
                self.hass,
                self._async_dayahead_fallback,
                hour=fallback.hour,
                minute=fallback.minute,
                second=0,
            )
        )
        _LOGGER.debug(
            "Scheduler started: MPC every %s, day-ahead fallback at %s",
            config.mpc_interval,
            fallback,
        )

    def async_stop(self) -> None:
        while self._unsubscribes:
            self._unsubscribes.pop()()

    # -- triggers -------------------------------------------------------------

    async def _async_mpc_tick(self, _now: datetime) -> None:
        await self.coordinator.async_request_refresh()
        self._check_price_horizon()

    async def _async_dayahead_fallback(self, _now: datetime) -> None:
        if self._recently_ran_dayahead():
            _LOGGER.debug(
                "Skipping day-ahead fallback; a day-ahead run already happened "
                "recently after new prices arrived"
            )
            return
        await self._async_run_dayahead("scheduled fallback time")

    def _check_price_horizon(self) -> None:
        """Fire a day-ahead run when the price series gains a new day."""
        data = self.coordinator.data
        if not data or not data.buy_price:
            return

        horizon = data.buy_price.end
        previous, self._price_horizon = self._price_horizon, horizon
        if previous is None:
            # First observation only establishes the baseline; the initial
            # day-ahead run is triggered explicitly at setup.
            return

        if horizon - previous < PRICE_EXTENSION_THRESHOLD:
            return
        if self._recently_ran_dayahead():
            return

        self.hass.async_create_task(
            self._async_run_dayahead(
                f"price horizon extended to {dt_util.as_local(horizon):%Y-%m-%d %H:%M}"
            )
        )

    async def _async_run_dayahead(self, reason: str) -> None:
        _LOGGER.info("Running day-ahead optimisation (%s)", reason)
        self._last_dayahead = dt_util.utcnow()
        try:
            await self.coordinator.async_run_dayahead()
        except Exception:
            _LOGGER.exception("Day-ahead optimisation failed")

    def _recently_ran_dayahead(self) -> bool:
        if self._last_dayahead is None:
            return False
        return dt_util.utcnow() - self._last_dayahead < MIN_DAYAHEAD_INTERVAL

    async def async_run_initial(self) -> None:
        """Build a plan at startup so entities are populated immediately."""
        await self._async_run_dayahead("integration startup")
        if self.coordinator.data and self.coordinator.data.buy_price:
            self._price_horizon = self.coordinator.data.buy_price.end

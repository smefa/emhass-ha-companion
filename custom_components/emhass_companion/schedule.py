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

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .coordinator import EmhassCoordinator

_LOGGER = logging.getLogger(__name__)

# How much further the price horizon must reach before it counts as "a new day
# of prices arrived" rather than the horizon simply rolling forward.
PRICE_EXTENSION_THRESHOLD = timedelta(hours=6)

# Guards against a source that republishes a slightly longer horizon repeatedly.
MIN_DAYAHEAD_INTERVAL = timedelta(hours=1)

# How long the startup run keeps retrying a source that is not ready yet,
# before giving up and leaving the plan to the regular schedule. Waiting for
# Home Assistant itself to finish starting (below) only guards against the
# bulk-startup race; a forecast source that fetches over the network during
# its own setup -- Solcast is the one that has actually done this -- can
# leave its entities missing for a further stretch after Home Assistant
# reports as started, which this retries through instead of surfacing as a
# permanent failure.
INITIAL_RUN_RETRY_DELAYS: tuple[int, ...] = (15, 30, 60, 120, 120)


def _slot_trigger_kwargs(step_minutes: int) -> dict[str, list[int] | int] | None:
    """``async_track_time_change`` kwargs that fire on clock-aligned slots.

    A plain ``async_track_time_interval`` fires every ``step_minutes`` from
    whenever the integration happened to start or last reload -- e.g. ticks at
    :07, :22, :37 rather than :00, :15, :30. Aligning to the clock instead
    means the MPC correction lands on the same boundaries as the optimisation
    timesteps themselves.

    Only interval lengths that tile the clock evenly can do this without
    drifting: sub-hour steps that divide 60 (5, 10, 15, 20, 30), or whole
    hours that divide a day. Anything else (7 minutes, 90 minutes) has no
    fixed set of minute/hour marks to repeat on, so ``None`` tells the caller
    to fall back to the rolling interval.
    """
    if step_minutes <= 0:
        return None
    if step_minutes <= 60 and 60 % step_minutes == 0:
        return {"minute": list(range(0, 60, step_minutes)), "second": 0}
    if step_minutes % 60 == 0 and 1440 % step_minutes == 0:
        return {"hour": list(range(0, 24, step_minutes // 60)), "minute": 0, "second": 0}
    return None


class Scheduler:
    """Owns the timers for one config entry."""

    def __init__(self, hass: HomeAssistant, coordinator: EmhassCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self._unsubscribes: list[Callable[[], None]] = []
        self._price_horizon: datetime | None = None
        self._last_dayahead: datetime | None = None
        # Overridable per instance so a test can shrink this to nothing rather
        # than sleeping through minutes of real backoff.
        self._retry_delays = INITIAL_RUN_RETRY_DELAYS

    def async_start(self) -> None:
        config = self.coordinator.config

        # The horizon check runs on each coordinator *update*, not after
        # requesting one: async_request_refresh is debounced and returns before
        # the refresh happens, so checking there reads the previous run's data
        # and notices new prices a whole cycle late.
        self._unsubscribes.append(self.coordinator.async_add_listener(self._check_price_horizon))

        slot_kwargs = _slot_trigger_kwargs(config.mpc_interval_minutes)
        if slot_kwargs is not None:
            self._unsubscribes.append(
                async_track_time_change(self.hass, self._async_mpc_tick, **slot_kwargs)
            )
        else:
            _LOGGER.warning(
                "mpc_interval_minutes=%d does not evenly divide the clock; MPC "
                "runs will roll on a fixed interval instead of aligning to "
                "clock slots (:00, :15, :30, ...)",
                config.mpc_interval_minutes,
            )
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

    async def _async_dayahead_fallback(self, _now: datetime) -> None:
        if self._recently_ran_dayahead():
            _LOGGER.debug(
                "Skipping day-ahead fallback; a day-ahead run already happened "
                "recently after new prices arrived"
            )
            return
        await self._async_run_dayahead("scheduled fallback time")

    @callback
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

    async def _async_run_dayahead(self, reason: str) -> bool:
        """Run the day-ahead optimisation. Returns whether it succeeded."""
        _LOGGER.info("Running day-ahead optimisation (%s)", reason)
        self._last_dayahead = dt_util.utcnow()
        try:
            await self.coordinator.async_run_dayahead()
        except Exception:
            _LOGGER.exception("Day-ahead optimisation failed")
            return False
        return True

    def _recently_ran_dayahead(self) -> bool:
        if self._last_dayahead is None:
            return False
        return dt_util.utcnow() - self._last_dayahead < MIN_DAYAHEAD_INTERVAL

    async def async_run_initial(self) -> None:
        """Build a plan at startup so entities are populated immediately.

        Waits for Home Assistant's own startup sequence to finish first --
        this runs as a background task fired from ``async_setup_entry``, which
        can itself be invoked while every other integration is still mid-setup,
        a race this component can never win on its own.

        That alone is not always enough. A forecast source that fetches over
        the network during its own setup -- Solcast is the one that has
        actually caused this -- can leave its entities missing for a further
        stretch after Home Assistant reports as started. So a first failure
        here retries with backoff for a while (``INITIAL_RUN_RETRY_DELAYS``)
        rather than being treated as permanent; if every attempt fails, this
        gives up quietly and leaves the plan to the regular schedule -- the
        next MPC tick or price-horizon check, whichever comes first, exactly
        as if this retry loop did not exist.
        """
        await self._async_wait_until_started()

        succeeded = False
        for attempt, delay in enumerate((0, *self._retry_delays)):
            if delay:
                _LOGGER.debug(
                    "Retrying the startup optimisation in %ds (attempt %d)",
                    delay,
                    attempt + 1,
                )
                await asyncio.sleep(delay)
            if await self._async_run_dayahead("integration startup"):
                succeeded = True
                break

        if not succeeded:
            _LOGGER.warning(
                "Startup optimisation did not succeed after %d attempts; "
                "leaving it to the regular schedule",
                len(self._retry_delays) + 1,
            )

        if self.coordinator.data and self.coordinator.data.buy_price:
            self._price_horizon = self.coordinator.data.buy_price.end

    async def _async_wait_until_started(self) -> None:
        """Wait for Home Assistant's own startup sequence to finish.

        A no-op, returning immediately, for the overwhelmingly common case:
        this integration being set up or reloaded well after boot (adding it,
        changing options, a manual reload). The wait only actually happens
        during real Home Assistant startup.
        """
        if self.hass.is_running:
            return
        started = asyncio.Event()

        # Must be marked @callback: an unmarked target is classified as
        # HassJobType.Executor and dispatched via loop.run_in_executor,
        # running on a worker thread. asyncio.Event.set() is not
        # thread-safe -- called there it raises "Non-thread-safe operation
        # invoked on an event loop other than the current one" inside that
        # thread, which nothing awaits, so the exception is silently lost
        # and this wait never wakes up.
        @callback
        def _mark_started(_hass: HomeAssistant) -> None:
            started.set()

        unsub = async_at_started(self.hass, _mark_started)
        try:
            await started.wait()
        finally:
            unsub()

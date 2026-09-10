"""The scheduler: the startup optimisation, and the day-ahead trigger.

The startup half waits out slow-starting dependencies.

A forecast source that fetches over the network during its own setup --
Solcast is the one that has actually done this -- can leave its entities
missing for a while after Home Assistant itself reports as started. Before
this existed, that turned into a single failed day-ahead run at startup and
no plan until the next scheduled MPC tick, sometimes fifteen minutes later.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
import pytest

from custom_components.emhass_companion.schedule import (
    INITIAL_RUN_RETRY_DELAYS,
    PRICE_EXTENSION_THRESHOLD,
    Scheduler,
)


def _scheduler(hass: HomeAssistant, *, run_dayahead=None) -> Scheduler:
    coordinator = Mock()
    coordinator.data = None
    coordinator.async_run_dayahead = run_dayahead or AsyncMock()
    return Scheduler(hass, coordinator)


# --- waiting for Home Assistant's own startup -------------------------------


async def test_wait_is_a_no_op_once_home_assistant_is_running(hass: HomeAssistant) -> None:
    """The overwhelmingly common case: set up or reloaded well after boot."""
    assert hass.is_running
    scheduler = _scheduler(hass)

    await scheduler._async_wait_until_started()


async def test_wait_blocks_until_the_started_event_fires(hass: HomeAssistant) -> None:
    """A background task, exactly as the real caller creates it.

    ``async_block_till_done`` waits for every *foreground* task in flight --
    including, deadlocking the test, one that is itself waiting on something
    the test hasn't done yet. The real code never hits this: the initial run
    is started via ``entry.async_create_background_task``, which
    ``async_block_till_done`` does not wait for by default.
    """
    hass.set_state(CoreState.not_running)
    scheduler = _scheduler(hass)

    task = hass.async_create_background_task(
        scheduler._async_wait_until_started(),
        "test-wait-until-started",
    )
    await asyncio.sleep(0)  # let it run up to its own first await point
    assert not task.done()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await task
    assert task.exception() is None


# --- the retry loop -----------------------------------------------------------


@patch("custom_components.emhass_companion.schedule.asyncio.sleep", new_callable=AsyncMock)
async def test_a_first_try_success_never_sleeps(sleep: AsyncMock, hass: HomeAssistant) -> None:
    run_dayahead = AsyncMock()
    scheduler = _scheduler(hass, run_dayahead=run_dayahead)

    await scheduler.async_run_initial()

    run_dayahead.assert_awaited_once()
    sleep.assert_not_awaited()


@patch("custom_components.emhass_companion.schedule.asyncio.sleep", new_callable=AsyncMock)
async def test_a_failure_is_retried_with_the_first_backoff_delay(
    sleep: AsyncMock, hass: HomeAssistant
) -> None:
    """Solcast's entities are missing on the first attempt, present on the
    second -- the exact shape of the bug this exists to survive."""
    run_dayahead = AsyncMock(side_effect=[RuntimeError("Entity not found"), None])
    scheduler = _scheduler(hass, run_dayahead=run_dayahead)

    await scheduler.async_run_initial()

    assert run_dayahead.await_count == 2
    sleep.assert_awaited_once_with(INITIAL_RUN_RETRY_DELAYS[0])


@patch("custom_components.emhass_companion.schedule.asyncio.sleep", new_callable=AsyncMock)
async def test_giving_up_after_every_retry_is_quiet_not_fatal(
    sleep: AsyncMock, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A source still broken after every retry is left to the regular
    schedule -- the next MPC tick or price-horizon check -- exactly as if
    this retry loop did not exist."""
    run_dayahead = AsyncMock(side_effect=RuntimeError("still broken"))
    scheduler = _scheduler(hass, run_dayahead=run_dayahead)

    await scheduler.async_run_initial()

    assert run_dayahead.await_count == len(INITIAL_RUN_RETRY_DELAYS) + 1
    assert sleep.await_count == len(INITIAL_RUN_RETRY_DELAYS)
    assert "did not succeed" in caplog.text


@patch("custom_components.emhass_companion.schedule.asyncio.sleep", new_callable=AsyncMock)
async def test_retry_delays_are_overridable_per_instance(
    sleep: AsyncMock, hass: HomeAssistant
) -> None:
    """A real test suite must not sleep through minutes of real backoff."""
    run_dayahead = AsyncMock(side_effect=RuntimeError("still broken"))
    scheduler = _scheduler(hass, run_dayahead=run_dayahead)
    scheduler._retry_delays = (1, 2)

    await scheduler.async_run_initial()

    assert run_dayahead.await_count == 3
    sleep.assert_any_await(1)
    sleep.assert_any_await(2)


# --- the two guards compose ----------------------------------------------------


@patch("custom_components.emhass_companion.schedule.asyncio.sleep", new_callable=AsyncMock)
async def test_the_run_waits_for_startup_before_its_first_attempt(
    sleep: AsyncMock, hass: HomeAssistant
) -> None:
    hass.set_state(CoreState.not_running)
    run_dayahead = AsyncMock()
    scheduler = _scheduler(hass, run_dayahead=run_dayahead)

    task = hass.async_create_background_task(scheduler.async_run_initial(), "test-run-initial")
    await asyncio.sleep(0)  # let it run up to the wait, and no further
    run_dayahead.assert_not_awaited()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await task

    run_dayahead.assert_awaited_once()


# --- the day-ahead trigger ---------------------------------------------------
#
# The scheduler deliberately does not fire day-ahead on a clock: markets
# publish at different hours and publication slips. It watches the price
# series instead. What it must watch is how far the *source* reached, not the
# series published alongside the plan -- that one is trimmed to the horizon,
# so for any source publishing further ahead than the horizon it sits at the
# horizon and creeps forward one MPC interval per run.

T0 = datetime(2026, 7, 28, 12, 45, tzinfo=UTC)


def _priced_scheduler(hass: HomeAssistant, *, run_dayahead=None) -> Scheduler:
    scheduler = _scheduler(hass, run_dayahead=run_dayahead)
    scheduler.coordinator.data = Mock(price_source_end=None)
    return scheduler


def _observe(scheduler: Scheduler, end: datetime) -> None:
    scheduler.coordinator.data.price_source_end = end
    scheduler._check_price_horizon()


async def test_the_first_observation_only_establishes_a_baseline(hass: HomeAssistant) -> None:
    """Nothing to compare against yet; the initial run is triggered at setup."""
    run = AsyncMock()
    scheduler = _priced_scheduler(hass, run_dayahead=run)

    _observe(scheduler, T0 + timedelta(hours=11))
    await hass.async_block_till_done()

    run.assert_not_awaited()


async def test_a_new_day_of_prices_fires_a_dayahead_run(hass: HomeAssistant) -> None:
    run = AsyncMock()
    scheduler = _priced_scheduler(hass, run_dayahead=run)

    _observe(scheduler, T0 + timedelta(hours=11))
    _observe(scheduler, T0 + timedelta(hours=35))
    await hass.async_block_till_done()

    run.assert_awaited_once()


async def test_the_horizon_merely_rolling_forward_does_not_fire(hass: HomeAssistant) -> None:
    """One MPC interval of extra reach is the series ageing, not a new day."""
    run = AsyncMock()
    scheduler = _priced_scheduler(hass, run_dayahead=run)

    _observe(scheduler, T0 + timedelta(hours=11))
    _observe(scheduler, T0 + timedelta(hours=11, minutes=30))
    await hass.async_block_till_done()

    run.assert_not_awaited()


async def test_a_source_publishing_past_the_horizon_still_fires(hass: HomeAssistant) -> None:
    """The regression this field exists for.

    `data.buy_price` is cut at the optimisation horizon, so a 48h-publishing
    market reads as a series that never grows -- even at the default 24h
    horizon, and always for a horizon shorter than a day. Watching the
    source's own reach is what keeps the trigger working there.
    """
    run = AsyncMock()
    scheduler = _priced_scheduler(hass, run_dayahead=run)

    # Well past any horizon on both observations, so a trimmed series would
    # have shown no growth at all between them.
    _observe(scheduler, T0 + timedelta(hours=41))
    _observe(scheduler, T0 + timedelta(hours=59))
    await hass.async_block_till_done()

    run.assert_awaited_once()


async def test_a_recent_dayahead_run_suppresses_a_second_one(hass: HomeAssistant) -> None:
    """Guards a source that republishes a slightly longer horizon repeatedly."""
    run = AsyncMock()
    scheduler = _priced_scheduler(hass, run_dayahead=run)

    _observe(scheduler, T0 + timedelta(hours=11))
    _observe(scheduler, T0 + timedelta(hours=35))
    await hass.async_block_till_done()
    _observe(scheduler, T0 + timedelta(hours=59))
    await hass.async_block_till_done()

    run.assert_awaited_once()


async def test_no_price_source_end_yet_is_not_an_observation(hass: HomeAssistant) -> None:
    """A run that carried no price series at all leaves the baseline unset,
    so the next real one does not read as a jump."""
    run = AsyncMock()
    scheduler = _priced_scheduler(hass, run_dayahead=run)

    scheduler._check_price_horizon()
    _observe(scheduler, T0 + timedelta(hours=35))
    await hass.async_block_till_done()

    run.assert_not_awaited()


def test_the_threshold_is_bigger_than_a_days_worth_of_rolling(hass: HomeAssistant) -> None:
    """Sanity bound: the trigger has to tell 'a new day landed' apart from
    'the horizon moved', so it must sit well above one MPC interval and well
    below a full day."""
    assert timedelta(hours=1) < PRICE_EXTENSION_THRESHOLD < timedelta(hours=24)

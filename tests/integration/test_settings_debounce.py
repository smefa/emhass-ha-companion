"""Settings changes share a trailing debounce without delaying explicit runs."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import ACTION_MPC, DOMAIN
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.number import LOAD_NUMBERS, LoadNumber
from custom_components.emhass_companion.switch import LOAD_SWITCHES, LoadSwitch


@pytest.fixture
async def coordinator(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[
            {
                "subentry_type": "deferrable_load",
                "title": name,
                "unique_id": name,
                "data": {"recurrence": "daily", "nominal_power_w": 2000},
            }
            for name in ("Car", "Dishwasher")
        ],
    )
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)
    coordinator.async_run = AsyncMock(return_value=coordinator.data)
    for load in loads.all():
        load.enabled = True
    yield coordinator
    await coordinator.async_shutdown()


async def _advance(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed_exact(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def _number(hass, coordinator, load):
    number = LoadNumber(
        coordinator, load, next(item for item in LOAD_NUMBERS if item.key == "nominal_power")
    )
    number.hass = hass
    return number


async def test_single_change_saves_immediately_and_solves_after_three_seconds(
    hass, freezer, coordinator
):
    load = coordinator.loads.all()[0]
    number = _number(hass, coordinator, load)
    await number.async_set_native_value(3000)
    assert load.nominal_power_w == 3000
    assert number.native_value == 3000
    coordinator.async_run.assert_not_awaited()

    await _advance(hass, freezer, 2)
    coordinator.async_run.assert_not_awaited()
    await _advance(hass, freezer, 1)
    coordinator.async_run.assert_awaited_once_with(ACTION_MPC, notify=False)
    await _advance(hass, freezer, 15)
    coordinator.async_run.assert_awaited_once()


async def test_changes_across_loads_restart_shared_timer_and_use_final_values(
    hass, freezer, coordinator
):
    first, second = coordinator.loads.all()
    first_number = _number(hass, coordinator, first)
    second_number = _number(hass, coordinator, second)
    seen = []

    async def _run(*args, **kwargs):
        seen.append((first.nominal_power_w, second.nominal_power_w))
        return coordinator.data

    coordinator.async_run.side_effect = _run
    await first_number.async_set_native_value(3000)
    await _advance(hass, freezer, 2)
    await second_number.async_set_native_value(4000)
    await _advance(hass, freezer, 2)
    coordinator.async_run.assert_not_awaited()
    await first_number.async_set_native_value(3500)
    await _advance(hass, freezer, 2)
    coordinator.async_run.assert_not_awaited()
    await _advance(hass, freezer, 1)
    assert seen == [(3500, 4000)]

    await second_number.async_set_native_value(4500)
    await _advance(hass, freezer, 3)
    assert seen == [(3500, 4000), (3500, 4500)]


async def test_disabled_edits_do_not_queue_or_extend_a_solve(hass, freezer, coordinator):
    load = coordinator.loads.all()[0]
    number = _number(hass, coordinator, load)
    enabled = LoadSwitch(
        coordinator, load, next(item for item in LOAD_SWITCHES if item.key == "enabled")
    )
    await enabled.async_turn_off()
    await _advance(hass, freezer, 2)
    await number.async_set_native_value(3000)
    await _advance(hass, freezer, 1)
    coordinator.async_run.assert_awaited_once()

    coordinator.async_run.reset_mock()
    await number.async_set_native_value(3500)
    await _advance(hass, freezer, 4)
    coordinator.async_run.assert_not_awaited()
    await enabled.async_turn_on()
    await _advance(hass, freezer, 3)
    coordinator.async_run.assert_awaited_once()
    assert load.nominal_power_w == 3500


@pytest.mark.parametrize("run_method", ["async_run_mpc", "async_request_refresh"])
async def test_explicit_and_scheduled_runs_do_not_wait_for_settings_timer(
    hass, freezer, coordinator, run_method
):
    await coordinator.async_request_settings_refresh()
    await getattr(coordinator, run_method)()
    coordinator.async_run.assert_awaited_once()
    await _advance(hass, freezer, 2)
    coordinator.async_run.assert_awaited_once()
    await _advance(hass, freezer, 1)
    assert coordinator.async_run.await_count == 2


async def test_shutdown_cancels_pending_solve_and_ignores_late_requests(hass, freezer, coordinator):
    await coordinator.async_request_settings_refresh()
    await coordinator.async_shutdown()
    await coordinator.async_request_settings_refresh()
    await _advance(hass, freezer, 4)
    coordinator.async_run.assert_not_awaited()

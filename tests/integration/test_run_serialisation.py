"""Optimisation runs are serialised against each other.

Runs arrive from four independent places -- the MPC tick (through
``async_request_refresh``), the day-ahead trigger, the buttons and the
services -- and none of them coordinates with the others. A run is not a pure
read: it advances each load's runtime accumulator from the plan currently in
hand, re-derives the surplus budgets from that same plan, and anchors the
end-SOC hysteresis on the previous decision. Two runs overlapping therefore
consume the same previous plan twice and both mutate the shared registry
state, and whichever finishes last publishes over the other regardless of
which one started first.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import ACTION_DAYAHEAD, ACTION_MPC, DOMAIN
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.models import LastRun


def _coordinator(hass: HomeAssistant) -> EmhassCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"})
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    return EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)


async def test_two_runs_never_overlap(hass: HomeAssistant) -> None:
    """The whole point: the second caller waits rather than racing the first."""
    coordinator = _coordinator(hass)
    in_flight = 0
    overlapped = False
    gate = asyncio.Event()

    async def _optimize(action: str, _payload: dict) -> tuple[LastRun, None]:
        nonlocal in_flight, overlapped
        in_flight += 1
        overlapped = overlapped or in_flight > 1
        # Suspend inside the run, which is the only state an overlap can be
        # observed in -- a mock that returns without awaiting anything is run
        # to completion eagerly, and no amount of concurrency would collide.
        await gate.wait()
        in_flight -= 1
        return LastRun(status="ok"), None

    coordinator.client.async_optimize = _optimize

    first = hass.async_create_task(coordinator.async_run(ACTION_DAYAHEAD))
    await asyncio.sleep(0.05)
    second = hass.async_create_task(coordinator.async_run(ACTION_MPC))
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(first, second)

    assert not overlapped


async def test_an_earlier_run_cannot_publish_over_a_later_one(
    hass: HomeAssistant,
) -> None:
    """Serialisation has to cover the publish, not only the request.

    EMHASS answering a slow day-ahead call after a fast MPC one is entirely
    normal. Unserialised, both are in flight together and the day-ahead's
    result lands last -- so every entity ends up rendering a plan that is not
    the newest one actually solved. Here the later run is deliberately
    released first; the lock is what keeps it from starting at all until the
    earlier one has finished and published.
    """
    coordinator = _coordinator(hass)
    gates = {ACTION_DAYAHEAD: asyncio.Event(), ACTION_MPC: asyncio.Event()}
    finished: list[str] = []

    async def _optimize(action: str, _payload: dict) -> tuple[LastRun, None]:
        await gates[action].wait()
        finished.append(action)
        return LastRun(status="ok"), None

    coordinator.client.async_optimize = _optimize

    first = hass.async_create_task(coordinator.async_run(ACTION_DAYAHEAD))
    await asyncio.sleep(0.05)
    second = hass.async_create_task(coordinator.async_run(ACTION_MPC))
    await asyncio.sleep(0.05)
    gates[ACTION_MPC].set()
    await asyncio.sleep(0.05)
    gates[ACTION_DAYAHEAD].set()
    await asyncio.gather(first, second)

    assert finished == [ACTION_DAYAHEAD, ACTION_MPC]
    assert coordinator.data.last_action == ACTION_MPC

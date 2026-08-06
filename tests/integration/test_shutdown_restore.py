"""The inverter is handed back *before* Home Assistant finishes stopping.

An inverter whose registers persist until changed has no idea Home Assistant
went away, so a restart during a forced charge leaves the battery charging
until somebody notices the bill. The restore used to be scheduled as a
background task from the stop listener -- but ``hass.async_stop`` cancels
every background task before it even fires that event, and the
``async_block_till_done`` it then waits on only covers foreground tasks. So
the restore was racing the interpreter in precisely the case it exists for.
An awaited coroutine listener is dispatched into ``hass._tasks``, which the
stop sequence does wait for.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import DOMAIN


async def test_the_restore_completes_within_the_stop_sequence(
    hass: HomeAssistant,
) -> None:
    """Waiting for the stop event to be dispatched must be enough.

    ``async_block_till_done`` is what the real stop sequence uses to decide
    integrations have finished stopping, and it does not wait for background
    tasks. A restore that has not completed by the time it returns is a
    restore the shutdown is free to walk away from.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"})
    entry.add_to_hass(hass)

    with patch("custom_components.emhass_companion.EmhassClient") as client_cls:
        client_cls.return_value = AsyncMock(spec=EmhassClient)
        client_cls.return_value.async_get_version = AsyncMock(return_value="0.17.9")
        client_cls.return_value.base_url = "http://localhost:5000"
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    restored: list[str] = []

    async def _restore(reason: str) -> None:
        # A real restore renders its steps and issues service calls, so it
        # suspends several times before it is done. A mock that returns
        # without ever yielding would finish inside the first event-loop pass
        # regardless of how it was scheduled, and prove nothing.
        for _ in range(5):
            await asyncio.sleep(0)
        restored.append(reason)

    entry.runtime_data.executor.async_restore = _restore

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert restored == ["Home Assistant stopping"]

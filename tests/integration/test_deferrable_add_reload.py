"""Adding a deferrable load must make its entities actually appear.

``ConfigSubentryFlowManager.async_finish_flow`` (Home Assistant core) only
calls ``async_add_subentry`` for a newly created subentry -- it schedules no
reload, unlike the reconfigure path's ``async_update_reload_and_abort``. This
integration builds every load's entities exactly once, in
``async_setup_entry``, from ``entry.runtime_data.loads.all()``. Put those two
facts together and a freshly added load's device sat in storage with zero
entities until the user happened to reload or restart on their own, with
nothing in the UI telling them a step was missing.

The fix (a ``SIGNAL_CONFIG_ENTRY_CHANGED`` listener in ``__init__.py``,
scheduling a reload when the subentry set no longer matches what is loaded)
matters as much as this test: an earlier version scheduled the reload directly
from the subentry flow's own step, which races
``ConfigSubentryFlowManager.async_finish_flow`` -- that call happens *after*
the step returns, so the reload could run before the subentry it was meant to
pick up had actually been persisted. It passed every quick check and failed
only under real event-loop contention, which is exactly what running this
suite's other full-setup tests alongside it reproduces.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    CONF_NAME,
    CONF_NOMINAL_POWER,
    CONF_OPERATING_HOURS,
    CONF_SEMI_CONTINUOUS,
    CONF_SINGLE_CONSTANT,
    DOMAIN,
    SUBENTRY_TYPE_DEFERRABLE,
)


def _mock_client() -> AsyncMock:
    client = AsyncMock(spec=EmhassClient)
    client.async_get_version = AsyncMock(return_value="0.17.9")
    # entity.py reads this real (non-async) property into DeviceInfo's
    # configuration_url; left unset it's a MagicMock, which HA's URL
    # validator rejects -- and entity_platform swallows that per-entity,
    # silently leaving the device with zero entities rather than raising.
    client.base_url = "http://localhost:5000"
    return client


async def test_a_newly_added_load_gets_entities_without_a_manual_reload(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"})
    entry.add_to_hass(hass)

    # The reload async_step_user schedules happens as a background task, well
    # after the initial setup call returns -- the mock has to stay in place for
    # the whole test, not just the first setup, or that reload constructs a
    # real EmhassClient and tries to hit the network.
    with patch("custom_components.emhass_companion.EmhassClient", return_value=_mock_client()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, SUBENTRY_TYPE_DEFERRABLE), context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_NAME: "Dishwasher",
                CONF_NOMINAL_POWER: 2000,
                CONF_OPERATING_HOURS: 2,
                CONF_SEMI_CONTINUOUS: True,
                CONF_SINGLE_CONSTANT: False,
            },
        )
        assert result["type"] == "create_entry"

        # The reload is scheduled from a SIGNAL_CONFIG_ENTRY_CHANGED listener,
        # itself a task that -- once it reaches async_setup_entry -- spawns
        # further background tasks (the scheduler's initial run), so wait for
        # background tasks too.
        await hass.async_block_till_done(wait_background_tasks=True)

    subentry_id = next(iter(entry.subentries))
    registry = er.async_get(hass)
    load_entities = [
        entry_reg
        for entry_reg in registry.entities.values()
        if entry_reg.config_subentry_id == subentry_id
    ]

    # One each from number, switch, time, binary_sensor, sensor for this load.
    assert len(load_entities) >= 5
    assert any(e.entity_id.startswith("number.") for e in load_entities)
    assert any(e.entity_id.startswith("switch.") for e in load_entities)

"""Reconfiguring a load must reach the running integration.

The runtime and every per-load entity are built once, at setup, from the
subentry. Before, only a membership change or a Power edit reloaded the entry,
so moving a load to a new switch saved the new switch while the executor kept
driving the old one until the next restart.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import DOMAIN, SUBENTRY_TYPE_DEFERRABLE
from custom_components.emhass_companion.models import LastRun


@pytest.fixture
async def entry(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_DEFERRABLE,
                "title": "Appliance",
                "unique_id": "appliance",
                "data": {
                    "name": "Appliance",
                    "nominal_power_w": 1000,
                    "operating_hours": 2,
                    "control_entity": "switch.old_appliance",
                },
            }
        ],
    )
    entry.add_to_hass(hass)
    client = AsyncMock(spec=EmhassClient)
    client.base_url = "http://localhost:5000"
    client.async_get_version.return_value = "0.18.8"
    client.async_optimize.return_value = (LastRun(status="no-run"), None)
    with patch("custom_components.emhass_companion.EmhassClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        yield entry


async def test_a_new_control_entity_reaches_the_runtime(hass: HomeAssistant, entry) -> None:
    subentry = next(iter(entry.subentries.values()))
    assert entry.runtime_data.loads.all()[0].control_entity == "switch.old_appliance"

    hass.config_entries.async_update_subentry(
        entry, subentry, data={**subentry.data, "control_entity": "switch.new_appliance"}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.runtime_data.loads.all()[0].control_entity == "switch.new_appliance"


async def test_a_renamed_load_reloads(hass: HomeAssistant, entry) -> None:
    subentry = next(iter(entry.subentries.values()))
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        hass.config_entries.async_update_subentry(entry, subentry, title="Dishwasher")
        await hass.async_block_till_done()
    reload.assert_called_once_with(entry.entry_id)


async def test_an_unrelated_entry_update_does_not_reload(hass: HomeAssistant, entry) -> None:
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        hass.config_entries.async_update_entry(entry, data={**entry.data, "x": 1})
        await hass.async_block_till_done()
    reload.assert_not_called()


async def test_a_running_run_now_survives_the_reload(hass: HomeAssistant, entry) -> None:
    """Renaming a load must not switch off one that is being force-run."""
    load = entry.runtime_data.loads.all()[0]
    load.force_run()
    forced_at = load.forced_at
    subentry = next(iter(entry.subentries.values()))

    hass.config_entries.async_update_subentry(entry, subentry, title="Dishwasher")
    await hass.async_block_till_done(wait_background_tasks=True)

    reloaded = entry.runtime_data.loads.all()[0]
    assert reloaded is not load
    assert reloaded.mode == "force_on"
    assert reloaded.forced_at == forced_at


@pytest.mark.parametrize(("commanded", "expected_calls"), [(True, 1), (False, 0)])
async def test_a_replaced_switch_we_turned_on_is_switched_off(
    hass: HomeAssistant, entry, commanded, expected_calls
) -> None:
    """Nothing addresses the old switch after the reload, so release it now.

    One someone else switched on is left alone.
    """
    calls = async_mock_service(hass, "switch", "turn_off")
    hass.states.async_set("switch.old_appliance", "on")
    load = entry.runtime_data.loads.all()[0]
    load.commanded_since = dt_util.utcnow() if commanded else None
    subentry = next(iter(entry.subentries.values()))

    hass.config_entries.async_update_subentry(
        entry, subentry, data={**subentry.data, "control_entity": "switch.new_appliance"}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    old_calls = [
        c
        for c in calls
        if c.data.get("entity_id") in ("switch.old_appliance", ["switch.old_appliance"])
    ]
    assert len(old_calls) == expected_calls

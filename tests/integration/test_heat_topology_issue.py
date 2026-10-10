"""The repair raised when EMHASS's stored heat_topology would wipe the loads.

In EMHASS's default replace mode a topology saved in the add-on overwrites every
deferrable load a run sends. ``async_check_heat_topology`` says so once per
setup, but only when there is a Companion load to lose.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient, EmhassConnectionError
from custom_components.emhass_companion.const import (
    CONF_NOMINAL_POWER,
    CONF_OPERATING_HOURS,
    DOMAIN,
    ISSUE_HEAT_TOPOLOGY_REPLACES,
    SUBENTRY_TYPE_DEFERRABLE,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry

TOPOLOGY = {"sources": [{"id": "hp"}], "storage": [{"id": "dhw"}], "flows": []}


async def _coordinator(
    hass: HomeAssistant,
    stored_config: dict | Exception,
    *,
    with_load: bool = True,
    version: str | None = "0.18.5",
) -> EmhassCoordinator:
    subentries = (
        [
            {
                "subentry_type": SUBENTRY_TYPE_DEFERRABLE,
                "title": "Dishwasher",
                "unique_id": "dishwasher",
                "data": {CONF_NOMINAL_POWER: 2000, CONF_OPERATING_HOURS: 2},
            }
        ]
        if with_load
        else []
    )
    entry = MockConfigEntry(
        domain=DOMAIN, data={"url": "http://localhost:5000"}, subentries_data=subentries
    )
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()

    client = AsyncMock(spec=EmhassClient)
    if isinstance(stored_config, Exception):
        client.async_get_config = AsyncMock(side_effect=stored_config)
    else:
        client.async_get_config = AsyncMock(return_value=stored_config)
    coordinator = EmhassCoordinator(hass, entry, client, loads)
    coordinator.backend_version = version
    return coordinator


def _issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_HEAT_TOPOLOGY_REPLACES)


async def test_replace_mode_topology_with_loads_raises_a_repair(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass, {"heat_topology": TOPOLOGY})

    await coordinator.async_check_heat_topology()

    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == ISSUE_HEAT_TOPOLOGY_REPLACES
    assert issue.translation_placeholders == {"required": "0.18.5"}


async def test_an_old_backend_is_told_to_upgrade(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass, {"heat_topology": TOPOLOGY}, version="0.18.4")

    await coordinator.async_check_heat_topology()

    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == f"{ISSUE_HEAT_TOPOLOGY_REPLACES}_upgrade"


async def test_an_unknown_backend_version_gets_the_extend_advice(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass, {"heat_topology": TOPOLOGY}, version=None)

    await coordinator.async_check_heat_topology()

    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == ISSUE_HEAT_TOPOLOGY_REPLACES


async def test_extend_mode_raises_nothing(hass: HomeAssistant) -> None:
    topology = {**TOPOLOGY, "extend_deferrable_loads": True}
    coordinator = await _coordinator(hass, {"heat_topology": topology})

    await coordinator.async_check_heat_topology()

    assert _issue(hass) is None


async def test_the_extend_flag_on_a_backend_that_ignores_it_still_warns(
    hass: HomeAssistant,
) -> None:
    topology = {**TOPOLOGY, "extend_deferrable_loads": True}
    coordinator = await _coordinator(hass, {"heat_topology": topology}, version="0.18.4")

    await coordinator.async_check_heat_topology()

    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == f"{ISSUE_HEAT_TOPOLOGY_REPLACES}_upgrade"


async def test_no_topology_raises_nothing(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass, {"heat_topology": None})

    await coordinator.async_check_heat_topology()

    assert _issue(hass) is None


async def test_without_companion_loads_there_is_nothing_to_wipe(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass, {"heat_topology": TOPOLOGY}, with_load=False)

    await coordinator.async_check_heat_topology()

    assert _issue(hass) is None


async def test_the_repair_clears_once_the_topology_is_fixed(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass, {"heat_topology": TOPOLOGY})
    await coordinator.async_check_heat_topology()
    assert _issue(hass) is not None

    coordinator.client.async_get_config = AsyncMock(
        return_value={"heat_topology": {**TOPOLOGY, "extend_deferrable_loads": True}}
    )
    await coordinator.async_check_heat_topology()

    assert _issue(hass) is None


async def test_unreachable_emhass_is_silent_and_keeps_an_existing_repair(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(hass, {"heat_topology": TOPOLOGY})
    await coordinator.async_check_heat_topology()
    assert _issue(hass) is not None

    coordinator.client.async_get_config = AsyncMock(side_effect=EmhassConnectionError("down"))
    await coordinator.async_check_heat_topology()  # must not raise

    assert _issue(hass) is not None

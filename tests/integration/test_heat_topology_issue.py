"""Saved topology is overridden per request, so its old repair is obsolete."""

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import DOMAIN, ISSUE_HEAT_TOPOLOGY_REPLACES
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry


@pytest.mark.parametrize("version", [None, "0.17.9", "0.18.4", "0.18.8"])
async def test_runtime_topology_override_clears_stale_repair_without_backend_read(
    hass: HomeAssistant,
    version: str | None,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"})
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    client = AsyncMock(spec=EmhassClient)
    coordinator = EmhassCoordinator(hass, entry, client, loads)
    coordinator.backend_version = version
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_HEAT_TOPOLOGY_REPLACES,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_HEAT_TOPOLOGY_REPLACES,
    )

    await coordinator.async_check_heat_topology()

    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_HEAT_TOPOLOGY_REPLACES) is None
    client.async_get_config.assert_not_awaited()

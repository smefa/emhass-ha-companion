"""Fresh EMHASS instances must not require an optimisation before setup."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import DOMAIN


async def test_entry_setup_accepts_degraded_health_before_first_run(hass: HomeAssistant) -> None:
    url = "http://emhass:5000"
    response = MagicMock(status=503)
    response.text = AsyncMock(
        return_value=json.dumps(
            {
                "status": "degraded",
                "last_run_ts": None,
                "last_run_status": None,
                "versions": {"emhass": "0.18.8"},
            }
        )
    )
    session = MagicMock()
    session.request.return_value.__aenter__.return_value = response
    health_client = EmhassClient(session, url)
    client = AsyncMock(spec=EmhassClient)
    client.base_url = url
    client.async_get_version = health_client.async_get_version
    entry = MockConfigEntry(domain=DOMAIN, data={"url": url})
    entry.add_to_hass(hass)

    with patch("custom_components.emhass_companion.EmhassClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.runtime_data.coordinator.backend_version == "0.18.8"
    session.request.assert_called_once()
    assert await hass.config_entries.async_unload(entry.entry_id)

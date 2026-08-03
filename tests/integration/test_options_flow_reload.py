"""The options flow must not conflict with OptionsFlowWithReload.

`EmhassCompanionOptionsFlow` inherits `OptionsFlowWithReload`, which Home
Assistant core documents as: "It's not allowed to use this class if the
integration uses config entry update listeners." `async_setup_entry` used to
register one anyway (`entry.add_update_listener(...)`), which a newer Home
Assistant release started enforcing as a hard `ValueError` raised when the
options flow finishes -- turning every options save (not just tariff, any of
them) into a raw 500 "Unknown error occurred when trying to save" the moment
two independent reload mechanisms existed for the same entry. The listener
was pure redundancy: OptionsFlowWithReload already reloads the entry itself.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import CONF_LOAD, DOMAIN


async def _setup_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"})
    entry.add_to_hass(hass)

    with patch("custom_components.emhass_companion.EmhassClient") as client_cls:
        client_cls.return_value = AsyncMock(spec=EmhassClient)
        client_cls.return_value.async_get_version = AsyncMock(return_value="0.17.9")
        # entity.py reads this real (non-async) property into DeviceInfo's
        # configuration_url; left unset it's a MagicMock, which HA's URL
        # validator rejects -- entity_platform swallows that per-entity, so
        # setup "succeeds" while silently adding zero entities.
        client_cls.return_value.base_url = "http://localhost:5000"
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return entry


async def test_saving_tariff_options_does_not_raise_the_reload_conflict(
    hass: HomeAssistant,
) -> None:
    """Reproduces the reported crash end to end, through the real flow manager."""
    entry = await _setup_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "tariff"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "buy_mode": "linear",
            "buy_multiplier": 1.25,
            "buy_adder": -0.2,
            "sell_mode": "linear",
            "sell_multiplier": 1.25,
            "sell_adder": 0.0,
        },
    )

    assert result["type"] == "create_entry"


async def test_load_forecast_method_is_reachable_and_saves(hass: HomeAssistant) -> None:
    """The load source (e.g. switching to mlforecaster) must be reconfigurable
    without re-adding the integration -- it used to only be set once, at
    initial setup, with no menu entry to revisit it.
    """
    entry = await _setup_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "load"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"profile": "load/sensor"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"entity": "sensor.house_load", "method": "mlforecaster"},
    )

    assert result["type"] == "create_entry"
    assert entry.options[CONF_LOAD] == {
        "profile": "load/sensor",
        "profile_options": {"entity": "sensor.house_load", "method": "mlforecaster"},
    }

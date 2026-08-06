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
from custom_components.emhass_companion.const import CONF_LOAD, CONF_PV, DOMAIN


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


async def test_load_step_preselects_create_a_sensor_when_that_is_how_it_was_set_up(
    hass: HomeAssistant,
) -> None:
    """ "House load sensor (without deferrables)" and "Create a house load sensor"
    both persist as profile "load/sensor" -- the latter just skips storing
    "entity" (it's resolved dynamically, see async_step_load_create). Without
    accounting for that, this step always preselected the plain profile, even
    for an entry actually built through the create flow.
    """
    entry = await _setup_entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_LOAD: {"profile": "load/sensor", "profile_options": {"method": "typical"}},
            "house_load_total_entity": "sensor.load_power",
        },
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "load"}
    )

    (marker,) = (key for key in result["data_schema"].schema if key == "profile")
    assert marker.description["suggested_value"] == "__create__"


async def test_create_a_house_load_sensor_lets_you_pick_the_forecast_method(
    hass: HomeAssistant,
) -> None:
    """ "Create a house load sensor" used to jump straight from picking the
    total-power sensor to saving, silently defaulting "method" to "typical"
    with no form shown -- the same forecast-method choice the plain "House
    load sensor" profile gets was unreachable through this path.
    """
    entry = await _setup_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "load"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"profile": "__create__"}
    )
    assert result["step_id"] == "load_create"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"house_load_total_entity": "sensor.total_house_power"}
    )
    assert result["step_id"] == "load_create_options"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"method": "mlforecaster"}
    )

    assert result["type"] == "create_entry"
    assert entry.options[CONF_LOAD] == {
        "profile": "load/sensor",
        "profile_options": {"method": "mlforecaster"},
    }
    assert entry.options["house_load_total_entity"] == "sensor.total_house_power"


async def test_the_pv_profile_is_reachable_and_saves(hass: HomeAssistant) -> None:
    """Same set-once bug as the load source, in the solar forecast.

    Nothing but the initial wizard could touch it, so an install could never
    pick up an entity a profile template gained later -- Solcast's day-3
    sensor, which End SOC's Optimized mode needs to see the next solar day
    past the horizon.
    """
    entry = await _setup_entry(hass)
    # The Solcast profile only appears once its integration is detected.
    hass.config.components.add("solcast_solar")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "pv" in result["menu_options"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "pv"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"profile": "pv/solcast"}
    )
    assert result["step_id"] == "pv_options"

    entities = [
        "sensor.solcast_pv_forecast_forecast_today",
        "sensor.solcast_pv_forecast_forecast_tomorrow",
        "sensor.solcast_pv_forecast_forecast_day_3",
    ]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entities": entities, "estimate": "pv_estimate"}
    )

    assert result["type"] == "create_entry"
    assert entry.options[CONF_PV] == {
        "profile": "pv/solcast",
        "profile_options": {"entities": entities, "estimate": "pv_estimate"},
    }


async def test_the_pv_step_suggests_what_is_stored_not_the_template_default(
    hass: HomeAssistant,
) -> None:
    """A changed template default is an offer, not a silent migration."""
    entry = await _setup_entry(hass)
    hass.config.components.add("solcast_solar")
    stored = [
        "sensor.solcast_pv_forecast_forecast_today",
        "sensor.solcast_pv_forecast_forecast_tomorrow",
    ]
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PV: {
                "profile": "pv/solcast",
                "profile_options": {"entities": stored, "estimate": "pv_estimate"},
            },
        },
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "pv"}
    )
    (marker,) = (key for key in result["data_schema"].schema if key == "profile")
    assert marker.description["suggested_value"] == "pv/solcast"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"profile": "pv/solcast"}
    )
    (marker,) = (key for key in result["data_schema"].schema if key == "entities")
    assert marker.description["suggested_value"] == stored


async def test_reconfigure_reloads_so_the_new_address_takes_effect(
    hass: HomeAssistant,
) -> None:
    """Updating the URL without a reload leaves the old client in charge.

    `EmhassClient` is built once, in `async_setup_entry`, from the address
    stored at that moment. A reconfigure that only wrote the entry back sent
    every subsequent request to the previous host until Home Assistant
    restarted -- which is exactly the state someone opens this step to escape.
    """
    entry = await _setup_entry(hass)
    old_client = entry.runtime_data.coordinator.client

    result = await entry.start_reconfigure_flow(hass)
    with (
        patch("custom_components.emhass_companion.EmhassClient") as client_cls,
        patch(
            "custom_components.emhass_companion.config_flow._async_validate_connection",
            AsyncMock(return_value=("0.17.9", None)),
        ),
    ):
        client_cls.return_value = AsyncMock(spec=EmhassClient)
        client_cls.return_value.async_get_version = AsyncMock(return_value="0.17.9")
        client_cls.return_value.base_url = "http://elsewhere:5000"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"url": "http://elsewhere:5000"}
        )
        await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["url"] == "http://elsewhere:5000"
    # The reload is the point: a fresh client, built from the new address.
    assert entry.runtime_data.coordinator.client is not old_client

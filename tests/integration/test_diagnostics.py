"""The support bundle: every new section, redaction, and the log ring buffer.

Exercises the real ``async_setup_entry``/``async_unload_entry`` path -- not a
hand-built runtime-data stand-in like some other integration tests use --
because the log ring buffer this also checks is installed and torn down by
those two functions themselves, not by anything diagnostics.py owns.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    CONF_NOMINAL_POWER,
    CONF_SOC_ENTITY,
    DOMAIN,
    SUBENTRY_TYPE_DEFERRABLE,
    SUBENTRY_TYPE_THERMAL,
)
from custom_components.emhass_companion.diagnostics import async_get_config_entry_diagnostics
from custom_components.emhass_companion.log_ring import MAX_MESSAGE_CHARS

MISSING_ENTITY = "sensor.definitely_missing"


async def _setup_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        # A soc_entity that is never registered -- the "wrong entity id"
        # failure the entities section exists to catch.
        options={CONF_SOC_ENTITY: MISSING_ENTITY},
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_DEFERRABLE,
                "title": "Dishwasher",
                "unique_id": "dishwasher",
                "data": {CONF_NOMINAL_POWER: 2000},
            },
            {
                "subentry_type": SUBENTRY_TYPE_THERMAL,
                "title": "Heat pump",
                "unique_id": "heat-pump",
                "data": {CONF_NOMINAL_POWER: 3000},
            },
        ],
    )
    entry.add_to_hass(hass)

    with patch("custom_components.emhass_companion.EmhassClient") as client_cls:
        client_cls.return_value = AsyncMock(spec=EmhassClient)
        client_cls.return_value.async_get_version = AsyncMock(return_value="0.17.9")
        # What diagnostics' own backend probe reads at dump time -- includes a
        # coordinate, which redaction must strip before it reaches a public
        # issue tracker.
        client_cls.return_value.async_get_config = AsyncMock(
            return_value={"latitude": 59.3, "longitude": 18.0, "some_setting": "value"}
        )
        # entity.py reads this real (non-async) property into DeviceInfo's
        # configuration_url; left unset it's a MagicMock, which HA's URL
        # validator rejects.
        client_cls.return_value.base_url = "http://localhost:5000"
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return entry


async def test_every_new_section_is_present(hass: HomeAssistant) -> None:
    entry = await _setup_entry(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    for key in (
        "environment",
        "backend",
        "subentries",
        "entities",
        "custom_profiles",
        "logs",
        "triage",
    ):
        assert key in diagnostics


async def test_last_payload_is_still_top_level_where_the_old_script_expects_it(
    hass: HomeAssistant,
) -> None:
    """scripts/check_infeasibility.py and its tests read this key unchanged."""
    entry = await _setup_entry(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert "last_payload" in diagnostics
    assert "warnings" in diagnostics
    assert "deferrable_order" in diagnostics


async def test_subentries_include_both_the_deferrable_and_thermal_load(hass: HomeAssistant) -> None:
    entry = await _setup_entry(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    types = {item["subentry_type"] for item in diagnostics["subentries"]}
    assert types == {SUBENTRY_TYPE_DEFERRABLE, SUBENTRY_TYPE_THERMAL}
    # Sorted by type then title, so two bundles diff cleanly.
    assert diagnostics["subentries"] == sorted(
        diagnostics["subentries"], key=lambda item: (item["subentry_type"], item["title"])
    )


async def test_the_missing_entity_is_reported_as_not_existing(hass: HomeAssistant) -> None:
    entry = await _setup_entry(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    records = {item["entity_id"]: item for item in diagnostics["entities"] if "entity_id" in item}
    assert MISSING_ENTITY in records
    assert records[MISSING_ENTITY]["exists"] is False
    assert any("soc_entity" in path for path in records[MISSING_ENTITY]["referenced_by"])


async def test_the_missing_entity_is_flagged_in_triage(hass: HomeAssistant) -> None:
    entry = await _setup_entry(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert any(MISSING_ENTITY in finding["message"] for finding in diagnostics["triage"])


async def test_an_input_that_reached_emhass_by_no_route_is_flagged_in_triage(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(hass)
    coordinator = entry.runtime_data.coordinator
    coordinator.data.payload = {}

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert any(
        "EMHASS was given no load forecast" in finding["message"] and finding["severity"] == "error"
        for finding in diagnostics["triage"]
    )


async def test_an_empty_series_emhass_computes_itself_is_not_flagged_in_triage(
    hass: HomeAssistant,
) -> None:
    """The load/sensor profile's ordinary shape.

    It contributes a sensor and a method name rather than points, and EMHASS
    builds the forecast server-side -- so an empty local series is correct
    here. Reporting it would be the same cried-wolf mistake as comparing
    forecast lengths against each other.
    """
    entry = await _setup_entry(hass)
    coordinator = entry.runtime_data.coordinator
    coordinator.data.payload = {
        "load_forecast_method": "naive",
        "sensor_power_load_no_var_loads": "sensor.net_house_load",
    }

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert not any(
        "load forecast" in finding["message"] and finding["severity"] == "error"
        for finding in diagnostics["triage"]
    )


async def test_a_load_forecast_method_that_was_not_honoured_is_info_not_an_error(
    hass: HomeAssistant,
) -> None:
    """The wait for history is by design and self-resolving, so this must not
    read as something to go and fix.
    """
    entry = await _setup_entry(hass)
    coordinator = entry.runtime_data.coordinator
    coordinator.config.load.options = {"method": "mlforecaster"}
    coordinator.data.payload = {"load_forecast_method": "naive"}

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    downgrade = [
        finding
        for finding in diagnostics["triage"]
        if "mlforecaster" in finding["message"] and "naive" in finding["message"]
    ]
    assert downgrade
    assert all(finding["severity"] == "info" for finding in downgrade)


async def test_latitude_in_the_emhass_config_comes_back_redacted(hass: HomeAssistant) -> None:
    entry = await _setup_entry(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    backend_config = diagnostics["backend"]["config"]
    assert backend_config["latitude"] == REDACTED
    assert backend_config["longitude"] == REDACTED
    # Deliberately not redacted -- an unrecognised key is not a coordinate or
    # a credential, so it stays visible for diffing against what was sent.
    assert backend_config["some_setting"] == "value"


async def test_backend_url_is_scrubbed_of_credentials_but_keeps_host_and_port(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["backend"]["url"] == "http://localhost:5000"


async def test_environment_reports_versions_and_no_coordinates(hass: HomeAssistant) -> None:
    entry = await _setup_entry(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    environment = diagnostics["environment"]
    assert environment["integration_version"]
    assert environment["home_assistant_version"]
    assert "latitude" not in environment
    assert "longitude" not in environment


async def test_logs_section_carries_records_from_this_integrations_logger(
    hass: HomeAssistant,
) -> None:
    """The ring buffer is installed by async_setup_entry and reachable from
    runtime_data, as the plan asks; a submodule logger (a child of the
    package logger) must reach it too, since that is where the interesting
    reasoning actually gets logged.
    """
    entry = await _setup_entry(hass)
    logging.getLogger("custom_components.emhass_companion.coordinator").warning(
        "diagnostics test marker"
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    messages = [record["message"] for record in diagnostics["logs"]]
    assert "diagnostics test marker" in messages


async def test_a_runaway_log_message_is_truncated(hass: HomeAssistant) -> None:
    """Bounding the record count is not enough when one record is the problem.

    Add-on discovery logs the Supervisor's whole store entry for EMHASS at
    debug level, and that carries the add-on's rendered long_description --
    several kilobytes of README in a single message, which in a real bundle
    outweighed every other record put together.
    """
    entry = await _setup_entry(hass)
    logging.getLogger("custom_components.emhass_companion.coordinator").warning("x" * 50_000)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    runaway = next(record for record in diagnostics["logs"] if record["message"].startswith("xxx"))
    assert len(runaway["message"]) < MAX_MESSAGE_CHARS + 100
    assert "48000 more characters" in runaway["message"]


async def test_the_log_handler_is_removed_when_the_entry_is_unloaded(hass: HomeAssistant) -> None:
    entry = await _setup_entry(hass)
    logger = logging.getLogger("custom_components.emhass_companion")
    handler = entry.runtime_data.log_handler
    assert handler in logger.handlers

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert handler not in logger.handlers


async def test_a_credential_in_a_custom_profile_is_not_dumped_verbatim(
    hass: HomeAssistant, tmp_path
) -> None:
    """Profiles are the likeliest place in this integration for a secret.

    Their `emhass:` block is an unconstrained mapping, so a profile using
    EMHASS's own Solcast method has to put the key in the file -- and this
    section otherwise puts every byte of that file into a download users are
    told to attach to a public issue.
    """
    profiles = tmp_path / "emhass_companion" / "profiles" / "pv"
    profiles.mkdir(parents=True)
    (profiles / "mine.yaml").write_text(
        "kind: pv\n"
        "name: Mine\n"
        "# the comment survives\n"
        "emhass:\n"
        "  solcast_api_key: hunter2\n"
        "  solcast_rooftop_id: not-a-secret\n"
        "  weather_forecast_method: solcast\n"
    )
    hass.config.config_dir = str(tmp_path)

    entry = await _setup_entry(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    profile = next(item for item in diagnostics["custom_profiles"] if "content" in item)
    assert "hunter2" not in profile["content"]
    assert REDACTED in profile["content"]
    # Everything that is not a credential still reads exactly as on disk --
    # the point of shipping the file at all is that somebody can read it.
    assert "# the comment survives" in profile["content"]
    assert "solcast_rooftop_id: not-a-secret" in profile["content"]
    assert "weather_forecast_method: solcast" in profile["content"]


async def test_a_credential_a_profile_contributed_to_the_payload_is_redacted(
    hass: HomeAssistant,
) -> None:
    """build_payload merges every selected profile's `emhass:` block in whole.

    So the payload can carry the same key that is redacted out of
    `backend.config` a few lines below it in the same bundle.
    """
    entry = await _setup_entry(hass)
    payload = entry.runtime_data.coordinator.data.payload
    payload["solcast_api_key"] = "hunter2"
    payload["optimization_time_step"] = 15

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["last_payload"]["solcast_api_key"] == REDACTED
    # Redaction must not cost the payload's actual troubleshooting value.
    assert diagnostics["last_payload"]["optimization_time_step"] == 15

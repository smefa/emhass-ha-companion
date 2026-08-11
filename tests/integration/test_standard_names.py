"""EMHASS-standard entity ids for the plan sensors.

The option exists for someone replacing a bare EMHASS install with the
Companion: their dashboards, templates and automations are written against
``sensor.p_pv_forecast`` and friends, and nothing else in Home Assistant will
tell them those ids stopped being written.

Both halves are under test: the entity ids themselves, and the forecast series
that has to travel in EMHASS's own shape for a consumer to read it. The id a
sensor had before the option went on is recorded, because turning it back off
has to restore exactly that rather than whatever Home Assistant would generate
today.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.config_flow import (
    EmhassCompanionConfigFlow,
    EmhassCompanionOptionsFlow,
)
from custom_components.emhass_companion.const import (
    CONF_EMHASS_STANDARD_NAMES,
    CONF_ENTITY_IDS_BEFORE_STANDARD,
    DOMAIN,
    EMHASS_STANDARD_OBJECT_IDS,
    ISSUE_STANDARD_NAMES_TAKEN,
)
from custom_components.emhass_companion.coordinator import EmhassData
from custom_components.emhass_companion.models import Plan, Point, Series
from custom_components.emhass_companion.naming import async_taken_standard_ids


def _mock_client():
    """Keep EMHASS out of it -- these tests only care about entity ids.

    Needed around every reload as well as the first setup: without it the real
    client tries to reach the address and setup raises ConfigEntryNotReady,
    which would leave the rename looking like it simply never ran.
    """
    patcher = patch("custom_components.emhass_companion.EmhassClient")
    client_cls = patcher.start()
    client_cls.return_value = AsyncMock(spec=EmhassClient)
    client_cls.return_value.async_get_version = AsyncMock(return_value="0.17.9")
    client_cls.return_value.base_url = "http://localhost:5000"
    return patcher


async def _setup(hass: HomeAssistant, *, standard_names: bool | None = None) -> MockConfigEntry:
    # With a battery, so that the two battery sensors exist at all -- they are
    # only added when there is one (sensor.py), and half the mapping is theirs.
    options: dict[str, Any] = {
        "battery": {
            "use_battery": True,
            "capacity_wh": 10000,
            "charge_power_max_w": 5000,
            "discharge_power_max_w": 5000,
        }
    }
    if standard_names is not None:
        options[CONF_EMHASS_STANDARD_NAMES] = standard_names
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"}, options=options)
    entry.add_to_hass(hass)

    patcher = _mock_client()
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        patcher.stop()
    return entry


async def _reload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    patcher = _mock_client()
    try:
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        patcher.stop()


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str | None:
    """Where a plan sensor currently lives, found by its stable unique id."""
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")


async def _set_option(hass: HomeAssistant, entry: MockConfigEntry, value: bool) -> None:
    """Flip the option the way the options flow does: update, then reload."""
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_EMHASS_STANDARD_NAMES: value}
    )
    await _reload(hass, entry)


# --- the option itself ---------------------------------------------------------


async def test_companion_names_are_the_default(hass: HomeAssistant) -> None:
    """Nobody who has not asked for this should ever see an EMHASS id."""
    entry = await _setup(hass)

    assert (
        _entity_id(hass, entry, "pv_forecast") == "sensor.emhass_companion_planned_solar_production"
    )
    assert hass.states.get("sensor.p_pv_forecast") is None


async def test_every_mapped_sensor_takes_its_emhass_id(hass: HomeAssistant) -> None:
    entry = await _setup(hass, standard_names=True)

    for key, object_id in EMHASS_STANDARD_OBJECT_IDS.items():
        assert _entity_id(hass, entry, key) == f"sensor.{object_id}", key


async def test_turning_it_on_renames_existing_sensors(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert _entity_id(hass, entry, "battery_soc") == "sensor.emhass_companion_planned_battery_level"

    await _set_option(hass, entry, True)

    assert _entity_id(hass, entry, "battery_soc") == "sensor.soc_batt_forecast"


async def test_turning_it_off_restores_the_original_ids(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    before = {key: _entity_id(hass, entry, key) for key in EMHASS_STANDARD_OBJECT_IDS}

    await _set_option(hass, entry, True)
    await _set_option(hass, entry, False)

    assert {key: _entity_id(hass, entry, key) for key in EMHASS_STANDARD_OBJECT_IDS} == before


async def test_the_original_ids_are_recorded_only_on_the_way_in(hass: HomeAssistant) -> None:
    """A second setup with the option still on must not overwrite the record
    with the standard id the sensor was already renamed to -- that would make
    turning it off a no-op."""
    entry = await _setup(hass)
    original = _entity_id(hass, entry, "pv_forecast")

    await _set_option(hass, entry, True)
    await _reload(hass, entry)

    assert entry.options[CONF_ENTITY_IDS_BEFORE_STANDARD]["pv_forecast"] == original


async def test_the_record_is_cleared_when_the_option_goes_off(hass: HomeAssistant) -> None:
    entry = await _setup(hass)

    await _set_option(hass, entry, True)
    await _set_option(hass, entry, False)

    assert entry.options[CONF_ENTITY_IDS_BEFORE_STANDARD] == {}


# --- collisions ----------------------------------------------------------------


async def test_an_id_taken_by_emhass_itself_is_left_alone(hass: HomeAssistant) -> None:
    """EMHASS's publish-data posts straight to the state machine over REST, so
    its sensors have a state and no registry entry at all. Renaming onto one
    would put two writers on a single id, each overwriting the other."""
    hass.states.async_set("sensor.p_pv_forecast", "1234")
    entry = await _setup(hass, standard_names=True)

    assert _entity_id(hass, entry, "pv_forecast") != "sensor.p_pv_forecast"
    # The ones that are free still move -- a conflict on one must not abandon
    # the whole option.
    assert _entity_id(hass, entry, "battery_soc") == "sensor.soc_batt_forecast"


async def test_a_collision_raises_a_repair_naming_the_id(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.p_pv_forecast", "1234")
    await _setup(hass, standard_names=True)

    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_STANDARD_NAMES_TAKEN)
    assert issue is not None
    assert "sensor.p_pv_forecast" in issue.translation_placeholders["details"]


async def test_no_repair_when_every_id_is_free(hass: HomeAssistant) -> None:
    await _setup(hass, standard_names=True)

    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_STANDARD_NAMES_TAKEN) is None


async def test_our_own_sensors_do_not_count_as_conflicts(hass: HomeAssistant) -> None:
    """Once renamed, this entry holds the standard ids itself. Reporting those
    as taken would raise a repair on every subsequent reload."""
    entry = await _setup(hass, standard_names=True)

    assert async_taken_standard_ids(hass, entry) == []


async def test_the_config_flow_sees_ids_taken_by_someone_else(hass: HomeAssistant) -> None:
    """With no entry to exclude -- the install-flow case -- an existing EMHASS
    sensor is exactly the signal that makes the step worth showing."""
    hass.states.async_set("sensor.p_batt_forecast", "-3000")

    assert async_taken_standard_ids(hass) == ["sensor.p_batt_forecast"]


# --- the forecast series in EMHASS's shape -------------------------------------


def _plan(hass: HomeAssistant) -> Plan:
    """Two points, one behind and one ahead of now."""
    now = dt_util.utcnow()
    return Plan.from_response(
        {
            "status": "ok",
            "generated_at": now.isoformat(),
            "emhass_schema_version": "1.0",
            "plan": [
                {"timestamp": (now - timedelta(minutes=30)).isoformat(), "P_PV": 1000.0},
                {"timestamp": (now + timedelta(minutes=30)).isoformat(), "P_PV": 2500.456},
            ],
        }
    )


async def _publish(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    coordinator = entry.runtime_data.coordinator
    coordinator.data = EmhassData(plan=_plan(hass), last_success=dt_util.utcnow())
    coordinator.async_update_listeners()
    await hass.async_block_till_done()


async def test_the_emhass_series_attribute_is_absent_by_default(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _publish(hass, entry)

    state = hass.states.get(_entity_id(hass, entry, "pv_forecast"))
    assert "forecast" in state.attributes
    assert "forecasts" not in state.attributes


async def test_the_emhass_series_is_published_alongside_the_native_one(
    hass: HomeAssistant,
) -> None:
    """Matching the entity id is only half of it: a consumer written for EMHASS
    reads `forecasts`, not this integration's `forecast`. The native attribute
    stays because the dashboard cards read it."""
    entry = await _setup(hass, standard_names=True)
    await _publish(hass, entry)

    state = hass.states.get("sensor.p_pv_forecast")
    assert "forecast" in state.attributes
    assert state.attributes["forecasts"] == [
        {"date": state.attributes["forecast"][-1]["time"], "p_pv_forecast": "2500.46"}
    ]


async def test_the_emhass_series_starts_at_the_current_timestep(hass: HomeAssistant) -> None:
    """EMHASS slices from now, so a consumer reading element 0 expects now --
    not the start of a day-ahead plan that began at midnight."""
    entry = await _setup(hass, standard_names=True)
    await _publish(hass, entry)

    state = hass.states.get("sensor.p_pv_forecast")
    assert len(state.attributes["forecast"]) == 2
    assert len(state.attributes["forecasts"]) == 1


async def test_prices_use_emhass_own_attribute_name_and_precision(hass: HomeAssistant) -> None:
    """Each quantity has its own list name upstream, and prices round to four
    decimals rather than two."""
    entry = await _setup(hass, standard_names=True)
    now = dt_util.utcnow()
    coordinator = entry.runtime_data.coordinator
    coordinator.data = EmhassData(
        plan=_plan(hass),
        buy_price=Series([Point(now + timedelta(minutes=30), 1.23456)]),
        last_success=now,
    )
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    state = hass.states.get("sensor.unit_load_cost")
    assert state.attributes["unit_load_cost_forecasts"][0]["unit_load_cost"] == "1.2346"


async def test_a_sensor_with_no_series_gets_no_alias(hass: HomeAssistant) -> None:
    """optim_status and total_cost_fun_value are scalars upstream too."""
    entry = await _setup(hass, standard_names=True)
    await _publish(hass, entry)

    state = hass.states.get("sensor.optim_status")
    assert not any(key.endswith("forecasts") for key in state.attributes)


# --- where the option is offered ------------------------------------------------


async def _install_flow(hass: HomeAssistant) -> EmhassCompanionConfigFlow:
    """The install flow, parked at the point where the grid step hands over."""
    flow = EmhassCompanionConfigFlow()
    flow.hass = hass
    flow._data = {"url": "http://localhost:5000", "emhass_version": "0.17.9"}
    flow._options = {}
    return flow


async def test_the_install_flow_does_not_ask_a_new_user(hass: HomeAssistant) -> None:
    """Nobody without EMHASS entities should have to answer this to get set up."""
    result = await (await _install_flow(hass)).async_step_compatibility()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_EMHASS_STANDARD_NAMES not in result["options"]


async def test_the_install_flow_asks_someone_migrating(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.p_pv_forecast", "1234")

    result = await (await _install_flow(hass)).async_step_compatibility()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "compatibility"
    assert "sensor.p_pv_forecast" in result["description_placeholders"]["existing"]


async def test_answering_the_install_step_stores_the_choice(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.p_pv_forecast", "1234")
    flow = await _install_flow(hass)

    result = await flow.async_step_compatibility({CONF_EMHASS_STANDARD_NAMES: True})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_EMHASS_STANDARD_NAMES] is True


async def test_the_options_step_warns_about_an_occupied_id(hass: HomeAssistant) -> None:
    """Turning it on while EMHASS still publishes would silently half-apply."""
    entry = await _setup(hass)
    hass.states.async_set("sensor.p_pv_forecast", "1234")

    flow = EmhassCompanionOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    result = await flow.async_step_compatibility()

    assert result["type"] is FlowResultType.FORM
    assert "sensor.p_pv_forecast" in result["description_placeholders"]["conflicts"]


async def test_the_options_step_says_nothing_when_every_id_is_free(hass: HomeAssistant) -> None:
    entry = await _setup(hass)

    flow = EmhassCompanionOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    result = await flow.async_step_compatibility()

    assert result["description_placeholders"]["conflicts"] == ""

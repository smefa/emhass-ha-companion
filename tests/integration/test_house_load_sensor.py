"""Tests for the "Create a house load sensor" feature.

Covers the two pieces that only make sense against a real ``hass``: the
sensor's own subtraction (``NetHouseLoadSensor``, driven directly like the
other per-device sensors in test_load_sensors.py) and the config-entry-level
entity id resolution (``EmhassConfig._resolve_net_house_load_entity``, driven
like the live-blend readers in test_live_blend.py).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.configuration import EmhassConfig
from custom_components.emhass_companion.const import (
    CONF_HOUSE_LOAD_TOTAL_ENTITY,
    CONF_LOAD,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    DOMAIN,
    NET_HOUSE_LOAD_KEY,
    PROFILE_KEY_LOAD_SENSOR,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry, DeferrableRuntime
from custom_components.emhass_companion.sensor import NetHouseLoadSensor

# --- NetHouseLoadSensor.native_value / available -------------------------------


def _coordinator(hass: HomeAssistant) -> EmhassCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"})
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    return EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)


def _sensor(hass: HomeAssistant, loads: list[DeferrableRuntime]) -> NetHouseLoadSensor:
    """A sensor with one sample already recorded, as async_added_to_hass would.

    native_value averages over a trailing window rather than reading
    instantaneously (see NetHouseLoadSensor) -- without an initial sample
    there would be no history yet for these tests to read back out.
    """
    sensor = NetHouseLoadSensor(_coordinator(hass), "sensor.total_house", loads)
    sensor.hass = hass
    sensor._record_sample(dt_util.utcnow())
    return sensor


async def test_equals_total_with_no_deferrables(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.total_house", "1500")
    sensor = _sensor(hass, loads=[])
    assert sensor.native_value == 1500.0


async def test_subtracts_a_running_deferrables_power_sensor(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.total_house", "3000")
    hass.states.async_set("sensor.dishwasher_power", "1200")
    load = DeferrableRuntime(
        subentry_id="d1", name="Dishwasher", power_sensor="sensor.dishwasher_power"
    )
    assert _sensor(hass, [load]).native_value == 1800.0


async def test_converts_a_deferrables_kw_reading(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.total_house", "3000")
    hass.states.async_set("sensor.dishwasher_power", "1.2", {"unit_of_measurement": "kW"})
    load = DeferrableRuntime(
        subentry_id="d1", name="Dishwasher", power_sensor="sensor.dishwasher_power"
    )
    assert _sensor(hass, [load]).native_value == 1800.0


async def test_imputes_nominal_power_for_a_switch_only_load(hass: HomeAssistant) -> None:
    """No power meter on the load -- only a control entity -- still counts."""
    hass.states.async_set("sensor.total_house", "3000")
    hass.states.async_set("switch.dishwasher", STATE_ON)
    load = DeferrableRuntime(
        subentry_id="d1",
        name="Dishwasher",
        control_entity="switch.dishwasher",
        nominal_power_w=1200,
    )
    assert _sensor(hass, [load]).native_value == 1800.0


async def test_ignores_a_load_with_no_running_source_at_all(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.total_house", "3000")
    load = DeferrableRuntime(subentry_id="d1", name="Dishwasher")
    assert _sensor(hass, [load]).native_value == 3000.0


async def test_clamps_at_zero_rather_than_going_negative(hass: HomeAssistant) -> None:
    """The total and each deferrable update on their own schedules, not in lockstep."""
    hass.states.async_set("sensor.total_house", "500")
    hass.states.async_set("sensor.dishwasher_power", "1200")
    load = DeferrableRuntime(
        subentry_id="d1", name="Dishwasher", power_sensor="sensor.dishwasher_power"
    )
    assert _sensor(hass, [load]).native_value == 0.0


async def test_reports_whole_watts(hass: HomeAssistant) -> None:
    """An unrounded trailing average differs in its last decimal on every
    recomputation, so the recorder stores a row for each of the thousands of
    daily rewrites even when the load has not actually moved."""
    hass.states.async_set("sensor.total_house", "1500")
    hass.states.async_set("sensor.dishwasher_power", "333.333333")
    load = DeferrableRuntime(
        subentry_id="d1", name="Dishwasher", power_sensor="sensor.dishwasher_power"
    )
    value = _sensor(hass, [load]).native_value
    assert value == 1167
    assert value == int(value)


async def test_native_value_is_none_when_the_total_entity_is_unavailable(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("sensor.total_house", STATE_UNAVAILABLE)
    assert _sensor(hass, []).native_value is None


async def test_available_is_false_when_the_total_entity_is_unknown(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.total_house", STATE_UNKNOWN)
    assert _sensor(hass, []).available is False


async def test_available_is_true_when_the_total_entity_has_a_reading(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.total_house", "1500")
    assert _sensor(hass, []).available is True


# --- entity id resolution (EmhassConfig.from_entry) -----------------------------


async def test_load_sensor_entity_is_left_unresolved_before_registration(
    hass: HomeAssistant,
) -> None:
    """Before the sensor platform has ever run once, nothing to point at yet."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        options={
            CONF_HOUSE_LOAD_TOTAL_ENTITY: "sensor.total_house",
            CONF_LOAD: {CONF_PROFILE: PROFILE_KEY_LOAD_SENSOR, CONF_PROFILE_OPTIONS: {}},
        },
    )
    entry.add_to_hass(hass)

    config = EmhassConfig.from_entry(hass, entry)

    assert config.load.options.get("entity") is None


async def test_load_sensor_entity_resolves_once_registered(hass: HomeAssistant) -> None:
    """Mirrors what __init__.async_setup_entry does before syncing EMHASS's config."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        options={
            CONF_HOUSE_LOAD_TOTAL_ENTITY: "sensor.total_house",
            CONF_LOAD: {CONF_PROFILE: PROFILE_KEY_LOAD_SENSOR, CONF_PROFILE_OPTIONS: {}},
        },
    )
    entry.add_to_hass(hass)
    registry_entry = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_{NET_HOUSE_LOAD_KEY}",
        config_entry=entry,
        suggested_object_id=NET_HOUSE_LOAD_KEY,
    )

    config = EmhassConfig.from_entry(hass, entry)

    assert config.load.options["entity"] == registry_entry.entity_id


async def test_load_sensor_entity_is_untouched_for_a_different_load_profile(
    hass: HomeAssistant,
) -> None:
    """A house_load_total_entity left over from a since-changed profile is inert."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        options={
            CONF_HOUSE_LOAD_TOTAL_ENTITY: "sensor.total_house",
            CONF_LOAD: {
                CONF_PROFILE: "load/emhass_native",
                CONF_PROFILE_OPTIONS: {"average_w": 900},
            },
        },
    )
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_{NET_HOUSE_LOAD_KEY}",
        config_entry=entry,
        suggested_object_id=NET_HOUSE_LOAD_KEY,
    )

    config = EmhassConfig.from_entry(hass, entry)

    assert "entity" not in config.load.options

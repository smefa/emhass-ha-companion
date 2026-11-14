"""Snow cover wired through the coordinator, the action and the binary sensor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.emhass_companion import snow_cover
from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    CONF_PV,
    CONF_PV_ENTITY,
    CONF_TEMPERATURE,
    DOMAIN,
    SNOW_COVER_KEY,
)
from custom_components.emhass_companion.models import Point, Series

OPTIONS = {
    CONF_PV: {CONF_PROFILE: "pv/solcast", CONF_PROFILE_OPTIONS: {}},
    CONF_PV_ENTITY: "sensor.pv_now",
    CONF_TEMPERATURE: {
        CONF_PROFILE: "temperature/weather_entity",
        CONF_PROFILE_OPTIONS: {"entity": "weather.home"},
    },
}


async def _setup(hass: HomeAssistant, options: dict[str, Any] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        options=OPTIONS if options is None else options,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.emhass_companion.EmhassClient") as client_cls:
        client_cls.return_value = AsyncMock(spec=EmhassClient)
        client_cls.return_value.async_get_version = AsyncMock(return_value="0.17.9")
        client_cls.return_value.base_url = "http://localhost:5000"
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _sensor_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_{SNOW_COVER_KEY}"
    )
    assert entity_id is not None
    return entity_id


def _pv_series(now: datetime, watts: float = 3000.0) -> Series:
    start = now - timedelta(hours=12)
    return Series(Point(start + timedelta(minutes=30 * i), watts) for i in range(48))


async def test_the_binary_sensor_follows_the_action(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    entity_id = _sensor_id(hass, entry)
    assert hass.states.get(entity_id).state == "off"

    await hass.services.async_call(DOMAIN, "set_snow_cover", {"mode": "covered"}, blocking=True)
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["state"] == "covered"
    assert state.attributes["source"] == "manual"
    assert state.attributes["factor"] == snow_cover.MANUAL_COVERED_FACTOR
    assert state.attributes["since"] is not None

    await hass.services.async_call(DOMAIN, "set_snow_cover", {"mode": "clear"}, blocking=True)
    assert hass.states.get(entity_id).state == "off"

    await hass.services.async_call(DOMAIN, "set_snow_cover", {"mode": "auto"}, blocking=True)
    assert entry.runtime_data.coordinator.snow_cover.override == snow_cover.MODE_AUTO


async def test_the_action_rejects_an_unknown_mode(hass: HomeAssistant) -> None:
    await _setup(hass)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "set_snow_cover", {"mode": "melted"}, blocking=True)


async def test_covered_is_refused_without_a_solar_forecast_source(hass: HomeAssistant) -> None:
    """EMHASS forecasts the sun itself then, so a reduction would be shown but not applied."""
    entry = await _setup(hass, {})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "set_snow_cover", {"mode": "covered"}, blocking=True)
    assert not entry.runtime_data.coordinator.snow_cover.active
    await hass.services.async_call(DOMAIN, "set_snow_cover", {"mode": "clear"}, blocking=True)


async def test_the_measurement_is_held_against_the_step_that_just_ended(
    hass: HomeAssistant,
) -> None:
    """The live value is a trailing average, so a forecast ramp ahead is not a shortfall."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data.coordinator
    now = datetime(2026, 1, 10, 19, 0, tzinfo=UTC)
    pv = Series([Point(now - timedelta(hours=1), 600.0), Point(now, 3000.0)])
    hass.states.async_set("weather.home", "sunny", {"temperature": -3})
    coordinator._update_snow_cover(now + timedelta(seconds=5), pv, 580.0)
    assert [s.planned_w for s in coordinator.snow_cover.samples] == [600.0]


async def test_the_action_needs_a_loaded_entry(hass: HomeAssistant) -> None:
    from custom_components.emhass_companion.services import async_register_services

    async_register_services(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "set_snow_cover", {"mode": "clear"}, blocking=True)


async def test_the_state_is_restored_after_a_restart(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    coordinator = entry.runtime_data.coordinator
    await hass.services.async_call(DOMAIN, "set_snow_cover", {"mode": "covered"}, blocking=True)
    saved = snow_cover.to_json(coordinator.snow_cover)

    coordinator.snow_cover = snow_cover.SnowCoverState()
    with patch.object(coordinator._snow_store, "async_load", AsyncMock(return_value=saved)):
        await coordinator.async_load_snow_cover()
    assert coordinator.snow_cover.state == snow_cover.STATE_COVERED
    assert coordinator.snow_cover.override == snow_cover.MODE_COVERED


async def test_a_run_measures_scales_and_reports(hass: HomeAssistant) -> None:
    """Cold clear day after snowfall, output at 9 % of the raw forecast for two hours."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data.coordinator
    hass.states.async_set("sensor.pv_now", "270")
    coordinator.async_start_pv_live_tracking()

    # Late morning at the test instance's location, well past its sunrise.
    start = datetime(2026, 1, 10, 18, 0, tzinfo=UTC)
    pv = _pv_series(start + timedelta(hours=3))
    # The weather entity reports, so a shortfall is only snow once it has snowed.
    hass.states.async_set("weather.home", "snowy", {"temperature": -3})
    coordinator._update_snow_cover(start - timedelta(hours=6), pv, None)
    hass.states.async_set("weather.home", "sunny", {"temperature": -3})
    factor = 1.0
    for step in range(1, 10):
        now = start + timedelta(minutes=15 * step)
        with patch.object(coordinator, "_read_pv_live", return_value=270.0):
            factor = coordinator._update_snow_cover(now, pv, 270.0)
    assert coordinator.snow_cover.state == snow_cover.STATE_COVERED
    assert factor == pytest.approx(0.09)
    assert {s.planned_w for s in coordinator.snow_cover.samples} == {3000.0}


async def test_weather_state_primes_suspected_without_a_measurement(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    coordinator = entry.runtime_data.coordinator
    hass.states.async_set("weather.home", "snowy", {"temperature": -1})
    now = datetime(2026, 1, 10, 22, 0, tzinfo=UTC)
    factor = coordinator._update_snow_cover(now, _pv_series(now), None)
    assert coordinator.snow_cover.state == snow_cover.STATE_SUSPECTED
    assert factor == snow_cover.SUSPECTED_FACTOR


async def test_without_a_weather_entity_measurement_still_detects(hass: HomeAssistant) -> None:
    options = {CONF_PV: OPTIONS[CONF_PV], CONF_PV_ENTITY: "sensor.pv_now"}
    entry = await _setup(hass, options)
    coordinator = entry.runtime_data.coordinator
    assert coordinator._weather_entity() is None
    assert coordinator._read_outdoor_temperature() is None
    now = datetime(2026, 1, 10, 22, 0, tzinfo=UTC)
    # No source, no temperature: the gate stays shut and nothing happens.
    assert coordinator._update_snow_cover(now, _pv_series(now), 0.0) == 1.0
    assert coordinator.snow_cover.state == snow_cover.STATE_CLEAR


async def test_a_non_weather_temperature_entity_gives_a_reading_but_no_priming(
    hass: HomeAssistant,
) -> None:
    options = {
        CONF_PV: OPTIONS[CONF_PV],
        CONF_TEMPERATURE: {
            CONF_PROFILE: "temperature/generic_attribute",
            CONF_PROFILE_OPTIONS: {"entity": "sensor.outdoor"},
        },
    }
    entry = await _setup(hass, options)
    coordinator = entry.runtime_data.coordinator
    hass.states.async_set("sensor.outdoor", "-4.5")
    assert coordinator._weather_entity() is None
    assert coordinator._read_outdoor_temperature() == -4.5


async def test_any_weather_entity_serves_when_no_temperature_source_is_configured(
    hass: HomeAssistant,
) -> None:
    options = {CONF_PV: OPTIONS[CONF_PV], CONF_PV_ENTITY: "sensor.pv_now"}
    entry = await _setup(hass, options)
    coordinator = entry.runtime_data.coordinator
    hass.states.async_set("weather.smhi", "snowy", {"temperature": 30, "temperature_unit": "°F"})
    hass.states.async_set("weather.home", "snowy", {"temperature": -1, "temperature_unit": "°C"})
    assert coordinator._weather_entity() == "weather.home"
    assert coordinator._read_outdoor_temperature() == -1
    hass.states.async_remove("weather.home")
    assert coordinator._weather_entity() == "weather.smhi"
    assert coordinator._read_outdoor_temperature() == pytest.approx(-1.11, abs=0.01)


async def test_without_a_live_solar_sensor_weather_alone_changes_nothing(
    hass: HomeAssistant,
) -> None:
    """A suspicion that no measurement could ever lift must not be raised."""
    options = {CONF_PV: OPTIONS[CONF_PV], CONF_TEMPERATURE: OPTIONS[CONF_TEMPERATURE]}
    entry = await _setup(hass, options)
    coordinator = entry.runtime_data.coordinator
    hass.states.async_set("weather.home", "snowy", {"temperature": -1})
    now = datetime(2026, 1, 10, 22, 0, tzinfo=UTC)
    assert coordinator._update_snow_cover(now, _pv_series(now), None) == 1.0
    assert coordinator.snow_cover.state == snow_cover.STATE_CLEAR

    await hass.services.async_call(DOMAIN, "set_snow_cover", {"mode": "covered"}, blocking=True)
    assert (
        coordinator._update_snow_cover(now, _pv_series(now), None)
        == snow_cover.MANUAL_COVERED_FACTOR
    )

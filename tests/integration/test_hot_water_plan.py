"""Hot-water plan edits persist and reach the running model without reloads."""

from copy import deepcopy
from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import DOMAIN
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.hot_water_plan import (
    PeriodEnabled,
    PeriodEnd,
    PeriodEnergy,
    PeriodStart,
)
from custom_components.emhass_companion.models import LastRun


def _period(**changes):
    return {
        "name": "Morning",
        "draw_start": "06:00:00",
        "draw_end": "08:00:00",
        "draw_energy_kwh": 3,
        **changes,
    }


@pytest.fixture
async def tank(hass, request):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[
            {
                "subentry_type": "thermal_load",
                "title": "Tank",
                "unique_id": "tank",
                "data": {
                    "name": "Tank",
                    "thermal_model": "water_tank",
                    "tank_volume_litres": 200,
                    "nominal_power_w": 2000,
                    "temperature_sensor": "sensor.tank_temperature",
                    "comfort_temperature": 60,
                    "setback_temperature": 50,
                    "max_temperature": 65,
                    "hot_water_periods": [_period()],
                },
            }
        ],
    )
    entry.add_to_hass(hass)
    if getattr(request, "param", False):
        subentry = next(iter(entry.subentries.values()))
        data = dict(subentry.data)
        data.pop("hot_water_periods")
        data.update(draw_start="07:00:00", draw_end="08:00:00", draw_energy_kwh=3)
        hass.config_entries.async_update_subentry(entry, subentry, data=data)
    hass.states.async_set("sensor.tank_temperature", "55", {"unit_of_measurement": "°C"})
    client = AsyncMock(spec=EmhassClient)
    client.base_url = "http://localhost:5000"
    client.async_get_version.return_value = "0.18.8"
    client.async_optimize.return_value = (LastRun(status="no-run"), None)
    with patch("custom_components.emhass_companion.EmhassClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        coordinator = entry.runtime_data.coordinator
        coordinator.async_request_settings_refresh = AsyncMock()
        load = entry.runtime_data.loads.all()[0]
        yield entry, coordinator, load


def _entity(hass, entry, load, suffix, domain):
    return er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, f"{entry.entry_id}_{load.subentry_id}_hot_water_period_{suffix}"
    )


async def _set_plan(hass, entry, load, periods):
    device = next(
        device
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        if (DOMAIN, f"{entry.entry_id}_{load.subentry_id}") in device.identifiers
    )
    await hass.services.async_call(
        DOMAIN, "set_hot_water_plan", {"device_id": device.id, "periods": periods}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_controls_persist_and_disabled_plan_uses_standby_without_reload(hass, tank):
    entry, coordinator, load = tank
    identity = entry.runtime_data
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        for domain, suffix, service, data in (
            ("time", "1_start", "set_value", {"time": "07:00:00"}),
            ("time", "1_end", "set_value", {"time": "09:00:00"}),
            ("number", "1_energy", "set_value", {"value": 4}),
            ("switch", "1_enabled", "turn_off", {}),
        ):
            await hass.services.async_call(
                domain,
                service,
                {"entity_id": _entity(hass, entry, load, suffix, domain), **data},
                blocking=True,
            )
        await hass.async_block_till_done(wait_background_tasks=True)
        reload.assert_not_called()
    assert entry.runtime_data is identity
    assert coordinator.async_request_settings_refresh.await_count == 4
    stored = entry.subentries[load.subentry_id].data["hot_water_periods"]
    assert stored == [
        _period(draw_start="07:00:00", draw_end="09:00:00", draw_energy_kwh=4, enabled=False)
    ]
    # A fresh registry reads saved settings rather than stale restored states.
    restored = DeferrableRegistry(hass, entry)
    restored.sync()
    assert restored.all()[0].hot_water_periods == stored
    model = coordinator.deferrable_loads(datetime.now(UTC))[0].thermal
    payload = model.to_emhass(datetime.now(UTC), timedelta(minutes=30), 48)
    assert payload["min_temperatures"] == [50] * 48
    assert payload["draw_off_demand"] == [0] * 48
    assert hass.states.get(_entity(hass, entry, load, "1_start", "time")).state == "07:00:00"
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": _entity(hass, entry, load, "1_enabled", "switch")},
        blocking=True,
    )
    assert load.hot_water_periods[0]["enabled"] is True
    assert load.hot_water_periods[0]["draw_energy_kwh"] == 4


async def test_whole_plan_action_is_atomic_adds_controls_and_removes_slots_live(hass, tank):
    entry, coordinator, load = tank
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await _set_plan(hass, entry, load, [_period(enabled=False), _period(name="Evening")])
        reload.assert_not_called()
    coordinator.async_request_settings_refresh.assert_awaited_once()
    second = _entity(hass, entry, load, "2_start", "time")
    assert hass.states.get(second).state == "06:00:00"
    assert hass.states.get(_entity(hass, entry, load, "1_enabled", "switch")).state == "off"
    await _set_plan(hass, entry, load, [_period()])
    assert hass.states.get(second).state == "unavailable"
    # Reusing a slot reuses the same entity ID.
    await _set_plan(hass, entry, load, [_period(), _period(draw_start="18:00:00")])
    assert _entity(hass, entry, load, "2_start", "time") == second
    assert hass.states.get(second).state == "18:00:00"


@pytest.mark.parametrize(
    "bad",
    [
        [],
        [_period()] * 9,
        [_period(enabled="false")],
        [_period(draw_energy_kwh=float("nan"))],
        [_period(draw_start="bad")],
    ],
)
async def test_invalid_action_leaves_saved_and_running_plan_unchanged(hass, tank, bad):
    entry, coordinator, load = tank
    before = deepcopy(load.hot_water_periods)
    with pytest.raises(ServiceValidationError):
        await _set_plan(hass, entry, load, bad)
    assert load.hot_water_periods == before
    assert entry.subentries[load.subentry_id].data["hot_water_periods"] == before
    coordinator.async_request_settings_refresh.assert_not_awaited()


async def test_saved_setup_edits_update_runtime_and_entities_without_reload(hass, tank):
    entry, coordinator, load = tank
    subentry = entry.subentries[load.subentry_id]
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        hass.config_entries.async_update_subentry(
            entry,
            subentry,
            data={**subentry.data, "hot_water_periods": [_period(enabled=False), _period()]},
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        reload.assert_not_called()
    coordinator.async_request_settings_refresh.assert_awaited_once()
    assert load.hot_water_periods[0]["enabled"] is False
    assert hass.states.get(_entity(hass, entry, load, "2_start", "time")).state == "06:00:00"
    # Disabled periods remain editable.
    await PeriodStart(coordinator, load, 0).async_set_value(time(5))
    assert load.hot_water_periods[0]["enabled"] is False
    assert load.hot_water_periods[0]["draw_start"] == "05:00:00"


async def test_removed_slots_stay_managed_after_restart(hass, tank):
    entry, _, load = tank
    await _set_plan(hass, entry, load, [_period(), _period()])
    second = _entity(hass, entry, load, "2_start", "time")
    await _set_plan(hass, entry, load, [_period()])
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert any(
        second in platform.entities
        for platform in entity_platform.async_get_platforms(hass, DOMAIN)
        if platform.domain == "time"
    )
    assert hass.states.get(second).state == "unavailable"


@pytest.mark.parametrize("tank", [True], indirect=True)
async def test_legacy_migration_removes_comfort_entities_live(hass, tank):
    entry, coordinator, load = tank
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_{load.subentry_id}"
    assert registry.async_get_entity_id("time", DOMAIN, f"{prefix}_comfort_start")
    subentry = entry.subentries[load.subentry_id]
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        hass.config_entries.async_update_subentry(
            entry, subentry, data={**subentry.data, "hot_water_periods": [_period()]}
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        reload.assert_not_called()
    for key in ("comfort_start", "comfort_end"):
        assert registry.async_get_entity_id("time", DOMAIN, f"{prefix}_{key}") is None
    assert hass.states.get(_entity(hass, entry, load, "1_start", "time")).state == "06:00:00"
    coordinator.async_request_settings_refresh.assert_awaited_once()


async def test_disabled_tank_plan_edits_save_without_refresh(hass, tank):
    entry, coordinator, load = tank
    # Load enable/disable transition behaviour is covered across load types
    # in test_disabled_load_edits; exercise every hot-water editing route here.
    load.enabled = False
    await PeriodStart(coordinator, load, 0).async_set_value(time(5))
    await PeriodEnd(coordinator, load, 0).async_set_value(time(9))
    await PeriodEnergy(coordinator, load, 0).async_set_native_value(4)
    await PeriodEnabled(coordinator, load, 0).async_turn_off()
    assert entry.subentries[load.subentry_id].data["hot_water_periods"] == [
        _period(draw_start="05:00:00", draw_end="09:00:00", draw_energy_kwh=4, enabled=False)
    ]
    await _set_plan(hass, entry, load, [_period(), _period(name="Evening")])
    subentry = entry.subentries[load.subentry_id]
    hass.config_entries.async_update_subentry(
        entry, subentry, data={**subentry.data, "hot_water_periods": [_period(enabled=False)]}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert load.hot_water_periods == [_period(enabled=False)]
    coordinator.async_request_settings_refresh.assert_not_awaited()

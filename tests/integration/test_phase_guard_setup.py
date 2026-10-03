"""The phase guard wired up through a real config entry.

The executor's clamp itself is covered in test_executor.py; this is about the
plumbing around it -- the entities exist only when a fuse is configured, the
meter's own state changes reach the guard, and the grid form refuses half a
configuration.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import DOMAIN, ISSUE_PHASE_CLAMP_OFF

PHASE_GRID = {
    "grid_import_max_w": 11000,
    "grid_export_max_w": 11000,
    "phase_l1_entity": "sensor.l1",
    "phase_l2_entity": "sensor.l2",
    "phase_l3_entity": "sensor.l3",
    "main_fuse_a": 16,
    "phase_margin_a": 1.0,
}


async def _setup_entry(
    hass: HomeAssistant, grid: dict[str, Any] | None, extra: dict[str, Any] | None = None
) -> MockConfigEntry:
    options: dict[str, Any] = dict(extra or {})
    if grid is not None:
        options["grid"] = grid
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"}, options=options)
    entry.add_to_hass(hass)

    with patch("custom_components.emhass_companion.EmhassClient") as client_cls:
        client_cls.return_value = AsyncMock(spec=EmhassClient)
        client_cls.return_value.async_get_version = AsyncMock(return_value="0.17.9")
        client_cls.return_value.base_url = "http://localhost:5000"
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _set_phases(hass: HomeAssistant, l1: float, l2: float = 300, l3: float = 300) -> None:
    for entity_id, watts in (("sensor.l1", l1), ("sensor.l2", l2), ("sensor.l3", l3)):
        hass.states.async_set(entity_id, str(watts), {"unit_of_measurement": "W"})


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, platform: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")


async def test_no_fuse_means_no_guard_and_no_entities(hass: HomeAssistant) -> None:
    entry = await _setup_entry(hass, None)

    assert entry.runtime_data.coordinator.phase_guard is None
    assert _entity_id(hass, entry, "sensor", "phase_headroom") is None
    assert _entity_id(hass, entry, "binary_sensor", "phase_guard_active") is None


async def test_meter_changes_reach_the_guard_and_its_sensor(hass: HomeAssistant) -> None:
    _set_phases(hass, 400)
    entry = await _setup_entry(hass, PHASE_GRID)
    guard = entry.runtime_data.coordinator.phase_guard
    assert guard is not None

    headroom_id = _entity_id(hass, entry, "sensor", "phase_headroom")
    assert headroom_id is not None
    # 3 x (3450 - 400) W.
    assert float(hass.states.get(headroom_id).state) == 9150

    _set_phases(hass, 2600)
    await hass.async_block_till_done()

    assert guard.headroom_w == pytest.approx(2550)
    state = hass.states.get(headroom_id)
    assert float(state.state) == 2550
    assert state.attributes["worst_phase"] == "L1"
    assert state.attributes["fuse_a"] == 16

    guard_active_id = _entity_id(hass, entry, "binary_sensor", "phase_guard_active")
    assert guard_active_id is not None


async def test_unloading_stops_listening_to_the_meter(hass: HomeAssistant) -> None:
    _set_phases(hass, 400)
    entry = await _setup_entry(hass, PHASE_GRID)
    guard = entry.runtime_data.coordinator.phase_guard

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    _set_phases(hass, 2600)
    await hass.async_block_till_done()

    assert guard.headroom_w == pytest.approx(9150)


async def test_an_empty_fuse_turns_the_guard_off_whatever_the_phases(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(hass, None)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "grid"}
    )
    base = {
        "grid_import_max_w": 11000,
        "grid_export_max_w": 11000,
        "optimization_time_step": "15",
    }
    phases = {
        "phase_l1_entity": "sensor.l1",
        "phase_l2_entity": "sensor.l2",
        "phase_l3_entity": "sensor.l3",
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, "advanced": phases}
    )
    assert result["type"] == "create_entry"
    assert entry.options["grid"]["main_fuse_a"] is None
    assert entry.options["grid"]["phase_l2_entity"] == "sensor.l2"


async def test_two_phases_with_a_fuse_are_refused(hass: HomeAssistant) -> None:
    entry = await _setup_entry(hass, None)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "grid"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "grid_import_max_w": 11000,
            "grid_export_max_w": 11000,
            "optimization_time_step": "15",
            "advanced": {
                "phase_l1_entity": "sensor.l1",
                "phase_l2_entity": "sensor.l2",
                "main_fuse_a": 16,
            },
        },
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "phase_entities_incomplete"}


async def test_a_battery_without_a_power_sensor_raises_a_repair(hass: HomeAssistant) -> None:
    entry = await _setup_entry(
        hass, PHASE_GRID, extra={"battery": {"use_battery": True, "capacity_wh": 10000}}
    )

    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_PHASE_CLAMP_OFF)
    assert issue is not None
    assert issue.translation_key == "phase_clamp_off_no_battery_sensor"
    assert entry.runtime_data.coordinator.phase_guard is not None


async def test_no_repair_once_the_battery_is_measured(hass: HomeAssistant) -> None:
    await _setup_entry(
        hass,
        PHASE_GRID,
        extra={
            "battery": {"use_battery": True, "capacity_wh": 10000},
            "battery_power_entity": "sensor.battery_power",
        },
    )

    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_PHASE_CLAMP_OFF) is None

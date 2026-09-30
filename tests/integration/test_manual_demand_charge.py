"""The "Flat demand charge (manual)" network option and the migration onto it.

The option behaves exactly like the retired grid-step number did: one rate,
priced across the whole horizon on every run, with no window and no memory.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion import async_migrate_entry
from custom_components.emhass_companion.const import (
    CONF_NETWORK,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    DOMAIN,
    MANUAL_DEMAND_PROFILE_KEY,
)
from custom_components.emhass_companion.network_calendar import NetworkCalendar
from custom_components.emhass_companion.profiles import _load_profiles
from custom_components.emhass_companion.profiles.engine import resolve_network


async def test_profile_renders_a_flat_rate_and_nothing_else(hass: HomeAssistant, tmp_path) -> None:
    profile = _load_profiles(tmp_path / "profiles").profiles[MANUAL_DEMAND_PROFILE_KEY]
    resolved = resolve_network(hass, profile, {"demand_rate": 45.0})
    assert float(resolved["flat_demand_charge"]) == 45.0
    calendar = NetworkCalendar.from_resolved(resolved)
    assert not calendar.demand_charges
    assert not calendar.bands
    assert calendar.capacity_limit is None


def _entry(options: dict) -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, data={}, options=options, version=1, minor_version=1)


async def _migrated(hass: HomeAssistant, options: dict) -> MockConfigEntry:
    entry = _entry(options)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == 2
    return entry


async def test_a_number_and_no_tariff_moves_onto_the_manual_option(hass: HomeAssistant) -> None:
    entry = await _migrated(
        hass, {"grid": {"grid_import_max_w": 9000, "capacity_cost_per_kw": 45.0}}
    )
    assert entry.options["grid"] == {"grid_import_max_w": 9000}
    assert entry.options[CONF_NETWORK] == {
        CONF_PROFILE: MANUAL_DEMAND_PROFILE_KEY,
        CONF_PROFILE_OPTIONS: {"demand_rate": 45.0},
    }


async def test_an_existing_network_tariff_wins_and_the_number_is_dropped(
    hass: HomeAssistant,
) -> None:
    network = {CONF_PROFILE: "network/goteborg_energi", CONF_PROFILE_OPTIONS: {}}
    entry = await _migrated(hass, {"grid": {"capacity_cost_per_kw": 45.0}, CONF_NETWORK: network})
    assert entry.options["grid"] == {}
    assert entry.options[CONF_NETWORK] == network


async def test_a_zero_number_just_disappears(hass: HomeAssistant) -> None:
    entry = await _migrated(hass, {"grid": {"capacity_cost_per_kw": 0.0}})
    assert entry.options["grid"] == {}
    assert not entry.options.get(CONF_NETWORK)


async def test_an_entry_with_no_grid_options_migrates_cleanly(hass: HomeAssistant) -> None:
    entry = await _migrated(hass, {})
    assert "grid" not in entry.options

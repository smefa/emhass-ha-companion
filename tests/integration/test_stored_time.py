"""A corrupted stored time must cost one setting, not the whole setup.

``time.fromisoformat`` raises on anything it does not recognise, and both
callers run inside ``async_setup_entry``: a hand-edited ``.storage``, a
restored backup or a schema change would otherwise take the integration down
with a traceback naming neither the field nor the load. Every case here is a
value a real store has been seen to hold.
"""

from __future__ import annotations

from datetime import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.configuration import EmhassConfig
from custom_components.emhass_companion.const import (
    CONF_COMFORT_END,
    CONF_COMFORT_START,
    CONF_DAYAHEAD_FALLBACK_TIME,
    CONF_EARLIEST_START,
    CONF_LATEST_END,
    CONF_NOMINAL_POWER,
    DEFAULT_DAYAHEAD_FALLBACK_TIME,
    DOMAIN,
    ISSUE_BAD_STORED_TIME,
    SUBENTRY_TYPE_DEFERRABLE,
    SUBENTRY_TYPE_THERMAL,
)
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.thermal import (
    DEFAULT_COMFORT_END,
    DEFAULT_COMFORT_START,
)

# "25:00" is the shape a hand-edit produces (an hour that does not exist);
# "not-a-time" is what a schema change or a wrong field produces. Note that a
# bare 1330 is *not* in here: Python 3.11 onward reads "1330" as 13:30, and
# "24:00:00" as midnight, so neither is corruption and both must keep working.
UNREADABLE = ["25:00", "not-a-time", "13:30 ", "6:3O", []]


def _config_entry(raw_time) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        options={CONF_DAYAHEAD_FALLBACK_TIME: raw_time},
    )


def _load_entry(subentry_type: str, data: dict) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[
            {
                "subentry_type": subentry_type,
                "title": "Dishwasher",
                "unique_id": "dishwasher",
                "data": {CONF_NOMINAL_POWER: 2000, **data},
            }
        ],
    )


# --- the day-ahead fallback time ---------------------------------------------


@pytest.mark.parametrize("raw_time", UNREADABLE)
async def test_an_unreadable_fallback_time_falls_back_instead_of_raising(
    hass: HomeAssistant, raw_time
) -> None:
    entry = _config_entry(raw_time)
    entry.add_to_hass(hass)

    config = EmhassConfig.from_entry(hass, entry)

    assert config.dayahead_fallback_time == time.fromisoformat(DEFAULT_DAYAHEAD_FALLBACK_TIME)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_BAD_STORED_TIME)
    assert issue is not None
    assert "Day-ahead fallback time" in issue.translation_placeholders["details"]


async def test_a_readable_fallback_time_is_used_and_raises_nothing(hass: HomeAssistant) -> None:
    entry = _config_entry("06:45:00")
    entry.add_to_hass(hass)

    config = EmhassConfig.from_entry(hass, entry)

    assert config.dayahead_fallback_time == time(6, 45)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_BAD_STORED_TIME) is None


async def test_the_repair_clears_once_the_value_parses_again(hass: HomeAssistant) -> None:
    """Otherwise the user fixes the setting and the warning stays forever."""
    broken = _config_entry("25:00")
    broken.add_to_hass(hass)
    EmhassConfig.from_entry(hass, broken)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_BAD_STORED_TIME) is not None

    hass.config_entries.async_update_entry(
        broken, options={CONF_DAYAHEAD_FALLBACK_TIME: "13:30:00"}
    )
    EmhassConfig.from_entry(hass, broken)

    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_BAD_STORED_TIME) is None


# --- a load's window and comfort band -----------------------------------------


@pytest.mark.parametrize("raw_time", UNREADABLE)
async def test_an_unreadable_window_leaves_the_load_unconstrained(
    hass: HomeAssistant, raw_time
) -> None:
    entry = _load_entry(
        SUBENTRY_TYPE_DEFERRABLE,
        {CONF_EARLIEST_START: raw_time, CONF_LATEST_END: "22:00:00"},
    )
    entry.add_to_hass(hass)

    registry = DeferrableRegistry(hass, entry)
    registry.sync()

    load = registry.all()[0]
    assert load.earliest_start is None
    assert load.latest_end == time(22, 0)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_BAD_STORED_TIME)
    assert issue is not None
    # Named by load *and* field: a user with six loads needs to know which.
    assert "Dishwasher: earliest start" in issue.translation_placeholders["details"]


@pytest.mark.parametrize("raw_time", UNREADABLE)
async def test_an_unreadable_comfort_band_falls_back_to_the_default(
    hass: HomeAssistant, raw_time
) -> None:
    entry = _load_entry(
        SUBENTRY_TYPE_THERMAL,
        {CONF_COMFORT_START: raw_time, CONF_COMFORT_END: raw_time},
    )
    entry.add_to_hass(hass)

    registry = DeferrableRegistry(hass, entry)
    registry.sync()

    load = registry.all()[0]
    assert load.comfort_start == DEFAULT_COMFORT_START
    assert load.comfort_end == DEFAULT_COMFORT_END
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_BAD_STORED_TIME) is not None


async def test_an_unset_window_is_not_reported_as_corruption(hass: HomeAssistant) -> None:
    """None and "" mean "no window", which is the normal case for most loads."""
    entry = _load_entry(
        SUBENTRY_TYPE_DEFERRABLE,
        {CONF_EARLIEST_START: "", CONF_LATEST_END: None},
    )
    entry.add_to_hass(hass)

    registry = DeferrableRegistry(hass, entry)
    registry.sync()

    load = registry.all()[0]
    assert load.earliest_start is None
    assert load.latest_end is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_BAD_STORED_TIME) is None

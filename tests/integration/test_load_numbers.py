"""Per-load number entities, instantiated against a real coordinator.

The interesting one is the run-time box: EMHASS can only honour a run time that
is a whole number of optimisation timesteps, so the entity's step has to follow
the configured timestep rather than a hardcoded quarter hour. At a 10-minute
step, arrows that move in 0.25 h offer values that cannot be solved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    CONF_MAX_STARTUPS,
    CONF_MINIMUM_POWER,
    CONF_NOMINAL_POWER,
    CONF_STARTUP_PENALTY,
    CONF_TIME_STEP,
    DOMAIN,
    SUBENTRY_TYPE_DEFERRABLE,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.number import LOAD_NUMBERS, LoadNumber


def _numbers(hass: HomeAssistant, step_minutes: int) -> dict[str, LoadNumber]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        options={CONF_TIME_STEP: step_minutes},
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_DEFERRABLE,
                "title": "Car",
                "unique_id": "car",
                "data": {
                    CONF_NOMINAL_POWER: 11000,
                    CONF_MINIMUM_POWER: 1380,
                    CONF_STARTUP_PENALTY: 0.5,
                    CONF_MAX_STARTUPS: 2,
                },
            }
        ],
    )
    entry.add_to_hass(hass)

    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)
    load = loads.get(next(iter(entry.subentries)))

    return {
        description.key: LoadNumber(coordinator, load, description) for description in LOAD_NUMBERS
    }


@pytest.mark.parametrize(
    ("step_minutes", "expected"),
    [(15, 0.25), (30, 0.5), (10, 1 / 6), (60, 1.0)],
)
async def test_run_time_steps_by_the_optimisation_timestep(
    hass: HomeAssistant, step_minutes, expected
) -> None:
    numbers = _numbers(hass, step_minutes)
    assert numbers["operating_hours"].native_step == pytest.approx(expected)


async def test_other_numbers_keep_their_own_step(hass: HomeAssistant) -> None:
    numbers = _numbers(hass, 10)
    assert numbers["nominal_power"].native_step == 10
    assert numbers["max_startups"].native_step == 1


async def test_the_new_numbers_are_seeded_from_the_subentry(hass: HomeAssistant) -> None:
    """The subentry holds what a load starts from; the entity owns it after that."""
    numbers = _numbers(hass, 15)
    assert numbers["minimum_power"].native_value == 1380
    assert numbers["startup_penalty"].native_value == 0.5
    assert numbers["max_startups"].native_value == 2


async def test_setting_a_number_reaches_the_live_load(hass: HomeAssistant) -> None:
    """This is the whole point of these being entities: an automation can change
    them without reloading the config entry."""
    numbers = _numbers(hass, 15)
    load = numbers["minimum_power"].load

    await numbers["minimum_power"].async_set_native_value(6000)
    await numbers["max_startups"].async_set_native_value(3)

    assert load.minimum_power_w == 6000
    # An int, because EMHASS treats this as a count of startups.
    assert load.max_startups == 3
    assert isinstance(load.max_startups, int)

    projected = load.to_load(datetime(2026, 7, 28, 10, 0, tzinfo=UTC), 15)
    assert projected.minimum_power_w == 6000
    assert projected.max_startups == 3

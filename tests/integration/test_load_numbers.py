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
    CONF_MINIMUM_OFF_TIME,
    CONF_MINIMUM_ON_TIME,
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
                    CONF_MINIMUM_ON_TIME: 20,
                    CONF_MINIMUM_OFF_TIME: 10,
                },
            }
        ],
    )
    entry.add_to_hass(hass)

    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)
    # These tests are about a number entity landing on the load, not about the
    # refresh a value change now kicks off (that belongs to the coordinator's
    # own tests) -- stub it out rather than letting a real refresh run against
    # a mocked client and leave its debounce timer ticking past the test.
    coordinator.async_request_settings_refresh = AsyncMock()
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
    assert numbers["minimum_on_time"].native_step == 1
    assert numbers["minimum_off_time"].native_step == 1


async def test_the_new_numbers_are_seeded_from_the_subentry(hass: HomeAssistant) -> None:
    """The subentry holds what a load starts from; the entity owns it after that."""
    numbers = _numbers(hass, 15)
    assert numbers["minimum_power"].native_value == 1380
    assert numbers["startup_penalty"].native_value == 0.5
    assert numbers["max_startups"].native_value == 2
    assert numbers["minimum_on_time"].native_value == 20
    assert numbers["minimum_off_time"].native_value == 10


async def test_setting_a_number_reaches_the_live_load(hass: HomeAssistant) -> None:
    """This is the whole point of these being entities: an automation can change
    them without reloading the config entry."""
    numbers = _numbers(hass, 15)
    load = numbers["minimum_power"].load

    await numbers["minimum_power"].async_set_native_value(6000)
    await numbers["max_startups"].async_set_native_value(3)
    await numbers["minimum_on_time"].async_set_native_value(30)
    await numbers["minimum_off_time"].async_set_native_value(15)

    assert load.minimum_power_w == 6000
    # An int, because EMHASS treats this as a count of startups.
    assert load.max_startups == 3
    assert isinstance(load.max_startups, int)
    assert load.minimum_on_time_minutes == 30
    assert load.minimum_off_time_minutes == 15

    projected = load.to_load(datetime(2026, 7, 28, 10, 0, tzinfo=UTC), 15)
    assert projected.minimum_power_w == 6000
    assert projected.max_startups == 3
    assert projected.minimum_on_time_minutes == 30
    assert projected.minimum_off_time_minutes == 15


async def test_a_standard_load_never_gets_a_reachability_issue(hass: HomeAssistant) -> None:
    """The thermal reachability check replays a comfort band against a
    heating_rate -- neither of which an ordinary load has. Saving one of its
    numbers must stay a no-op for that check, not just happen not to crash."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.emhass_companion.const import ISSUE_THERMAL_UNREACHABLE

    numbers = _numbers(hass, 15)
    load = numbers["minimum_power"].load
    assert not load.is_thermal

    await numbers["minimum_power"].async_set_native_value(6000)

    registry = ir.async_get(hass)
    assert (
        registry.async_get_issue(DOMAIN, f"{ISSUE_THERMAL_UNREACHABLE}_{load.subentry_id}") is None
    )


async def test_power_text_takes_one_value_or_a_sequence(hass: HomeAssistant) -> None:
    from homeassistant.exceptions import ServiceValidationError

    from custom_components.emhass_companion.text import LoadPowerText

    numbers = _numbers(hass, 15)
    load = numbers["nominal_power"].load
    power = LoadPowerText(numbers["nominal_power"].coordinator, load)
    assert power.native_value == "11000"

    await power.async_set_value("1500")
    assert (load.nominal_power_w, load.power_sequence) == (1500, ())

    load.operating_hours = 1
    await power.async_set_value("150, 2000, 0")
    assert load.power_sequence == (150, 2000, 0)
    assert load.nominal_power_w == 2000
    assert power.native_value == "150, 2000, 0"

    with pytest.raises(ServiceValidationError, match="needs at least"):
        await power.async_set_value("1, 2, 3, 4, 5".replace("1,", "100,"))
    with pytest.raises(ServiceValidationError, match="commas"):
        await power.async_set_value("fast")
    assert load.power_sequence == (150, 2000, 0)
    with pytest.raises(ServiceValidationError, match="needs at least"):
        await numbers["operating_hours"].async_set_native_value(0.25)
    assert load.operating_hours == 1
    for key in ("minimum_power", "minimum_on_time", "minimum_off_time", "max_startups"):
        assert not numbers[key].available

    await power.async_set_value("1500")
    assert (load.nominal_power_w, load.power_sequence) == (1500, ())


def _restore_power(hass, last_state, edited_at=None, *, new_api=True):
    """Run the setup-time Power restore against one stored state."""
    from unittest.mock import MagicMock, patch

    from homeassistant.core import State
    from homeassistant.helpers import entity_registry as er

    from custom_components.emhass_companion import _async_restore_power

    numbers = _numbers(hass, 15)
    load = numbers["nominal_power"].load
    coordinator = numbers["nominal_power"].coordinator
    entry = coordinator.config_entry
    registry = er.async_get(hass)
    number = registry.async_get_or_create(
        "number",
        DOMAIN,
        f"{entry.entry_id}_{load.subentry_id}_nominal_power",
        config_entry=entry,
        suggested_object_id="car_power",
    )
    load.power_edited_at = edited_at
    states = {number.entity_id: MagicMock(state=State(number.entity_id, *last_state))}
    if new_api:
        # HA 2026.11 and later.
        stored = MagicMock(spec=["async_get_stored_state"])
        stored.async_get_stored_state.side_effect = states.get
    else:
        stored = MagicMock(spec=["last_states"])
        stored.last_states = states
    with patch("custom_components.emhass_companion.restore_state.async_get", return_value=stored):
        _async_restore_power(hass, entry, coordinator.loads)
    assert registry.async_get(number.entity_id) is None
    return load


async def test_power_number_is_replaced_by_text_and_keeps_its_value(hass: HomeAssistant) -> None:
    """Upgrading drops the old Power number but carries its last value over."""
    assert _restore_power(hass, ("7400",)).nominal_power_w == 7400


@pytest.mark.parametrize("new_api", [True, False])
async def test_power_restore_reads_a_stored_sequence(hass: HomeAssistant, new_api) -> None:
    load = _restore_power(hass, ("300, 1830, 20",), new_api=new_api)
    assert load.power_sequence == (300, 1830, 20)
    assert load.nominal_power_w == 1830


@pytest.mark.parametrize(
    ("edited_at", "expected"),
    [
        (None, 7400),
        ("2026-01-01T00:00:00+00:00", 7400),
        ("2999-01-01T00:00:00+00:00", 11000),
    ],
)
async def test_power_restore_yields_to_a_newer_reconfigure_edit(
    hass: HomeAssistant, edited_at, expected
) -> None:
    """A Power reconfigured after the last stored state is not overwritten."""
    assert _restore_power(hass, ("7400",), edited_at).nominal_power_w == expected


async def test_power_text_rejects_a_string_that_outgrows_the_state_limit(
    hass: HomeAssistant,
) -> None:
    from homeassistant.exceptions import ServiceValidationError

    from custom_components.emhass_companion.deferrable import parse_power

    with pytest.raises(ServiceValidationError, match="too long"):
        parse_power(",".join(["1000"] * 50))


async def test_a_single_value_clears_a_pending_sequence_start(hass: HomeAssistant) -> None:
    from custom_components.emhass_companion.text import LoadPowerText

    numbers = _numbers(hass, 15)
    load = numbers["nominal_power"].load
    power = LoadPowerText(numbers["nominal_power"].coordinator, load)
    load.operating_hours = 1
    await power.async_set_value("300, 1800, 20")
    load.sequence_start_pending = True
    await power.async_set_value("1500")
    assert load.sequence_start_pending is False

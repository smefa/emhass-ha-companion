"""Registry behaviour against a live Home Assistant state machine."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.const import (
    CONF_NOMINAL_POWER,
    CONF_OPERATING_HOURS,
    CONF_POWER_SENSOR,
    CONF_SEMI_CONTINUOUS,
    DOMAIN,
    RECURRENCE_ON_DEMAND,
    SUBENTRY_TYPE_DEFERRABLE,
)
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.models import Plan, PlanRow

POWER_SENSOR = "sensor.dishwasher_power"


def _entry(*loads: dict) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_DEFERRABLE,
                "title": load["title"],
                "unique_id": load["title"],
                "data": load["data"],
            }
            for load in loads
        ],
    )


async def test_registry_builds_loads_from_subentries(hass: HomeAssistant) -> None:
    entry = _entry(
        {
            "title": "Dishwasher",
            "data": {
                CONF_NOMINAL_POWER: 2000,
                CONF_OPERATING_HOURS: 2,
                CONF_SEMI_CONTINUOUS: True,
            },
        }
    )
    entry.add_to_hass(hass)

    registry = DeferrableRegistry(hass, entry)
    registry.sync()

    loads = registry.all()
    assert len(loads) == 1
    assert loads[0].name == "Dishwasher"
    assert loads[0].nominal_power_w == 2000
    assert loads[0].operating_hours == 2


async def test_load_order_is_stable_and_not_dictionary_order(
    hass: HomeAssistant,
) -> None:
    """Order decides which P_deferrable{k} column belongs to which load."""
    entry = _entry(
        {"title": "Zebra", "data": {CONF_NOMINAL_POWER: 1}},
        {"title": "apple", "data": {CONF_NOMINAL_POWER: 2}},
        {"title": "Mango", "data": {CONF_NOMINAL_POWER: 3}},
    )
    entry.add_to_hass(hass)

    registry = DeferrableRegistry(hass, entry)
    registry.sync()

    assert [load.name for load in registry.all()] == ["apple", "Mango", "Zebra"]


async def test_removing_a_subentry_removes_the_load(hass: HomeAssistant) -> None:
    entry = _entry({"title": "Dishwasher", "data": {CONF_NOMINAL_POWER: 2000}})
    entry.add_to_hass(hass)

    registry = DeferrableRegistry(hass, entry)
    registry.sync()
    assert len(registry.all()) == 1

    for subentry_id in list(entry.subentries):
        hass.config_entries.async_remove_subentry(entry, subentry_id)
    registry.sync()

    assert registry.all() == []


async def test_sync_preserves_live_state(hass: HomeAssistant) -> None:
    """Editing a load must not silently reset what the user has adjusted."""
    entry = _entry(
        {"title": "Dishwasher", "data": {CONF_NOMINAL_POWER: 2000, CONF_OPERATING_HOURS: 2}}
    )
    entry.add_to_hass(hass)

    registry = DeferrableRegistry(hass, entry)
    registry.sync()
    load = registry.all()[0]
    load.operating_hours = 5
    load.enabled = False

    registry.sync()

    assert load.operating_hours == 5
    assert load.enabled is False


async def test_power_sensor_drives_the_runtime_counter(hass: HomeAssistant) -> None:
    entry = _entry(
        {
            "title": "Dishwasher",
            "data": {CONF_NOMINAL_POWER: 2000, CONF_POWER_SENSOR: POWER_SENSOR},
        }
    )
    entry.add_to_hass(hass)
    hass.states.async_set(POWER_SENSOR, "0")

    registry = DeferrableRegistry(hass, entry)
    registry.sync()
    registry.async_start()
    load = registry.all()[0]
    assert not load.is_running

    hass.states.async_set(POWER_SENSOR, "1900")
    await hass.async_block_till_done()
    assert load.is_running

    hass.states.async_set(POWER_SENSOR, "0")
    await hass.async_block_till_done()
    assert not load.is_running
    assert load.elapsed_today(dt_util.utcnow()) > timedelta()

    registry.async_stop()


async def test_a_load_already_running_at_startup_is_detected(
    hass: HomeAssistant,
) -> None:
    """Otherwise a restart mid-cycle looks like the load never started."""
    entry = _entry(
        {
            "title": "Dishwasher",
            "data": {CONF_NOMINAL_POWER: 2000, CONF_POWER_SENSOR: POWER_SENSOR},
        }
    )
    entry.add_to_hass(hass)
    hass.states.async_set(POWER_SENSOR, "1900")

    registry = DeferrableRegistry(hass, entry)
    registry.sync()
    registry.async_start()

    assert registry.all()[0].is_running
    registry.async_stop()


async def test_unavailable_power_sensor_does_not_crash(hass: HomeAssistant) -> None:
    entry = _entry(
        {
            "title": "Dishwasher",
            "data": {CONF_NOMINAL_POWER: 2000, CONF_POWER_SENSOR: POWER_SENSOR},
        }
    )
    entry.add_to_hass(hass)
    hass.states.async_set(POWER_SENSOR, "unavailable")

    registry = DeferrableRegistry(hass, entry)
    registry.sync()
    registry.async_start()

    assert not registry.all()[0].is_running
    registry.async_stop()


async def test_a_load_without_a_power_sensor_still_works(hass: HomeAssistant) -> None:
    """The power sensor is optional; everything else must degrade cleanly."""
    entry = _entry({"title": "Dishwasher", "data": {CONF_NOMINAL_POWER: 2000}})
    entry.add_to_hass(hass)

    registry = DeferrableRegistry(hass, entry)
    registry.sync()
    registry.async_start()

    load = registry.all()[0]
    assert load.power_sensor is None
    assert not load.is_running
    assert load.to_load(dt_util.utcnow(), 30).completed_timesteps == 0
    registry.async_stop()


async def test_to_loads_projects_every_load(hass: HomeAssistant) -> None:
    entry = _entry(
        {"title": "Dishwasher", "data": {CONF_NOMINAL_POWER: 2000, CONF_OPERATING_HOURS: 2}},
        {"title": "Car", "data": {CONF_NOMINAL_POWER: 11000, CONF_OPERATING_HOURS: 3}},
    )
    entry.add_to_hass(hass)

    registry = DeferrableRegistry(hass, entry)
    registry.sync()

    projected = registry.to_loads(dt_util.utcnow(), 30)
    assert [load.name for load in projected] == ["Car", "Dishwasher"]
    assert [load.nominal_power_w for load in projected] == [11000, 2000]


# --- on-demand loads -----------------------------------------------------------


async def test_unrequested_on_demand_load_is_excluded_from_to_loads(
    hass: HomeAssistant,
) -> None:
    entry = _entry({"title": "Dishwasher", "data": {CONF_NOMINAL_POWER: 2000}})
    entry.add_to_hass(hass)

    registry = DeferrableRegistry(hass, entry)
    registry.sync()
    registry.all()[0].recurrence = RECURRENCE_ON_DEMAND

    projected = registry.to_loads(dt_util.utcnow(), 30)
    assert projected[0].wants_to_run is False


async def test_a_power_sensor_stopping_auto_disarms_a_fulfilled_request(
    hass: HomeAssistant,
) -> None:
    """The lifecycle a user automation cannot build reliably: only the
    integration knows enough about elapsed runtime to clear the request."""
    entry = _entry(
        {
            "title": "Dishwasher",
            "data": {CONF_NOMINAL_POWER: 2000, CONF_POWER_SENSOR: POWER_SENSOR},
        }
    )
    entry.add_to_hass(hass)
    hass.states.async_set(POWER_SENSOR, "0")

    registry = DeferrableRegistry(hass, entry)
    registry.sync()
    registry.async_start()
    load = registry.all()[0]
    load.recurrence = RECURRENCE_ON_DEMAND
    # A zero target is met by any run at all, however short -- keeps the test
    # from racing real wall-clock time between the two state changes below.
    load.operating_hours = 0
    load.requested = True

    hass.states.async_set(POWER_SENSOR, "1900")
    await hass.async_block_till_done()
    assert load.requested is True  # still running -- nothing to disarm yet

    hass.states.async_set(POWER_SENSOR, "0")
    await hass.async_block_till_done()
    assert load.requested is False

    registry.async_stop()


async def test_a_power_sensor_stopping_early_does_not_disarm(hass: HomeAssistant) -> None:
    entry = _entry(
        {
            "title": "Dishwasher",
            "data": {CONF_NOMINAL_POWER: 2000, CONF_POWER_SENSOR: POWER_SENSOR},
        }
    )
    entry.add_to_hass(hass)
    hass.states.async_set(POWER_SENSOR, "0")

    registry = DeferrableRegistry(hass, entry)
    registry.sync()
    registry.async_start()
    load = registry.all()[0]
    load.recurrence = RECURRENCE_ON_DEMAND
    load.operating_hours = 2
    load.requested = True

    hass.states.async_set(POWER_SENSOR, "1900")
    await hass.async_block_till_done()
    hass.states.async_set(POWER_SENSOR, "0")
    await hass.async_block_till_done()

    assert load.requested is True

    registry.async_stop()


async def test_assume_from_plan_auto_disarms_a_sourceless_load(hass: HomeAssistant) -> None:
    entry = _entry({"title": "Dishwasher", "data": {CONF_NOMINAL_POWER: 2000}})
    entry.add_to_hass(hass)

    registry = DeferrableRegistry(hass, entry)
    registry.sync()
    load = registry.get(next(iter(entry.subentries)))
    load.recurrence = RECURRENCE_ON_DEMAND
    load.operating_hours = 0.5
    load.requested = True

    now = dt_util.utcnow()
    step = timedelta(minutes=15)
    plan = Plan(
        generated_at=now,
        schema_version="1.0",
        rows=[
            PlanRow(timestamp=now, deferrables=(2000.0,)),
            PlanRow(timestamp=now + step, deferrables=(2000.0,)),
        ],
    )

    registry.assume_from_plan(plan, [load.subentry_id], now + timedelta(minutes=30))

    assert load.requested is False

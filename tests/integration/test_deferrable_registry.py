"""Registry behaviour against a live Home Assistant state machine."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.const import (
    CONF_CONTROL_ENTITY,
    CONF_NOMINAL_POWER,
    CONF_OPERATING_HOURS,
    CONF_POWER_SENSOR,
    CONF_SEMI_CONTINUOUS,
    DOMAIN,
    LOAD_MODE_AUTO,
    LOAD_MODE_FORCE_ON,
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


async def test_a_forced_load_that_never_runs_still_disarms(hass: HomeAssistant) -> None:
    """The latch this check was made reachable for.

    A load with a power sensor is skipped by ``assume_from_plan``, and the
    source listener only fires on a running-to-stopped transition -- so a
    forced run on a load that never actually starts (no control entity, a dead
    contactor, a car left unplugged) used to reach neither, and stayed on
    indefinitely with no way back to auto short of restarting Home Assistant.

    Nothing here ever runs: the sensor sits at zero throughout.
    """
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
    load.operating_hours = 0
    load.force_run()
    assert load.mode == LOAD_MODE_FORCE_ON

    registry.check_auto_disarm(dt_util.utcnow())

    assert load.mode == LOAD_MODE_AUTO

    registry.async_stop()


async def test_an_unmet_forced_run_survives_the_disarm_check(hass: HomeAssistant) -> None:
    """The other half: running every cycle must not clear a run still owed its
    hours, or the button would be a no-op instead of a latch."""
    entry = _entry({"title": "Dishwasher", "data": {CONF_NOMINAL_POWER: 2000}})
    entry.add_to_hass(hass)

    registry = DeferrableRegistry(hass, entry)
    registry.sync()
    load = registry.all()[0]
    load.operating_hours = 2
    load.force_run()

    registry.check_auto_disarm(dt_util.utcnow())

    assert load.mode == LOAD_MODE_FORCE_ON


async def test_a_sourceless_load_disarms_on_the_runtime_the_plan_credits_it(
    hass: HomeAssistant,
) -> None:
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

    # The pairing the coordinator uses: the plan advances the accumulator,
    # then the disarm check reads it. Separate calls because disarming is not
    # conditional on any of assume_from_plan's guards -- see
    # DeferrableRegistry.check_auto_disarm.
    registry.assume_from_plan(plan, [load.subentry_id], now + timedelta(minutes=30))
    registry.check_auto_disarm(now + timedelta(minutes=30))

    assert load.requested is False


async def test_a_script_control_entity_is_ignored_entirely(hass: HomeAssistant) -> None:
    """The form accepted scripts once; a script is not switchable.

    Dropping it here covers both uses in one place: the executor has nothing
    to fire on every apply, and `running_source` does not report the load as
    idle merely because its script is not executing right now.
    """
    entry = _entry(
        {
            "title": "Dishwasher",
            "data": {
                CONF_NOMINAL_POWER: 2000,
                CONF_CONTROL_ENTITY: "script.start_dishwasher",
            },
        }
    )
    entry.add_to_hass(hass)

    registry = DeferrableRegistry(hass, entry)
    registry.sync()

    load = registry.all()[0]
    assert load.control_entity is None
    assert load.running_source is None


async def test_a_switch_control_entity_is_kept(hass: HomeAssistant) -> None:
    entry = _entry(
        {
            "title": "Dishwasher",
            "data": {
                CONF_NOMINAL_POWER: 2000,
                CONF_CONTROL_ENTITY: "input_boolean.dishwasher",
            },
        }
    )
    entry.add_to_hass(hass)

    registry = DeferrableRegistry(hass, entry)
    registry.sync()

    assert registry.all()[0].control_entity == "input_boolean.dishwasher"


async def test_a_script_control_entity_raises_a_repair(hass: HomeAssistant) -> None:
    """Silently ignoring it would leave the user with a load nothing switches."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.emhass_companion import _report_unusable_control_entities
    from custom_components.emhass_companion.const import ISSUE_SCRIPT_CONTROL_ENTITY

    entry = _entry(
        {
            "title": "Dishwasher",
            "data": {
                CONF_NOMINAL_POWER: 2000,
                CONF_CONTROL_ENTITY: "script.start_dishwasher",
            },
        }
    )
    entry.add_to_hass(hass)

    _report_unusable_control_entities(hass, entry)

    registry = ir.async_get(hass)
    issue = registry.async_get_issue(DOMAIN, ISSUE_SCRIPT_CONTROL_ENTITY)
    assert issue is not None
    assert "Dishwasher" in issue.translation_placeholders["details"]
    assert "script.start_dishwasher" in issue.translation_placeholders["details"]

    # And clears as soon as the load points at something switchable, without
    # waiting for a restart -- editing a load in place does not reload.
    subentry_id = next(iter(entry.subentries))
    hass.config_entries.async_update_subentry(
        entry,
        entry.subentries[subentry_id],
        data={CONF_NOMINAL_POWER: 2000, CONF_CONTROL_ENTITY: "switch.dishwasher"},
    )
    _report_unusable_control_entities(hass, entry)

    assert registry.async_get_issue(DOMAIN, ISSUE_SCRIPT_CONTROL_ENTITY) is None

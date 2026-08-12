"""The watch that notices a source entity has stopped reporting.

Two failure modes matter more than the happy path, because both of them are
ways this feature makes things worse rather than better: alerting on every
restart (when half the house is legitimately unavailable), and alerting on a
Modbus connection that drops and comes back a minute later. Each gets its own
test here, and the stand-down is checked to be narrow -- the battery is handed
back, the dishwasher is not switched off.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    CONF_INVERTER,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    CONF_PV_ENTITY,
    CONF_SOC_ENTITY,
    DOMAIN,
    ISSUE_SOURCES_BLIND,
    MODE_SELF_CONSUME,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator, EmhassData
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.health import SourceHealth, critical_entities

from .test_executor import (
    INVERTER_OPTIONS,
    MODE_SELECT,
    POWER_NUMBER,
    TEST_INVERTER_KEY,
    _plan,
    _test_inverter_profile,
)

SOC_ENTITY = "sensor.battery_level"
PV_ENTITY = "sensor.pv_power"


def _entry(hass: HomeAssistant, *, inverter: bool = False) -> MockConfigEntry:
    options = {
        "battery": {
            "use_battery": True,
            "capacity_wh": 10000,
            "charge_power_max_w": 5000,
            "discharge_power_max_w": 5000,
        },
        CONF_SOC_ENTITY: SOC_ENTITY,
        CONF_PV_ENTITY: PV_ENTITY,
    }
    if inverter:
        options[CONF_INVERTER] = {
            CONF_PROFILE: TEST_INVERTER_KEY,
            CONF_PROFILE_OPTIONS: INVERTER_OPTIONS,
        }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


def _register(hass: HomeAssistant, entity_id: str, state: str) -> None:
    """Give an entity both a registry entry and a state.

    The watch ignores anything Home Assistant has never heard of, so a test
    fixture that only sets a state would pass for the wrong reason once the
    state is removed.
    """
    domain, _, object_id = entity_id.partition(".")
    er.async_get(hass).async_get_or_create(domain, "test", object_id, suggested_object_id=object_id)
    hass.states.async_set(entity_id, state)


@pytest.fixture
def watch(hass: HomeAssistant):
    """Start a watch that is always stopped again, even on a failing assert.

    Without this a test that fails mid-way leaves its scan timer running and
    pytest reports a lingering-timer error on top of the real failure, which
    buries the thing that actually went wrong.
    """
    started: list[SourceHealth] = []

    def _start(entry: MockConfigEntry, grace: timedelta) -> SourceHealth:
        health = SourceHealth(hass, entry, grace=grace)
        health.async_start()
        started.append(health)
        return health

    yield _start
    for health in started:
        health.async_stop()


async def _advance(hass: HomeAssistant, freezer, delta: timedelta) -> None:
    """Move real time forward and let the scan timer fire.

    Both halves are needed: ``async_fire_time_changed`` alone runs the
    callback but leaves ``dt_util.utcnow()`` where it was, so the grace
    arithmetic inside would never see any time pass.
    """
    freezer.tick(delta)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def test_nothing_is_blind_while_home_assistant_is_starting(
    hass: HomeAssistant, watch, freezer
) -> None:
    """The startup exemption: unavailable during boot is not a fault.

    Without this the watch fires on every single restart, since integrations
    load in an arbitrary order and everything reads unavailable until its own
    has finished.

    Driven by the interval scan rather than a state change on purpose: a
    reading that is *already* missing when the watch starts never produces a
    state change at all, which is exactly the shape a cold start has.
    """
    entry = _entry(hass)
    _register(hass, SOC_ENTITY, "unavailable")
    _register(hass, PV_ENTITY, "unavailable")

    hass.set_state(CoreState.starting)
    health = watch(entry, timedelta(0))

    await _advance(hass, freezer, timedelta(minutes=5))
    assert health.blind == ()

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    await _advance(hass, freezer, timedelta(minutes=10))

    assert set(health.blind) == {PV_ENTITY, SOC_ENTITY}


async def test_a_source_already_missing_at_startup_gets_a_full_grace_period(
    hass: HomeAssistant, watch, freezer
) -> None:
    """The two exemptions compose rather than cancelling out.

    A slow integration is unavailable both during boot *and* for a while
    after. Its grace period has to run from when the watch first got to look
    at it, not from when it stopped reporting -- half an hour of Home
    Assistant starting up must not have eaten the grace period before the
    integration ever had its chance.
    """
    entry = _entry(hass)
    _register(hass, SOC_ENTITY, "unavailable")
    _register(hass, PV_ENTITY, "1200")

    hass.set_state(CoreState.starting)
    health = watch(entry, timedelta(minutes=15))
    await _advance(hass, freezer, timedelta(minutes=30))

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    # First look past the guard: this is where its clock starts, 30 minutes
    # of unavailability notwithstanding.
    await _advance(hass, freezer, timedelta(minutes=1))
    await _advance(hass, freezer, timedelta(minutes=10))
    assert health.blind == ()

    await _advance(hass, freezer, timedelta(minutes=10))
    assert health.blind == (SOC_ENTITY,)


async def test_a_reading_that_comes_back_within_the_grace_period_is_not_a_fault(
    hass: HomeAssistant, watch, freezer
) -> None:
    """A Modbus inverter dropping its connection for a minute is not news."""
    entry = _entry(hass)
    _register(hass, SOC_ENTITY, "57")
    _register(hass, PV_ENTITY, "1200")

    health = watch(entry, timedelta(minutes=15))

    hass.states.async_set(SOC_ENTITY, "unavailable")
    await _advance(hass, freezer, timedelta(minutes=2))
    assert health.blind == ()

    hass.states.async_set(SOC_ENTITY, "57")
    await _advance(hass, freezer, timedelta(minutes=20))
    # And the clock restarted: 20 minutes of being fine cannot make a reading
    # that flapped for two minutes retroactively blind.
    assert health.blind == ()


async def test_a_reading_gone_past_the_grace_period_is_reported_and_then_cleared(
    hass: HomeAssistant, watch, freezer
) -> None:
    entry = _entry(hass)
    _register(hass, SOC_ENTITY, "57")
    _register(hass, PV_ENTITY, "1200")

    changes: list[tuple[str, ...]] = []
    health = watch(entry, timedelta(minutes=15))
    health.add_listener(lambda: changes.append(health.blind))

    hass.states.async_set(SOC_ENTITY, "unavailable")
    await _advance(hass, freezer, timedelta(minutes=10))
    assert health.blind == ()

    await _advance(hass, freezer, timedelta(minutes=10))
    assert health.blind == (SOC_ENTITY,)
    assert health.report["blind_critical_entities"] == [SOC_ENTITY]
    assert health.report["blind_since"] is not None

    hass.states.async_set(SOC_ENTITY, "57")
    await hass.async_block_till_done()
    assert health.blind == ()
    # One notification each way, not one per scan: the listener drives an
    # inverter write, so a set that has not moved must stay quiet.
    assert changes == [(SOC_ENTITY,), ()]


async def test_entity_ids_home_assistant_has_never_heard_of_are_ignored(
    hass: HomeAssistant, watch, freezer
) -> None:
    """A typo in the options is a config error, not a source that failed.

    The reference walker matches anything shaped like an entity id, including
    strings in profile options that were never meant to be one, so this is
    what keeps the watch from reporting them forever.
    """
    entry = _entry(hass)
    _register(hass, SOC_ENTITY, "57")
    # PV_ENTITY deliberately never registered and never given a state.

    health = watch(entry, timedelta(0))
    await _advance(hass, freezer, timedelta(minutes=30))

    assert health.blind == ()


async def test_stopping_the_watch_forgets_everything(hass: HomeAssistant, watch, freezer) -> None:
    """A reload must not inherit the previous run's grace clocks."""
    entry = _entry(hass)
    _register(hass, SOC_ENTITY, "unavailable")

    health = watch(entry, timedelta(0))
    await _advance(hass, freezer, timedelta(minutes=2))
    assert health.blind == (SOC_ENTITY,)

    health.async_stop()
    assert health.blind == ()

    # And the unsubscribed listeners really are gone.
    hass.states.async_set(SOC_ENTITY, "unavailable")
    await hass.async_block_till_done()
    assert health.blind == ()


def test_only_the_readings_control_depends_on_are_critical(hass: HomeAssistant) -> None:
    """Live PV is worth reporting; it is not worth standing control down for."""
    entry = _entry(hass, inverter=True)
    from custom_components.emhass_companion.configuration import EmhassConfig

    critical = critical_entities(EmhassConfig.from_entry(hass, entry))
    assert SOC_ENTITY in critical
    assert MODE_SELECT in critical
    assert POWER_NUMBER in critical
    assert PV_ENTITY not in critical


@pytest.fixture
async def coordinator(hass: HomeAssistant) -> EmhassCoordinator:
    entry = _entry(hass, inverter=True)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)
    coordinator.profiles[TEST_INVERTER_KEY] = _test_inverter_profile()
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())
    return coordinator


async def test_a_blind_critical_reading_raises_an_error_repair(
    hass: HomeAssistant, coordinator: EmhassCoordinator
) -> None:
    _register(hass, SOC_ENTITY, "57")
    _register(hass, MODE_SELECT, "Self Consumption")
    _register(hass, POWER_NUMBER, "0")
    coordinator.health.grace = timedelta(0)
    coordinator.async_start_source_health()

    hass.states.async_set(SOC_ENTITY, "unavailable")
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_SOURCES_BLIND)
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.ERROR
    assert SOC_ENTITY in issue.translation_placeholders["entities"]
    assert coordinator.blind_sources == (SOC_ENTITY,)

    hass.states.async_set(SOC_ENTITY, "57")
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_SOURCES_BLIND) is None
    coordinator.async_stop_source_health()


async def test_a_blind_non_critical_reading_is_only_a_warning(
    hass: HomeAssistant, coordinator: EmhassCoordinator
) -> None:
    """The plan is still followed, so the repair must not claim otherwise."""
    _register(hass, SOC_ENTITY, "57")
    _register(hass, PV_ENTITY, "1200")
    _register(hass, MODE_SELECT, "Self Consumption")
    _register(hass, POWER_NUMBER, "0")
    coordinator.health.grace = timedelta(0)
    coordinator.async_start_source_health()

    hass.states.async_set(PV_ENTITY, "unavailable")
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_SOURCES_BLIND)
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.WARNING
    assert coordinator.blind_sources == ()
    coordinator.async_stop_source_health()


async def test_control_stands_down_but_the_load_schedule_is_left_alone(
    hass: HomeAssistant, coordinator: EmhassCoordinator
) -> None:
    """The narrow stand-down.

    A missing battery reading makes commanding the battery wrong; it does not
    make the plan's dishwasher row dangerous. Switching off every appliance in
    the house would be a far bigger intervention than this fault warrants.
    """
    from custom_components.emhass_companion.executor import Executor

    _register(hass, SOC_ENTITY, "57")
    _register(hass, MODE_SELECT, "Self Consumption")
    _register(hass, POWER_NUMBER, "0")
    executor = Executor(hass, coordinator)

    # Healthy: the plan's -3000 W is followed.
    assert executor._decide().action != MODE_SELF_CONSUME

    coordinator.health.grace = timedelta(0)
    coordinator.async_start_source_health()
    hass.states.async_set(SOC_ENTITY, "unavailable")
    await hass.async_block_till_done()

    decision = executor._decide()
    assert decision.action == MODE_SELF_CONSUME
    assert decision.power_w == 0.0
    assert decision.curtail is False
    assert SOC_ENTITY in decision.reason
    # Still reading the (fresh) plan for the loads rather than falling back.
    assert executor._plan_usable() is True
    coordinator.async_stop_source_health()

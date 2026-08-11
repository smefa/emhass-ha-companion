"""The control switch acts the moment it is flipped, not on the next tick.

It is the stop button. Someone who sees the battery force-charging into the
evening peak and switches control off expects the inverter back *now* -- but
the handover lives in the executor's apply, and applies are driven by the
coordinator, i.e. once per ``time_step_minutes`` (fifteen by default). Setting
``coordinator.control_enabled`` and stopping there means up to a full timestep
of continued charging after the user has already said stop.

Both directions are checked, because switching *on* was equally delayed: a
user who hands over control and then watches nothing happen for a quarter of
an hour has no way to tell the integration from a broken one.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.const import EVENT_CALL_SERVICE, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    CONF_INVERTER,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    DOMAIN,
)
from custom_components.emhass_companion.coordinator import EmhassData

from .test_executor import (
    INVERTER_OPTIONS,
    MODE_SELECT,
    POWER_NUMBER,
    TEST_INVERTER_KEY,
    _plan,
    _test_inverter_profile,
)


@pytest.fixture
def calls(hass: HomeAssistant) -> list[Event]:
    """Every service call, in order.

    Captured off the bus rather than by registering stub handlers: setting the
    entry up for real pulls in the actual ``select`` and ``number``
    integrations, whose entity services would replace any stub and then drop
    the call as targeting an unknown entity.
    """
    return async_capture_events(hass, EVENT_CALL_SERVICE)


def _options(calls: list[Event], domain: str, service: str) -> list[Any]:
    """The ``option``/``value`` of each matching call, in order."""
    return [
        event.data["service_data"].get("option")
        for event in calls
        if event.data["domain"] == domain and event.data["service"] == service
    ]


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    """A fully set-up entry: real entities, real executor, an inverter profile."""
    options: dict[str, Any] = {
        "battery": {
            "use_battery": True,
            "capacity_wh": 10000,
            "charge_power_max_w": 5000,
            "discharge_power_max_w": 5000,
        },
        CONF_INVERTER: {
            CONF_PROFILE: TEST_INVERTER_KEY,
            CONF_PROFILE_OPTIONS: INVERTER_OPTIONS,
        },
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"}, options=options)
    entry.add_to_hass(hass)

    hass.states.async_set(MODE_SELECT, "Self Consumption")
    hass.states.async_set(POWER_NUMBER, "0")

    with patch("custom_components.emhass_companion.EmhassClient") as client_cls:
        client_cls.return_value = AsyncMock(spec=EmhassClient)
        client_cls.return_value.async_get_version = AsyncMock(return_value="0.17.9")
        client_cls.return_value.base_url = "http://localhost:5000"
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    coordinator.profiles[TEST_INVERTER_KEY] = _test_inverter_profile()
    # A plan that wants a hard 3 kW charge, so the difference between acting
    # and not acting is visible in the recorded calls.
    coordinator.data = EmhassData(plan=_plan(-3000), last_success=dt_util.utcnow())
    return entry


def _control_switch(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """The control switch's entity id, by unique id rather than by name."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_control_enabled")
    assert entity_id is not None
    return entity_id


async def _flip(hass: HomeAssistant, entity_id: str, service: str) -> None:
    await hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)
    # No clock advance anywhere: `async_block_till_done` only drains work that
    # is already scheduled, which is the whole point.
    await hass.async_block_till_done()


async def test_turning_control_on_applies_without_waiting_for_a_tick(
    hass: HomeAssistant, calls: list[Event]
) -> None:
    entry = await _setup(hass)
    switch = _control_switch(hass, entry)
    calls.clear()

    await _flip(hass, switch, SERVICE_TURN_ON)

    assert _options(calls, "select", "select_option") == ["Forced Charge"]


async def test_turning_control_off_hands_the_inverter_back_immediately(
    hass: HomeAssistant, calls: list[Event]
) -> None:
    """The failure this exists for: a forced charge outliving the stop button."""
    entry = await _setup(hass)
    switch = _control_switch(hass, entry)

    await _flip(hass, switch, SERVICE_TURN_ON)
    assert _options(calls, "select", "select_option") == ["Forced Charge"]
    calls.clear()

    await _flip(hass, switch, SERVICE_TURN_OFF)

    # The test profile defines no explicit `restore`, so self-consumption is
    # the handover -- what matters is that it arrives on the same event loop
    # pass as the switch, with no time having passed.
    assert _options(calls, "select", "select_option") == ["Self Consumption"]


async def test_the_gate_is_still_respected_after_the_handover(
    hass: HomeAssistant, calls: list[Event]
) -> None:
    """Going through `async_apply` must not become a way to act while off."""
    entry = await _setup(hass)
    switch = _control_switch(hass, entry)

    await _flip(hass, switch, SERVICE_TURN_ON)
    await _flip(hass, switch, SERVICE_TURN_OFF)
    calls.clear()

    # A second apply with the gate still off: the handover already happened,
    # so this one must write nothing at all.
    await entry.runtime_data.executor.async_apply()
    await hass.async_block_till_done()

    assert _options(calls, "select", "select_option") == []

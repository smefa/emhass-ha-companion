"""The published battery action is the one the inverter was actually given.

The action sensor renders ``executor.last_decision``, but it is a
``CoordinatorEntity`` -- it only writes state when the coordinator notifies
its listeners. The apply is *started* from one of those listeners, so the
sensor publishes before the new decision exists, and then nothing writes it
again until the next plan run or clock tick.

Observed on a live install: the executor sent the inverter a stop at 12:15:09,
and ``sensor.battery_action`` went on reading ``force_charge`` until the clock
ticked at 12:24:30. Nine minutes of a card saying the battery was charging
while it sat at zero -- and, at the top of the quarter hour, nine minutes of
the reverse, ``idle`` published over a battery pulling 2.7 kW.

A whole timestep of that is the difference between a dashboard someone trusts
during a handover and one they learn to ignore.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.const import DOMAIN
from custom_components.emhass_companion.coordinator import EmhassData

from .test_control_switch_immediacy import _setup
from .test_executor import _plan


def _action_sensor(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_battery_action")
    assert entity_id is not None
    return entity_id


async def _publish(hass: HomeAssistant, entry: MockConfigEntry, p_batt: float) -> None:
    """Hand the coordinator a new plan the way a finished run does."""
    coordinator = entry.runtime_data.coordinator
    coordinator.data = EmhassData(plan=_plan(p_batt), last_success=dt_util.utcnow())
    coordinator.async_update_listeners()
    # No clock advance: `async_block_till_done` drains the apply that the
    # notification just scheduled, and nothing else.
    await hass.async_block_till_done()


async def test_the_action_sensor_catches_up_within_the_same_update(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass)
    sensor = _action_sensor(hass, entry)

    await _publish(hass, entry, -3000)

    assert hass.states.get(sensor).state == "force_charge"


async def test_a_stopped_battery_is_not_still_published_as_charging(
    hass: HomeAssistant,
) -> None:
    """The failure this exists for."""
    entry = await _setup(hass)
    sensor = _action_sensor(hass, entry)

    await _publish(hass, entry, -3000)
    assert hass.states.get(sensor).state == "force_charge"

    await _publish(hass, entry, 0)

    assert hass.states.get(sensor).state == "idle"


async def test_the_attributes_travel_with_the_state(hass: HomeAssistant) -> None:
    """A fresh action over stale reasoning would be its own kind of wrong."""
    entry = await _setup(hass)
    sensor = _action_sensor(hass, entry)

    await _publish(hass, entry, -3000)
    await _publish(hass, entry, 0)

    assert hass.states.get(sensor).attributes["power_w"] == 0
    assert "0 W" in hass.states.get(sensor).attributes["reason"]

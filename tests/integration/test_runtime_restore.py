"""``LoadRuntimeTodaySensor`` restoring only within the same local day.

The counter is fed back to EMHASS as ``def_current_operating_timesteps`` so a
load is not re-scheduled for work already done. Carrying it across midnight
therefore does not merely display a wrong number -- it tells EMHASS the load
has already run its hours, and the load is under-scheduled for the whole of
the following day.

The guard used to read ``hass.states.get(self.entity_id)`` for the timestamp,
which at ``async_added_to_hass`` time is either nothing at all or the
placeholder the entity registry writes for a not-yet-added entity. Its
``last_updated`` is this boot, so the check compared today against today and
never once fired.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    CONF_NOMINAL_POWER,
    DOMAIN,
    SUBENTRY_TYPE_DEFERRABLE,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.sensor import LoadRuntimeTodaySensor

ENTITY_ID = "sensor.dishwasher_ran_today"


async def _sensor(hass: HomeAssistant, *, hours: float, when) -> LoadRuntimeTodaySensor:
    """A runtime sensor whose stored state was last written at ``when``."""
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(ENTITY_ID, str(hours), last_updated=when),
                {"native_value": hours, "native_unit_of_measurement": "h"},
            ),
        ),
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_DEFERRABLE,
                "title": "Dishwasher",
                "unique_id": "dishwasher",
                "data": {CONF_NOMINAL_POWER: 2000},
            }
        ],
    )
    entry.add_to_hass(hass)

    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)
    subentry_id = next(iter(entry.subentries))

    sensor = LoadRuntimeTodaySensor(coordinator, loads.get(subentry_id))
    sensor.hass = hass
    sensor.entity_id = ENTITY_ID
    await sensor.async_added_to_hass()
    return sensor


async def test_todays_hours_are_restored(hass: HomeAssistant) -> None:
    """A restart mid-afternoon must not forget what already ran this morning."""
    sensor = await _sensor(hass, hours=1.5, when=dt_util.utcnow() - timedelta(minutes=20))

    assert sensor.load.runtime_today == timedelta(hours=1.5)


async def test_yesterdays_hours_are_not_carried_into_today(hass: HomeAssistant) -> None:
    """The bug: the guard read a boot-time timestamp and so never fired."""
    yesterday = dt_util.utcnow() - timedelta(days=1)
    # The placeholder state the old code read instead of the stored one --
    # written by the entity registry for an entity that has not been added
    # yet, and stamped with this boot rather than with when the value was
    # last true.
    hass.states.async_set(ENTITY_ID, "unavailable", {"restored": True})

    sensor = await _sensor(hass, hours=2.5, when=yesterday)

    assert sensor.load.runtime_today == timedelta(0)

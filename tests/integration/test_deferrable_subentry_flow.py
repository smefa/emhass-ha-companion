"""The add/edit forms for a deferrable load, driven end to end.

There was no coverage of these flows at all, and the cost was a form that could
not be submitted with any of its optional fields left blank: an empty ``default``
on a Time or Entity selector is validated by the selector before the step ever
runs, so "no time" and "no sensor" both failed with an error the user could do
nothing about.
"""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.const import (
    CONF_CONTROL_ENTITY,
    CONF_EARLIEST_START,
    CONF_LATEST_END,
    CONF_MAX_STARTUPS,
    CONF_MINIMUM_POWER,
    CONF_NAME,
    CONF_NOMINAL_POWER,
    CONF_OPERATING_HOURS,
    CONF_POWER_SENSOR,
    CONF_SEMI_CONTINUOUS,
    CONF_SINGLE_CONSTANT,
    CONF_STARTUP_PENALTY,
    CONF_TIME_STEP,
    DOMAIN,
    SUBENTRY_TYPE_DEFERRABLE,
)


def _entry(*, subentries: list[ConfigSubentryData] | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        options={CONF_TIME_STEP: 15},
        subentries_data=subentries or [],
    )


async def _start_add(hass: HomeAssistant, entry: MockConfigEntry):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_DEFERRABLE), context={"source": SOURCE_USER}
    )


async def test_a_load_can_be_added_with_every_optional_field_blank(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    result = await _start_add(hass, entry)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Dishwasher",
            CONF_NOMINAL_POWER: 2000,
            CONF_OPERATING_HOURS: 2,
            CONF_SEMI_CONTINUOUS: True,
            CONF_SINGLE_CONSTANT: False,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Dishwasher"
    assert CONF_EARLIEST_START not in result["data"]
    assert CONF_POWER_SENSOR not in result["data"]


async def test_a_binary_sensor_is_accepted_as_the_running_sensor(hass: HomeAssistant) -> None:
    """Not every load has a meter -- deferrable.state_to_power already reads
    on/off states (it's how the control_entity fallback works), so the picker
    should not turn away a plain binary_sensor."""
    hass.states.async_set("binary_sensor.dishwasher_running", "off")
    entry = _entry()
    entry.add_to_hass(hass)

    result = await _start_add(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Dishwasher",
            CONF_NOMINAL_POWER: 2000,
            CONF_OPERATING_HOURS: 2,
            CONF_SEMI_CONTINUOUS: True,
            CONF_SINGLE_CONSTANT: False,
            CONF_POWER_SENSOR: "binary_sensor.dishwasher_running",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_POWER_SENSOR] == "binary_sensor.dishwasher_running"


async def test_the_form_states_the_optimisation_timestep(hass: HomeAssistant) -> None:
    """The timestep is the granularity of every answer EMHASS can give, and it
    is configured two menus away from this form."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await _start_add(hass, entry)
    assert result["description_placeholders"]["time_step"] == "15"


async def test_an_empty_name_is_a_field_error_not_a_crash(hass: HomeAssistant) -> None:
    """An empty name passes TextSelector, then gets stripped as an empty
    optional value -- and the title lookup used to raise KeyError, which the UI
    can only show as "unknown error". A schema failure is attributed to the
    field instead."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await _start_add(hass, entry)
    with pytest.raises(InvalidData) as raised:
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                CONF_NAME: "",
                CONF_NOMINAL_POWER: 2000,
                CONF_OPERATING_HOURS: 2,
                CONF_SEMI_CONTINUOUS: True,
                CONF_SINGLE_CONSTANT: False,
            },
        )

    assert list(raised.value.path) == [CONF_NAME]


async def test_reconfigure_offers_only_what_the_subentry_still_owns(
    hass: HomeAssistant,
) -> None:
    """Powers, hours, the window and the load model became entities the moment
    the load was created; offering them here would show an edit box whose
    contents the restored entity immediately overrides."""
    entry = _entry(
        subentries=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_DEFERRABLE,
                title="Car",
                unique_id="car",
                data={CONF_NAME: "Car", CONF_NOMINAL_POWER: 11000, CONF_OPERATING_HOURS: 3},
            )
        ]
    )
    entry.add_to_hass(hass)
    subentry_id = next(iter(entry.subentries))

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_DEFERRABLE),
        context={"source": "reconfigure", "subentry_id": subentry_id},
    )

    assert result["type"] is FlowResultType.FORM
    fields = {str(marker.schema) for marker in result["data_schema"].schema}
    assert fields == {CONF_NAME, CONF_POWER_SENSOR, CONF_CONTROL_ENTITY}


async def test_reconfigure_keeps_the_values_it_no_longer_shows(
    hass: HomeAssistant,
) -> None:
    """Those values are what a load starts from after a restart, so dropping
    them here would silently reset the load to the defaults on its next reload."""
    entry = _entry(
        subentries=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_DEFERRABLE,
                title="Car",
                unique_id="car",
                data={
                    CONF_NAME: "Car",
                    CONF_NOMINAL_POWER: 11000,
                    CONF_MINIMUM_POWER: 1380,
                    CONF_OPERATING_HOURS: 3,
                    CONF_SEMI_CONTINUOUS: False,
                    CONF_SINGLE_CONSTANT: False,
                    CONF_STARTUP_PENALTY: 0.5,
                    CONF_MAX_STARTUPS: 2,
                    CONF_LATEST_END: "06:00:00",
                },
            )
        ]
    )
    entry.add_to_hass(hass)
    subentry_id = next(iter(entry.subentries))

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_DEFERRABLE),
        context={"source": "reconfigure", "subentry_id": subentry_id},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: "Tesla"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    data = entry.subentries[subentry_id].data
    assert entry.subentries[subentry_id].title == "Tesla"
    assert data[CONF_NOMINAL_POWER] == 11000
    assert data[CONF_MINIMUM_POWER] == 1380
    assert data[CONF_MAX_STARTUPS] == 2
    assert data[CONF_LATEST_END] == "06:00:00"
    assert data[CONF_SEMI_CONTINUOUS] is False


async def test_reconfigure_can_clear_the_power_sensor(hass: HomeAssistant) -> None:
    """A blank entity field arrives as an absent key, which has to mean "unset"
    rather than "leave whatever was there"."""
    entry = _entry(
        subentries=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_DEFERRABLE,
                title="Car",
                unique_id="car",
                data={
                    CONF_NAME: "Car",
                    CONF_NOMINAL_POWER: 11000,
                    CONF_POWER_SENSOR: "sensor.car_power",
                },
            )
        ]
    )
    entry.add_to_hass(hass)
    subentry_id = next(iter(entry.subentries))

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_DEFERRABLE),
        context={"source": "reconfigure", "subentry_id": subentry_id},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: "Car"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert CONF_POWER_SENSOR not in entry.subentries[subentry_id].data

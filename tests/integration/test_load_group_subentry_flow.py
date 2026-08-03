"""The add/edit form for a load group, driven end to end.

A group references existing deferrable/thermal loads rather than being a load
itself, so it is its own subentry type with its own flow -- this covers the
two validation rules (at least two loads, a power budget unless mutually
exclusive) and that reconfigure round-trips correctly.
"""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.const import (
    CONF_GROUP_LOAD_IDS,
    CONF_GROUP_MAX_POWER,
    CONF_GROUP_MUTUAL_EXCLUSION,
    CONF_NAME,
    CONF_NOMINAL_POWER,
    CONF_OPERATING_HOURS,
    DOMAIN,
    SUBENTRY_TYPE_DEFERRABLE,
    SUBENTRY_TYPE_LOAD_GROUP,
)


def _entry(*, extra_subentries: list[ConfigSubentryData] | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[
            # Explicit subentry_id so the two loads' ids are known ahead of
            # time -- needed to seed a group subentry that references them in
            # the same construction (see test_reconfigure_updates_the_group).
            ConfigSubentryData(
                subentry_id="car",
                subentry_type=SUBENTRY_TYPE_DEFERRABLE,
                title="Car",
                unique_id="car",
                data={CONF_NAME: "Car", CONF_NOMINAL_POWER: 11000, CONF_OPERATING_HOURS: 3},
            ),
            ConfigSubentryData(
                subentry_id="heater",
                subentry_type=SUBENTRY_TYPE_DEFERRABLE,
                title="Immersion heater",
                unique_id="heater",
                data={CONF_NAME: "Immersion heater", CONF_NOMINAL_POWER: 3000},
            ),
            *(extra_subentries or []),
        ],
    )


async def _start_add(hass: HomeAssistant, entry: MockConfigEntry):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_LOAD_GROUP), context={"source": SOURCE_USER}
    )


def _load_ids(entry: MockConfigEntry) -> list[str]:
    return [
        subentry_id
        for subentry_id, subentry in entry.subentries.items()
        if subentry.subentry_type == SUBENTRY_TYPE_DEFERRABLE
    ]


async def test_a_group_can_be_added_with_two_loads(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    car, heater = _load_ids(entry)

    result = await _start_add(hass, entry)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Fuse box",
            CONF_GROUP_LOAD_IDS: [car, heater],
            CONF_GROUP_MUTUAL_EXCLUSION: False,
            CONF_GROUP_MAX_POWER: 3500,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Fuse box"
    assert set(result["data"][CONF_GROUP_LOAD_IDS]) == {car, heater}
    assert result["data"][CONF_GROUP_MAX_POWER] == 3500


async def test_fewer_than_two_loads_is_a_field_error(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    (car, _heater) = _load_ids(entry)

    result = await _start_add(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Fuse box",
            CONF_GROUP_LOAD_IDS: [car],
            CONF_GROUP_MUTUAL_EXCLUSION: False,
            CONF_GROUP_MAX_POWER: 3500,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_GROUP_LOAD_IDS: "too_few_loads"}


async def test_no_max_power_without_mutual_exclusion_is_a_field_error(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    car, heater = _load_ids(entry)

    result = await _start_add(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Fuse box",
            CONF_GROUP_LOAD_IDS: [car, heater],
            CONF_GROUP_MUTUAL_EXCLUSION: False,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_GROUP_MAX_POWER: "max_power_required"}


async def test_mutual_exclusion_does_not_need_a_max_power(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    car, heater = _load_ids(entry)

    result = await _start_add(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Fuse box",
            CONF_GROUP_LOAD_IDS: [car, heater],
            CONF_GROUP_MUTUAL_EXCLUSION: True,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_GROUP_MAX_POWER not in result["data"]


async def test_reconfigure_updates_the_group(hass: HomeAssistant) -> None:
    entry = _entry(
        extra_subentries=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_LOAD_GROUP,
                title="Fuse box",
                unique_id="fuse-box",
                data={
                    CONF_NAME: "Fuse box",
                    CONF_GROUP_LOAD_IDS: ["car", "heater"],
                    CONF_GROUP_MUTUAL_EXCLUSION: False,
                    CONF_GROUP_MAX_POWER: 3500,
                },
            )
        ]
    )
    entry.add_to_hass(hass)
    group_id = next(
        subentry_id
        for subentry_id, subentry in entry.subentries.items()
        if subentry.subentry_type == SUBENTRY_TYPE_LOAD_GROUP
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_LOAD_GROUP),
        context={"source": "reconfigure", "subentry_id": group_id},
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Fuse box",
            CONF_GROUP_LOAD_IDS: ["car", "heater"],
            CONF_GROUP_MUTUAL_EXCLUSION: False,
            CONF_GROUP_MAX_POWER: 5000,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert entry.subentries[group_id].data[CONF_GROUP_MAX_POWER] == 5000

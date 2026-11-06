"""Creation and identity-only editing of immersion tanks and legacy rooms."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.const import DOMAIN, SUBENTRY_TYPE_THERMAL


def _tank_data():
    return {
        "temperature_sensor": "sensor.tank_temperature",
        "tank_volume_litres": 200,
        "nominal_power_w": 2000,
        "standby_loss_w": 50,
        "setback_temperature": 37,
        "comfort_temperature": 45,
        "max_temperature": 52,
        "hot_water_period_count": 2,
    }


def _creation_data(advanced=None, **overrides):
    """The first-setup form: standby loss, COP and penalty sit in the Advanced section."""
    data = {**_tank_data(), **overrides}
    standby = data.pop("standby_loss_w")
    return {**data, "advanced": {"standby_loss_w": standby, **(advanced or {})}}


def _period_data(**overrides):
    return {
        "enabled": True,
        "name": "Morning",
        "draw_start": "06:00:00",
        "draw_end": "08:00:00",
        "draw_energy_kwh": 3,
        **overrides,
    }


async def _start(hass, *, data=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[]
        if data is None
        else [
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_THERMAL,
                title="Tank",
                unique_id="tank",
                data=data,
            )
        ],
    )
    entry.add_to_hass(hass)
    context = (
        {"source": SOURCE_USER}
        if data is None
        else {
            "source": "reconfigure",
            "subentry_id": next(iter(entry.subentries)),
        }
    )
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_THERMAL),
        context=context,
    )
    return entry, result


@pytest.mark.parametrize("energy", [0, 3])
async def test_tank_creation_is_settings_then_each_period(hass: HomeAssistant, energy):
    entry, result = await _start(hass)
    assert {str(key) for key in result["data_schema"].schema} == {"name", "thermal_model"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Hot water", "thermal_model": "water_tank"},
    )
    assert result["step_id"] == "water_tank"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _creation_data()
    )
    assert result["step_id"] == "water_tank_period"
    assert result["description_placeholders"] == {"period": "1", "count": "2"}
    assert result["last_step"] is False
    assert not entry.subentries
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        _period_data(draw_energy_kwh=energy),
    )
    assert result["description_placeholders"] == {"period": "2", "count": "2"}
    assert result["last_step"] is True
    assert not entry.subentries
    evening = _period_data(
        name="Evening", draw_start="18:00:00", draw_end="20:00:00", draw_energy_kwh=2
    )
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], evening)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["thermal_model"] == "water_tank"
    assert data["name"] == "Hot water"
    assert data["hot_water_periods"] == [_period_data(draw_energy_kwh=energy), evening]
    assert "hot_water_period_count" not in data
    assert "advanced" not in data
    assert (data["heater_cop"], data["comfort_penalty"], data["standby_loss_w"]) == (1.0, 0.0, 50)
    assert (
        not {"draw_start", "draw_end", "draw_energy_kwh", "comfort_start", "comfort_end"}
        & data.keys()
    )
    assert (data["setback_temperature"], data["comfort_temperature"], data["max_temperature"]) == (
        37,
        45,
        52,
    )


async def test_advanced_section_values_are_stored_flat(hass: HomeAssistant):
    _entry, result = await _start(hass)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Tank", "thermal_model": "water_tank"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        _creation_data(
            {"heater_cop": 3.0, "comfort_penalty": 10, "standby_loss_w": 80},
            hot_water_period_count=1,
        ),
    )
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], _period_data())
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (result["data"]["heater_cop"], result["data"]["comfort_penalty"]) == (3.0, 10)
    assert result["data"]["standby_loss_w"] == 80
    assert "advanced" not in result["data"]


@pytest.mark.parametrize(
    "changes",
    [
        {"setback_temperature": 46},
        {"max_temperature": 44},
        {"tank_volume_litres": float("nan")},
        {"temperature_sensor": ""},
        {"hot_water_period_count": 0},
        {"hot_water_period_count": 9},
        {"hot_water_period_count": 1.5},
        {"hot_water_period_count": True},
    ],
)
async def test_invalid_tank_values_do_not_create_subentry(hass: HomeAssistant, changes):
    entry, result = await _start(hass)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Tank", "thermal_model": "water_tank"},
    )
    if changes.keys() & {"setback_temperature", "max_temperature"}:
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], _creation_data(**changes)
        )
        assert result["errors"]["base"] == "invalid_tank_band"
    else:
        with pytest.raises(InvalidData):
            await hass.config_entries.subentries.async_configure(
                result["flow_id"], _creation_data(**changes)
            )
    assert not entry.subentries


@pytest.mark.parametrize("standby", [-1, 1001, float("nan")])
async def test_invalid_standby_loss_in_advanced_does_not_create_subentry(
    hass: HomeAssistant, standby
):
    entry, result = await _start(hass)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Tank", "thermal_model": "water_tank"}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"], _creation_data({"standby_loss_w": standby})
        )
    assert not entry.subentries


async def test_bad_period_stays_on_same_page_and_does_not_save(hass: HomeAssistant):
    entry, result = await _start(hass)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Tank", "thermal_model": "water_tank"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _creation_data()
    )
    for change in (
        {"draw_energy_kwh": float("inf")},
        {"draw_energy_kwh": -1},
        {"draw_start": "invalid"},
    ):
        with pytest.raises(InvalidData):
            await hass.config_entries.subentries.async_configure(
                result["flow_id"], _period_data(**change)
            )
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], _period_data())
    assert result["description_placeholders"]["period"] == "2"
    assert not entry.subentries


@pytest.mark.parametrize("count", [1, 2, 3])
async def test_reconfigure_period_count_preserves_defaults_and_commits_at_end(hass, count):
    original = [
        _period_data(),
        _period_data(name="Evening", draw_start="18:00:00", draw_end="20:00:00"),
    ]
    stored = {
        **_tank_data(),
        "name": "Tank",
        "thermal_model": "water_tank",
        "hot_water_periods": original,
    }
    stored.pop("hot_water_period_count")
    entry, result = await _start(hass, data=stored)
    assert result["step_id"] == "reconfigure_water_tank"
    assert (
        result["data_schema"](
            {
                "name": "Tank",
                "temperature_sensor": "sensor.tank_temperature",
                "tank_volume_litres": 200,
            }
        )["hot_water_period_count"]
        == 2
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Renamed tank",
            "temperature_sensor": "sensor.new_temperature",
            "tank_volume_litres": 250,
            "hot_water_period_count": count,
        },
    )
    for index in range(count):
        assert next(iter(entry.subentries.values())).data == stored
        schema = result["data_schema"]
        period = (
            schema({})
            if index < 2
            else _period_data(name="Lunch", draw_start="12:00:00", draw_end="13:00:00")
        )
        if index < 2:
            assert period == original[index]
        result = await hass.config_entries.subentries.async_configure(result["flow_id"], period)
    assert result["type"] is FlowResultType.ABORT
    updated = next(iter(entry.subentries.values())).data
    assert updated["name"] == "Renamed tank"
    assert updated["tank_volume_litres"] == 250
    assert len(updated["hot_water_periods"]) == count
    assert updated["hot_water_periods"][: min(count, 2)] == original[: min(count, 2)]
    for field in (
        "standby_loss_w",
        "nominal_power_w",
        "setback_temperature",
        "comfort_temperature",
        "max_temperature",
    ):
        assert updated[field] == stored[field]


async def test_legacy_tank_opens_as_one_period_and_migrates_on_save(hass):
    stored = {
        **_tank_data(),
        **_period_data(),
        "name": "Tank",
        "thermal_model": "water_tank",
        "comfort_start": "05:00:00",
        "comfort_end": "22:00:00",
        "power_sensor": "sensor.old_power",
        "control_entity": "switch.old_heater",
    }
    stored.pop("hot_water_period_count")
    entry, result = await _start(hass, data=stored)
    basics = {
        "name": "Tank",
        "temperature_sensor": "sensor.tank_temperature",
        "tank_volume_litres": 200,
    }
    assert result["data_schema"](basics)["hot_water_period_count"] == 1
    assert next(iter(entry.subentries.values())).data == stored
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], basics)
    period = result["data_schema"]({})
    assert period["draw_start"] == "06:00:00"
    assert period["draw_end"] == "08:00:00"
    assert period["draw_energy_kwh"] == 3
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], period)
    assert result["type"] is FlowResultType.ABORT
    updated = next(iter(entry.subentries.values())).data
    assert updated["hot_water_periods"] == [
        {key: value for key, value in _period_data().items() if key != "name"}
    ]
    assert not {"power_sensor", "control_entity", "draw_start", "comfort_start"} & updated.keys()


async def test_reconfigure_cannot_convert_tank_model(hass: HomeAssistant):
    entry, result = await _start(
        hass, data={**_tank_data(), "name": "Tank", "thermal_model": "water_tank"}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                "name": "Tank",
                "temperature_sensor": "sensor.tank_temperature",
                "tank_volume_litres": 200,
                "thermal_model": "room",
            },
        )
    assert next(iter(entry.subentries.values())).data["thermal_model"] == "water_tank"


async def test_kind_step_cannot_override_validated_tank_settings(hass: HomeAssistant):
    _, result = await _start(hass)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Tank",
            "thermal_model": "water_tank",
            "tank_volume_litres": float("nan"),
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _creation_data(hot_water_period_count=1)
    )
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], _period_data())
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["tank_volume_litres"] == 200


@pytest.mark.parametrize("count", [1, 8])
async def test_minimum_and_maximum_period_count_complete_all_pages(hass, count):
    _, result = await _start(hass)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Tank", "thermal_model": "water_tank"},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        _creation_data(hot_water_period_count=count),
    )
    for index in range(count):
        assert result["description_placeholders"] == {"period": str(index + 1), "count": str(count)}
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], _period_data()
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(result["data"]["hot_water_periods"]) == count


async def test_new_room_uses_room_settings(hass: HomeAssistant):
    _, result = await _start(hass)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Room", "thermal_model": "room"},
    )
    assert result["step_id"] == "settings"
    assert "sense" in {str(key) for key in result["data_schema"].schema}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        result["data_schema"]({"advanced": {}}),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["thermal_model"] == "room"


async def test_legacy_room_retains_no_discriminator_even_with_tank_temperatures(
    hass: HomeAssistant,
):
    stored = {
        "name": "Legacy room",
        "sense": "heat",
        "comfort_temperature": 45,
        "setback_temperature": 37,
        "max_temperature": 52,
    }
    entry, result = await _start(hass, data=stored)
    assert result["step_id"] == "reconfigure"
    assert "thermal_model" not in {str(key) for key in result["data_schema"].schema}
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {"name": "Legacy room", "sense": "cool", "thermal_model": "water_tank"},
        )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Legacy room", "sense": "cool"},
    )
    assert result["type"] is FlowResultType.ABORT
    updated = next(iter(entry.subentries.values())).data
    assert "thermal_model" not in updated
    assert updated["comfort_temperature"] == 45
    assert updated["sense"] == "cool"

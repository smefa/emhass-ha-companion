"""Entity categories decide what Home Assistant's Overview shows for a device.

Overview's device and area pages list only entities with no category, so a
setting or a derived helper left uncategorized crowds out the entities a person
actually looks at. The cards must not depend on the category: they find entities
by registry translation_key.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.const import EntityCategory

from custom_components.emhass_companion import binary_sensor, number, sensor, switch

COMPONENT = Path(__file__).parent.parent / "custom_components" / "emhass_companion"


def _category(cls) -> EntityCategory | None:
    """What an instance reports, without building one.

    Home Assistant's metaclass swaps a class-level `_attr_entity_category` for a
    property, so the attribute cannot be read off the class itself.
    """
    return object.__new__(cls).entity_category


def _described(descriptions, key: str):
    return next(d for d in descriptions if d.translation_key == key)


def test_secondary_sensors_are_diagnostic() -> None:
    for description in (
        sensor.CURTAILMENT_SENSOR,
        sensor.HYBRID_INVERTER_SENSOR,
        *sensor.FORECAST_SENSORS,
        _described(sensor.SAVINGS_SENSORS, "solar_savings_today"),
        _described(sensor.SAVINGS_SENSORS, "battery_savings_today"),
    ):
        assert description.entity_category is EntityCategory.DIAGNOSTIC, description.key

    for cls in (
        sensor.NetHouseLoadSensor,
        sensor.SolarSurplusEnergySensor,
        sensor.SolarSurplusStartSensor,
        sensor.SolarSurplusEndSensor,
        sensor.LoadScheduledPowerSensor,
        sensor.LoadPlannedTemperatureSensor,
        sensor.LoadRuntimeTodaySensor,
        binary_sensor.PhaseGuardActiveBinarySensor,
    ):
        assert _category(cls) is EntityCategory.DIAGNOSTIC, cls.__name__


def test_settings_are_config() -> None:
    for key in ("comfort_temperature", "setback_temperature"):
        assert _described(number.LOAD_NUMBERS, key).entity_category is EntityCategory.CONFIG
    assert _described(switch.LOAD_SWITCHES, "start_asap").entity_category is EntityCategory.CONFIG


def test_the_primary_entities_stay_uncategorized() -> None:
    """What a person reads or acts on day to day is what Overview should list."""
    for description in (
        *sensor.POWER_SENSORS,
        *sensor.BATTERY_SENSORS,
        *sensor.PRICE_SENSORS,
        _described(sensor.SAVINGS_SENSORS, "energy_cost_today"),
        _described(sensor.SAVINGS_SENSORS, "savings_today"),
        _described(switch.LOAD_SWITCHES, "load_requested"),
    ):
        assert description.entity_category is None, description.key

    for cls in (
        sensor.EmhassDecisionSensor,
        sensor.SolarSurplusSensor,
        sensor.LoadNextStartSensor,
        binary_sensor.LoadShouldRunBinarySensor,
        binary_sensor.LoadRunningBinarySensor,
    ):
        assert _category(cls) is None, cls.__name__


def test_no_card_finds_entities_by_category() -> None:
    """A card keyed on category would lose entities the moment one is recategorized."""
    for script in (COMPONENT / "frontend").glob("*.js"):
        assert "entity_category" not in script.read_text(encoding="utf-8"), script.name

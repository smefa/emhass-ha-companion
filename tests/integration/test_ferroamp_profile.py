"""Regression tests for the shipped Ferroamp Operation Settings profile.

The integration behind it stages every select, number and switch inside Home
Assistant and only sends them to the Ferroamp Portal when its Update button is
pressed. A command that forgets the press is accepted, shows the right states
in the UI, and changes nothing on the EnergyHub -- which is why every action is
checked to end on it.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util.yaml import load_yaml
import pytest

from custom_components.emhass_companion.phase_guard import clamp_blocker
from custom_components.emhass_companion.profiles import (
    BUILTIN_ROOT,
    Profile,
    async_execute_steps,
    render_action,
)
from custom_components.emhass_companion.profiles.schema import validate_document

ENTITIES = {
    "mode_select": "select.ferroamp_operation_settings_mode",
    "battery_power_mode_select": "select.ferroamp_operation_settings_battery_power_mode",
    "charge_reference_number": "number.ferroamp_operation_settings_charge_reference",
    "discharge_reference_number": "number.ferroamp_operation_settings_discharge_reference",
    "charge_threshold_number": "number.ferroamp_operation_settings_charge_threshold",
    "discharge_threshold_number": "number.ferroamp_operation_settings_discharge_threshold",
    "lower_reference_number": "number.ferroamp_operation_settings_lower_reference",
    "upper_reference_number": "number.ferroamp_operation_settings_upper_reference",
    "update_button": "button.ferroamp_operation_settings_update",
    "limit_export_switch": "switch.ferroamp_operation_settings_limit_export",
}
GET_DATA = "button.ferroamp_operation_settings_get_data"


def _profile() -> Profile:
    path = BUILTIN_ROOT / "inverter" / "ferroamp_operation_settings.yaml"
    document = validate_document(load_yaml(str(path)))
    return Profile(
        key="inverter/ferroamp_operation_settings",
        path=str(path),
        kind="inverter",
        name=document["name"],
        document=document,
    )


def _options(**overrides) -> dict:
    """What an untouched setup form submits: the entities plus every default."""
    defaults = {
        key: option["default"] for key, option in _profile().options.items() if "default" in option
    }
    return {**defaults, **ENTITIES, **overrides}


def _writes(steps: list[dict]) -> dict[str, object]:
    """Entity -> the value or option it is set to, for the steps that set one."""
    written = {}
    for step in steps:
        data = step.get("data") or {}
        if "value" in data:
            written[step["target"]["entity_id"]] = data["value"]
        elif "option" in data:
            written[step["target"]["entity_id"]] = data["option"]
    return written


@pytest.mark.parametrize("action", ["self_consume", "force_charge", "force_discharge", "idle"])
def test_every_action_ends_by_pressing_update(hass: HomeAssistant, action: str) -> None:
    steps = render_action(hass, _profile(), _options(), action, power_w=-2000)
    assert steps[-1] == {
        "service": "button.press",
        "target": {"entity_id": ENTITIES["update_button"]},
    }


def test_force_charge_is_manual_charge_at_the_planned_power(hass: HomeAssistant) -> None:
    steps = render_action(hass, _profile(), _options(), "force_charge", power_w=-3240)
    written = _writes(steps)

    assert written[ENTITIES["mode_select"]] == "Default"  # = Manual in the Portal
    assert written[ENTITIES["battery_power_mode_select"]] == "Charge"
    assert written[ENTITIES["charge_reference_number"]] == 3200  # 100 W steps
    assert written[ENTITIES["discharge_reference_number"]] == 0


def test_force_discharge_is_manual_discharge_at_the_planned_power(hass: HomeAssistant) -> None:
    steps = render_action(hass, _profile(), _options(), "force_discharge", power_w=4100)
    written = _writes(steps)

    assert written[ENTITIES["mode_select"]] == "Default"
    assert written[ENTITIES["battery_power_mode_select"]] == "Discharge"
    assert written[ENTITIES["discharge_reference_number"]] == 4100
    assert written[ENTITIES["charge_reference_number"]] == 0


def test_forced_commands_disarm_the_export_limit_and_write_soc_bounds(
    hass: HomeAssistant,
) -> None:
    """Without Limit export off, a forced discharge cannot reach the grid."""
    for action in ("force_charge", "force_discharge"):
        steps = render_action(hass, _profile(), _options(soc_min=8), action, power_w=1000)
        written = _writes(steps)
        assert {
            "service": "switch.turn_off",
            "target": {"entity_id": ENTITIES["limit_export_switch"]},
        } in steps
        assert written[ENTITIES["lower_reference_number"]] == 8
        assert written[ENTITIES["upper_reference_number"]] == 100


def test_swapped_soc_limits_are_written_in_order(hass: HomeAssistant) -> None:
    """Options cannot be cross-validated, so a lower limit typed above the
    upper one must not reach the Portal as-is: it would block both charging
    and discharging."""
    steps = render_action(
        hass, _profile(), _options(soc_min=90, soc_max=20), "self_consume", power_w=0
    )
    written = _writes(steps)
    assert written[ENTITIES["lower_reference_number"]] == 20
    assert written[ENTITIES["upper_reference_number"]] == 90


def test_idle_is_manual_with_battery_power_off(hass: HomeAssistant) -> None:
    steps = render_action(hass, _profile(), _options(), "idle", power_w=0)
    written = _writes(steps)

    # "Off" must stay a string: unquoted, YAML would hand the select a False.
    assert written == {
        ENTITIES["battery_power_mode_select"]: "Off",
        ENTITIES["mode_select"]: "Default",
    }


def test_self_consume_is_peak_shaving_around_the_configured_thresholds(
    hass: HomeAssistant,
) -> None:
    options = _options(
        self_consume_charge_threshold_w=-75,
        self_consume_discharge_threshold_w=-75,
        self_consume_power_w=12000,
    )
    steps = render_action(hass, _profile(), options, "self_consume", power_w=0)
    written = _writes(steps)

    assert written[ENTITIES["mode_select"]] == "Peak Shaving"
    assert written[ENTITIES["charge_threshold_number"]] == -75
    assert written[ENTITIES["discharge_threshold_number"]] == -75
    assert written[ENTITIES["charge_reference_number"]] == 12000
    assert written[ENTITIES["discharge_reference_number"]] == 12000


def test_self_consume_defaults_hold_the_grid_at_zero(hass: HomeAssistant) -> None:
    steps = render_action(hass, _profile(), _options(), "self_consume", power_w=0)
    written = _writes(steps)

    assert written[ENTITIES["charge_threshold_number"]] == 0
    assert written[ENTITIES["discharge_threshold_number"]] == 0


def test_get_data_runs_first_when_configured(hass: HomeAssistant) -> None:
    steps = render_action(
        hass, _profile(), _options(get_data_button=GET_DATA), "force_charge", power_w=-1000
    )
    assert steps[0] == {"service": "button.press", "target": {"entity_id": GET_DATA}}


@pytest.fixture
def recorded(hass: HomeAssistant) -> list[ServiceCall]:
    calls: list[ServiceCall] = []

    async def _record(call: ServiceCall) -> None:
        calls.append(call)

    for domain, service in (
        ("button", "press"),
        ("number", "set_value"),
        ("select", "select_option"),
        ("switch", "turn_off"),
    ):
        hass.services.async_register(domain, service, _record)
    return calls


async def test_unset_optional_entities_are_skipped_not_sent(
    hass: HomeAssistant, recorded: list[ServiceCall]
) -> None:
    """Get data is off by default, and Limit export may be cleared on purpose
    by someone with a standing export cap. Neither may turn into a call aimed
    at an empty entity id."""
    options = _options()
    del options["limit_export_switch"]

    steps = render_action(hass, _profile(), options, "force_discharge", power_w=2000)
    await async_execute_steps(hass, steps)
    await hass.async_block_till_done()

    targets = [call.data["entity_id"] for call in recorded]
    # Get data and Limit export are the two dropped; everything else is sent.
    assert len(targets) == len(steps) - 2
    assert "" not in targets
    assert ENTITIES["limit_export_switch"] not in targets
    assert targets[-1] == ENTITIES["update_button"]


def test_write_floor_keeps_the_phase_clamp_available() -> None:
    """A cloud route wants a write floor, but past 30 s the phase guard's
    real-time charge clamp switches itself off."""
    control = _profile().control
    assert (
        clamp_blocker(
            battery_enabled=True,
            battery_power_entity="sensor.ferroamp_battery_power",
            min_write_interval_s=control["min_write_interval_s"],
        )
        is None
    )

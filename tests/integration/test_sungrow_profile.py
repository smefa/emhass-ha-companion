"""Regression tests for the shipped Sungrow SH-RT profile.

Sungrow's own hardware quirk -- forced charge/discharge only takes effect
while EMS mode is "Forced mode"; the direction select and power number are
otherwise inert -- means `idle` (battery held, neither charging nor
discharging) is a real third state on this hardware, not a synonym for
self-consumption. The profile's `idle` action used to leave EMS mode
untouched, silently inheriting whatever the previous action set: correct after
a forced command, a silent no-op after self_consume. That path-dependent
defect is what these tests pin down.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.util.yaml import load_yaml

from custom_components.emhass_companion.profiles import BUILTIN_ROOT, Profile, render_action
from custom_components.emhass_companion.profiles.schema import validate_document

OPTIONS = {
    "ems_mode_select": "select.ems_mode",
    "direction_select": "select.battery_forced_charge_discharge",
    "power_number": "number.battery_forced_charge_discharge_power",
    "ems_mode_self_consume": "Self-consumption mode (default)",
    "ems_mode_forced": "Forced mode",
    "direction_stop": "Stop (default)",
    "direction_charge": "Forced charge",
    "direction_discharge": "Forced discharge",
    "export_limit_switch": "switch.export_power_limit",
    "export_limit_number": "number.export_power_limit",
}


def _profile() -> Profile:
    path = BUILTIN_ROOT / "inverter" / "sungrow_sh10rt.yaml"
    document = validate_document(load_yaml(str(path)))
    return Profile(
        key="inverter/sungrow_sh10rt",
        path=str(path),
        kind="inverter",
        name=document["name"],
        document=document,
    )


def _by_entity(steps: list[dict]) -> dict[str, dict]:
    return {step["target"]["entity_id"]: step for step in steps}


def test_idle_sets_ems_mode_to_forced(hass: HomeAssistant) -> None:
    """The fix: idle is Forced mode at 0 W on this hardware, so it must set
    EMS mode itself rather than relying on whatever the previous action left
    behind. Before this fix, arriving at idle from a self_consume decision
    silently did nothing to the battery."""
    steps = render_action(hass, _profile(), OPTIONS, "idle", power_w=0)
    by_entity = _by_entity(steps)
    assert by_entity[OPTIONS["ems_mode_select"]]["data"]["option"] == OPTIONS["ems_mode_forced"]


def test_idle_still_stops_and_zeros_the_direction_and_power(hass: HomeAssistant) -> None:
    steps = render_action(hass, _profile(), OPTIONS, "idle", power_w=0)
    by_entity = _by_entity(steps)
    assert by_entity[OPTIONS["power_number"]]["data"]["value"] == 0
    assert by_entity[OPTIONS["direction_select"]]["data"]["option"] == OPTIONS["direction_stop"]


def test_curtail_arms_the_switch_and_sets_the_limit(hass: HomeAssistant) -> None:
    steps = render_action(hass, _profile(), OPTIONS, "curtail", curtail_w=1500)
    by_entity = _by_entity(steps)

    assert by_entity[OPTIONS["export_limit_switch"]]["service"] == "switch.turn_on"
    assert by_entity[OPTIONS["export_limit_number"]]["service"] == "number.set_value"
    assert by_entity[OPTIONS["export_limit_number"]]["data"]["value"] == 1500


def test_uncurtail_turns_the_switch_off_and_nothing_else(hass: HomeAssistant) -> None:
    """No standing export cap is ever raised by this action -- it only ever
    releases the limit this profile itself set, matching what the reference
    automation this profile replaces did."""
    steps = render_action(hass, _profile(), OPTIONS, "uncurtail", curtail_w=0)
    assert steps == [
        {"service": "switch.turn_off", "target": {"entity_id": OPTIONS["export_limit_switch"]}}
    ]


def test_profile_defines_the_full_capability_set(hass: HomeAssistant) -> None:
    profile = _profile()
    for action in (
        "force_charge",
        "force_discharge",
        "idle",
        "self_consume",
        "curtail",
        "uncurtail",
    ):
        assert profile.defines(action), f"expected sungrow_sh10rt to define '{action}'"

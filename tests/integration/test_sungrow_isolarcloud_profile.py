"""Regression tests for the shipped Sungrow iSolarCloud profile.

Unlike the mkaiser Modbus profiles (sungrow_sh10rt / sungrow_sh_t), this
integration's battery_mode select carries the forced-mode gate and the
charge/discharge direction as a single field ("Self-consumption", "Force
charge", "Force discharge", "Stop") rather than two separate selects. These
tests pin down that one select is enough here -- no second "arm forced mode"
step is needed before idle or a forced action takes effect.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.util.yaml import load_yaml

from custom_components.emhass_companion.profiles import BUILTIN_ROOT, Profile, render_action
from custom_components.emhass_companion.profiles.schema import validate_document

OPTIONS = {
    "battery_mode_select": "select.battery_mode",
    "power_number": "number.charge_discharge_power",
    "mode_self_consume": "Self-consumption",
    "mode_force_charge": "Force charge",
    "mode_force_discharge": "Force discharge",
    "mode_stop": "Stop",
    "feed_in_limitation_select": "select.feed_in_limitation",
    "feed_in_limitation_number": "number.feed_in_limitation_value",
    "feed_in_limitation_enable": "Enable",
    "feed_in_limitation_disable": "Disable",
}


def _profile() -> Profile:
    path = BUILTIN_ROOT / "inverter" / "sungrow_isolarcloud.yaml"
    document = validate_document(load_yaml(str(path)))
    return Profile(
        key="inverter/sungrow_isolarcloud",
        path=str(path),
        kind="inverter",
        name=document["name"],
        document=document,
    )


def _by_entity(steps: list[dict]) -> dict[str, dict]:
    return {step["target"]["entity_id"]: step for step in steps}


def test_force_charge_writes_power_before_mode(hass: HomeAssistant) -> None:
    steps = render_action(hass, _profile(), OPTIONS, "force_charge", power_w=-2500)
    assert steps[0]["target"]["entity_id"] == OPTIONS["power_number"]
    assert steps[1]["target"]["entity_id"] == OPTIONS["battery_mode_select"]
    assert steps[1]["data"]["option"] == OPTIONS["mode_force_charge"]


def test_force_discharge_selects_discharge_option(hass: HomeAssistant) -> None:
    steps = render_action(hass, _profile(), OPTIONS, "force_discharge", power_w=1800)
    by_entity = _by_entity(steps)
    assert by_entity[OPTIONS["power_number"]]["data"]["value"] == 1800
    assert (
        by_entity[OPTIONS["battery_mode_select"]]["data"]["option"]
        == OPTIONS["mode_force_discharge"]
    )


def test_idle_zeroes_power_and_stops_without_a_separate_arm_step(hass: HomeAssistant) -> None:
    steps = render_action(hass, _profile(), OPTIONS, "idle", power_w=0)
    assert len(steps) == 2
    by_entity = _by_entity(steps)
    assert by_entity[OPTIONS["power_number"]]["data"]["value"] == 0
    assert by_entity[OPTIONS["battery_mode_select"]]["data"]["option"] == OPTIONS["mode_stop"]


def test_self_consume_selects_self_consumption_and_zeroes_power(hass: HomeAssistant) -> None:
    steps = render_action(hass, _profile(), OPTIONS, "self_consume", power_w=0)
    by_entity = _by_entity(steps)
    assert (
        by_entity[OPTIONS["battery_mode_select"]]["data"]["option"] == OPTIONS["mode_self_consume"]
    )
    assert by_entity[OPTIONS["power_number"]]["data"]["value"] == 0


def test_curtail_sets_the_limit_before_enabling_it(hass: HomeAssistant) -> None:
    steps = render_action(hass, _profile(), OPTIONS, "curtail", curtail_w=3000)
    assert steps[0]["target"]["entity_id"] == OPTIONS["feed_in_limitation_number"]
    assert steps[0]["data"]["value"] == 3000
    assert steps[1]["target"]["entity_id"] == OPTIONS["feed_in_limitation_select"]
    assert steps[1]["data"]["option"] == OPTIONS["feed_in_limitation_enable"]


def test_uncurtail_only_disables_the_limit(hass: HomeAssistant) -> None:
    steps = render_action(hass, _profile(), OPTIONS, "uncurtail", curtail_w=0)
    assert steps == [
        {
            "service": "select.select_option",
            "target": {"entity_id": OPTIONS["feed_in_limitation_select"]},
            "data": {"option": OPTIONS["feed_in_limitation_disable"]},
        }
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
        assert profile.defines(action), f"expected sungrow_isolarcloud to define '{action}'"

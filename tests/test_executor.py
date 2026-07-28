"""Tests for the battery action decision.

This is the logic that decides what to do with a battery, so the cases that
matter most are the ones where doing nothing is safer than acting: a stale
plan, a missing row, a manual override.
"""

from __future__ import annotations

import pytest

from custom_components.emhass_companion.configuration import EmhassConfig
from custom_components.emhass_companion.const import (
    DEFAULT_POWER_DEADBAND_W,
    MODE_FORCE_CHARGE,
    MODE_FORCE_DISCHARGE,
    MODE_IDLE,
)
from custom_components.emhass_companion.executor import Decision, _battery_action
from custom_components.emhass_companion.models import BatteryConfig


def _config(**battery) -> EmhassConfig:
    defaults = {
        "enabled": True,
        "capacity_wh": 10000,
        "charge_power_max_w": 5000,
        "discharge_power_max_w": 5000,
    }
    return EmhassConfig(url="http://x", battery=BatteryConfig(**{**defaults, **battery}))


# --- sign convention ---------------------------------------------------------


def test_positive_plan_power_means_discharge():
    """EMHASS: positive is discharge. Reversing this inverts every decision."""
    action, power = _battery_action(3000, _config())
    assert action == MODE_FORCE_DISCHARGE
    assert power == 3000


def test_negative_plan_power_means_charge():
    action, power = _battery_action(-2500, _config())
    assert action == MODE_FORCE_CHARGE
    assert power == 2500


@pytest.mark.parametrize("p_batt", [0, 50, -50, DEFAULT_POWER_DEADBAND_W - 1])
def test_near_zero_power_is_idle(p_batt):
    """Small residuals must not produce a stream of tiny charge commands."""
    action, power = _battery_action(p_batt, _config())
    assert action == MODE_IDLE
    assert power == 0.0


# --- limits ------------------------------------------------------------------


def test_discharge_is_clamped_to_the_configured_maximum():
    _, power = _battery_action(99000, _config(discharge_power_max_w=5000))
    assert power == 5000


def test_charge_is_clamped_to_the_configured_maximum():
    _, power = _battery_action(-99000, _config(charge_power_max_w=4000))
    assert power == 4000


# --- decision reporting ------------------------------------------------------


def test_decision_attributes_are_serialisable():
    """These are entity attributes, so they must survive JSON encoding."""
    decision = Decision(
        action=MODE_FORCE_CHARGE,
        power_w=2500.4,
        reason="plan schedules -2500 W",
        loads={"abc": True},
        steps=[{"service": "number.set_value", "data": {"value": 2500}}],
    )
    attributes = decision.as_attributes()

    assert attributes["action"] == MODE_FORCE_CHARGE
    assert attributes["power_w"] == 2500
    assert attributes["applied"] is False
    assert attributes["loads"] == {"abc": True}
    assert attributes["steps"][0]["service"] == "number.set_value"


def test_a_decision_defaults_to_not_applied():
    """Nothing counts as applied until it demonstrably has been."""
    assert Decision(action=MODE_IDLE).applied is False

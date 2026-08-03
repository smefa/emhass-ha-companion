"""Tests for the Decision reporting shape.

The battery/curtailment decision logic itself lives in strategy.py and is
tested there (tests/test_strategy.py); this file covers what Decision, as an
object, guarantees regardless of how it was produced.
"""

from __future__ import annotations

from custom_components.emhass_companion.const import MODE_FORCE_CHARGE, MODE_IDLE
from custom_components.emhass_companion.executor import Decision


def test_decision_attributes_are_serialisable():
    """These are entity attributes, so they must survive JSON encoding."""
    decision = Decision(
        action=MODE_FORCE_CHARGE,
        power_w=2500.4,
        reason="plan schedules -2500 W",
        loads={"abc": True},
        steps=[{"service": "number.set_value", "data": {"value": 2500}}],
        curtail=True,
        curtail_w=1200.0,
        rules=["plan curtails 1200W PV"],
    )
    attributes = decision.as_attributes()

    assert attributes["action"] == MODE_FORCE_CHARGE
    assert attributes["power_w"] == 2500
    assert attributes["applied"] is False
    assert attributes["loads"] == {"abc": True}
    assert attributes["steps"][0]["service"] == "number.set_value"
    assert attributes["curtail"] is True
    assert attributes["curtail_w"] == 1200
    assert attributes["rules"] == ["plan curtails 1200W PV"]


def test_a_decision_defaults_to_not_applied():
    """Nothing counts as applied until it demonstrably has been."""
    assert Decision(action=MODE_IDLE).applied is False


def test_a_decision_defaults_to_no_curtailment_capability():
    """None, not False -- until something says otherwise, curtailment is not
    known to be applicable at all."""
    decision = Decision(action=MODE_IDLE)
    assert decision.curtail is None
    assert decision.as_attributes()["curtail"] is None

"""Tests for the plan-driven battery and curtailment strategy.

This is the logic that replaces a hand-written per-inverter automation with a
generic one: reading the plan's own P_grid column to decide self-consumption
vs forcing, instead of reconstructing a residual by hand and getting it wrong
by the inverter's own DC->AC conversion loss (see docs/on this in the plan
write-up). The most important cases here are the ones where the new logic
deliberately disagrees with what a hand-rolled automation would do.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from custom_components.emhass_companion.configuration import EmhassConfig
from custom_components.emhass_companion.const import (
    DEFAULT_POWER_DEADBAND_W,
    DEFAULT_SELF_CONSUME_THRESHOLD_W,
    MODE_FORCE_CHARGE,
    MODE_FORCE_DISCHARGE,
    MODE_IDLE,
    MODE_SELF_CONSUME,
)
from custom_components.emhass_companion.models import BatteryConfig, GridConfig, PlanRow
from custom_components.emhass_companion.strategy import decide_battery, decide_curtailment

FIXTURES = Path(__file__).parent / "fixtures"


def _config(**battery) -> EmhassConfig:
    defaults = {
        "enabled": True,
        "capacity_wh": 10000,
        "charge_power_max_w": 5000,
        "discharge_power_max_w": 5000,
    }
    return EmhassConfig(url="http://x", battery=BatteryConfig(**{**defaults, **battery}))


def _row(*, p_grid: float | None, p_batt: float | None = 0.0, **fields) -> PlanRow:
    from datetime import UTC, datetime

    return PlanRow(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC), p_grid=p_grid, p_batt=p_batt, **fields
    )


# --- decide_battery: rule 1, self-consumption -------------------------------


def test_no_grid_exchange_means_self_consume():
    """The automation's central insight: |p_grid| within threshold -> hand the
    battery to the inverter's own self-consumption mode, regardless of what
    p_batt says."""
    row = _row(p_grid=0.0, p_batt=-5816)  # a genuinely large planned charge
    action, power, rules = decide_battery(row, _config(), in_self_consume=False)
    assert action == MODE_SELF_CONSUME
    assert power == 0.0
    assert rules


@pytest.mark.parametrize("p_grid", [0, 150, -150, DEFAULT_SELF_CONSUME_THRESHOLD_W])
def test_within_entry_threshold_is_self_consume(p_grid):
    row = _row(p_grid=p_grid, p_batt=-2000)
    action, _, _ = decide_battery(row, _config(), in_self_consume=False)
    assert action == MODE_SELF_CONSUME


def test_just_beyond_entry_threshold_is_not_self_consume():
    row = _row(p_grid=DEFAULT_SELF_CONSUME_THRESHOLD_W + 1, p_batt=-2000)
    action, _, _ = decide_battery(row, _config(), in_self_consume=False)
    assert action != MODE_SELF_CONSUME


def test_hysteresis_keeps_a_boundary_row_in_self_consume():
    """Already self-consuming: leaving requires clearing threshold * exit
    factor, not just the entry threshold, so a plan sitting right at the
    boundary does not flip mode on every recalculation."""
    p_grid = DEFAULT_SELF_CONSUME_THRESHOLD_W * 1.5  # beyond entry, within exit
    row = _row(p_grid=p_grid, p_batt=-2000)

    entering = decide_battery(row, _config(), in_self_consume=False)
    already_in = decide_battery(row, _config(), in_self_consume=True)

    assert entering[0] != MODE_SELF_CONSUME
    assert already_in[0] == MODE_SELF_CONSUME


def test_hysteresis_does_not_prevent_leaving_when_grid_exchange_is_large():
    p_grid = DEFAULT_SELF_CONSUME_THRESHOLD_W * 3
    row = _row(p_grid=p_grid, p_batt=-2000)
    action, _, _ = decide_battery(row, _config(), in_self_consume=True)
    assert action != MODE_SELF_CONSUME


def test_no_p_grid_on_the_row_falls_back_to_battery_sign():
    """Honest about not having the information to do better -- also what keeps
    the existing hand-built single-row test plans (with no P_grid at all)
    behaving exactly as before."""
    row = _row(p_grid=None, p_batt=-3000)
    action, power, rules = decide_battery(row, _config(), in_self_consume=False)
    assert action == MODE_FORCE_CHARGE
    assert power == 3000
    assert "p_grid" in rules[0]


# --- decide_battery: rule 2, sign/magnitude of p_batt -----------------------


def test_positive_plan_power_means_discharge():
    row = _row(p_grid=5000, p_batt=3000)
    action, power, _ = decide_battery(row, _config(), in_self_consume=False)
    assert action == MODE_FORCE_DISCHARGE
    assert power == 3000


def test_negative_plan_power_means_charge():
    row = _row(p_grid=-5000, p_batt=-2500)
    action, power, _ = decide_battery(row, _config(), in_self_consume=False)
    assert action == MODE_FORCE_CHARGE
    assert power == 2500


def test_discharge_is_clamped_to_the_configured_maximum():
    row = _row(p_grid=99000, p_batt=99000)
    _, power, _ = decide_battery(row, _config(discharge_power_max_w=5000), in_self_consume=False)
    assert power == 5000


def test_charge_is_clamped_to_the_configured_maximum():
    row = _row(p_grid=-99000, p_batt=-99000)
    _, power, _ = decide_battery(row, _config(charge_power_max_w=4000), in_self_consume=False)
    assert power == 4000


def test_grid_exchange_with_near_zero_battery_is_idle_not_forced():
    """The one case where this deliberately disagrees with the automation it
    replaces: that automation's default branch fell through to 'Forced
    discharge' at 0 W whenever p_batt was near zero but grid exchange was
    still planned. Holding the battery (idle) is what was actually meant."""
    row = _row(p_grid=2000, p_batt=0)
    action, power, _ = decide_battery(row, _config(), in_self_consume=False)
    assert action == MODE_IDLE
    assert power == 0.0


@pytest.mark.parametrize("p_batt", [0, 50, -50, DEFAULT_POWER_DEADBAND_W - 1])
def test_near_zero_battery_power_is_idle(p_batt):
    row = _row(p_grid=5000, p_batt=p_batt)
    action, power, _ = decide_battery(row, _config(), in_self_consume=False)
    assert action == MODE_IDLE
    assert power == 0.0


# --- decide_curtailment -------------------------------------------------------


def test_plan_curtailment_column_takes_priority():
    """curtail_w is the allowed *export* (max(0, -p_grid)), not the shed
    amount -- an export_limit profile writes it straight to hardware as a
    feed-in cap, and those are not the same number."""
    row = _row(p_grid=-1200, p_pv_curtailment=1900, unit_prod_price=0.10)
    curtail, curtail_w, rules = decide_curtailment(row, _config(), soc_percent=50)
    assert curtail is True
    assert curtail_w == 1200
    assert rules


def test_plan_curtailment_is_skipped_without_p_grid():
    """Translating a shed amount into an export cap without P_grid means
    reconstructing the PV/load/battery balance by hand -- exactly the
    misclassification this module's docstring warns about avoiding."""
    row = _row(p_grid=None, p_pv_curtailment=1200, unit_prod_price=0.10)
    curtail, curtail_w, _rules = decide_curtailment(row, _config(), soc_percent=50)
    assert curtail is False
    assert curtail_w == 0.0


def test_no_curtailment_signal_means_uncurtail():
    row = _row(p_grid=0, p_pv_curtailment=None, unit_prod_price=0.10)
    curtail, curtail_w, rules = decide_curtailment(row, _config(), soc_percent=50)
    assert curtail is False
    assert curtail_w == 0.0
    assert rules == []


def test_negative_price_curtails_only_when_opted_in():
    row = _row(p_grid=0, p_pv_curtailment=None, unit_prod_price=-0.05)
    config = _config(soc_max=0.95)

    off = decide_curtailment(row, config, soc_percent=98)
    assert off[0] is False

    config.grid = GridConfig(curtail_on_negative_price=True)
    on = decide_curtailment(row, config, soc_percent=98)
    assert on[0] is True


def test_negative_price_curtailment_requires_no_battery_headroom():
    row = _row(p_grid=0, p_pv_curtailment=None, unit_prod_price=-0.05)
    config = _config(soc_max=0.95)
    config.grid = GridConfig(curtail_on_negative_price=True)

    has_headroom = decide_curtailment(row, config, soc_percent=50)
    assert has_headroom[0] is False

    unknown_soc = decide_curtailment(row, config, soc_percent=None)
    assert unknown_soc[0] is False


def test_positive_price_never_curtails_even_when_opted_in():
    row = _row(p_grid=0, p_pv_curtailment=None, unit_prod_price=0.20)
    config = _config(soc_max=0.95)
    config.grid = GridConfig(curtail_on_negative_price=True)
    curtail, _, _ = decide_curtailment(row, config, soc_percent=99)
    assert curtail is False


# --- against the real reference plan ------------------------------------------


def _load_reference_plan() -> list[PlanRow]:
    with (FIXTURES / "opt_res_reference_plan.csv").open(newline="", encoding="utf-8") as handle:
        return [PlanRow.from_record(record) for record in csv.DictReader(handle)]


def test_reference_plan_has_the_expected_shape():
    """Pins the fixture itself -- if this ever changes, the assertions below
    need to be re-derived, not silently pass against different data. (This
    fixture is a snapshot of a live EMHASS output file, re-generated every
    mpc_interval -- the counts here are this snapshot's, not a property of
    the algorithm.)"""
    rows = _load_reference_plan()
    assert len(rows) == 96
    zero_grid = [row for row in rows if row.p_grid == 0.0]
    assert len(zero_grid) == 45


def test_reference_plan_zero_grid_rows_are_mostly_real_battery_work():
    """Most of this real plan's zero-grid rows are not idle intervals: today
    the executor issues a forced command (up to ~5800 W) for every one of
    them. Under decide_battery they resolve to self_consume instead -- the
    behaviour this change moves. (One of the 45 also happens to have a small
    P_batt already below the deadband; self_consume covers that row too, just
    not for an interesting reason.)"""
    rows = _load_reference_plan()
    zero_grid = [row for row in rows if row.p_grid == 0.0]
    doing_real_work = [row for row in zero_grid if abs(row.p_batt) > DEFAULT_POWER_DEADBAND_W]
    assert len(doing_real_work) == 44


def test_reference_plan_self_consume_rows_match_the_threshold_exactly():
    """decide_battery, run over every row of the real plan with the strict
    (entry) threshold throughout, classifies exactly the rows within
    self_consume_threshold_w as self_consume -- no more, no fewer, and
    independent of P_batt. 6 of the 51 are not exact zeros (-110 to -287 W)
    but still within the default 300 W threshold -- the deliberate slack
    the user asked for, confirmed here against real boundary cases rather
    than hand-picked ones."""
    rows = _load_reference_plan()
    config = _config()

    resolved = [decide_battery(row, config, in_self_consume=False)[0] for row in rows]
    self_consume_count = sum(1 for action in resolved if action == MODE_SELF_CONSUME)

    assert self_consume_count == 51
    for row, action in zip(rows, resolved, strict=True):
        within_threshold = abs(row.p_grid) <= DEFAULT_SELF_CONSUME_THRESHOLD_W
        assert within_threshold == (action == MODE_SELF_CONSUME)

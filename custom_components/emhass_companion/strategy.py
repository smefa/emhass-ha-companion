"""Turn one plan row into a battery action and a curtailment decision.

Pure functions -- no ``hass``, no coordinator -- so every case is a table of
inputs and outputs, checkable against a recorded plan without touching Home
Assistant at all.

Both decisions read the plan's own columns rather than reconstructing
anything. EMHASS already balances the plan internally (``P_grid``, in
particular, is exact -- confirmed to the float against a live plan, including
through the inverter's own AC conversion loss); recomputing that balance from
``P_PV`` the way a hand-rolled automation would is how a 3% DC->AC conversion
loss turns into a systematic multi-hundred-watt misclassification.
"""

from __future__ import annotations

from .configuration import EmhassConfig
from .const import (
    DEFAULT_POWER_DEADBAND_W,
    MODE_FORCE_CHARGE,
    MODE_FORCE_DISCHARGE,
    MODE_IDLE,
    MODE_SELF_CONSUME,
    SELF_CONSUME_EXIT_FACTOR,
)
from .models import PlanRow


def decide_battery(
    row: PlanRow, config: EmhassConfig, *, in_self_consume: bool
) -> tuple[str, float, list[str]]:
    """Map one plan row onto a battery action.

    Checked in order:

    1. No grid exchange planned (``|P_grid|`` within threshold) -> hand the
       battery to the inverter's own self-consumption mode. When the plan
       schedules no grid exchange, the battery is by definition exactly
       absorbing surplus or covering deficit -- self-consumption does that
       while tracking *real* PV and load, so forecast error is absorbed by the
       inverter instead of becoming grid import/export. Skipped entirely when
       the plan carries no ``P_grid`` (older EMHASS, or a hand-built plan in
       tests) -- falling back to the sign-of-``P_batt`` rule below is honest
       about not having the information to do better.

    2. Otherwise, the sign and magnitude of ``P_batt`` (EMHASS convention:
       positive is discharge, negative is charge).

    ``in_self_consume`` selects which threshold applies: entering
    self-consumption requires clearing the configured threshold, but once in
    it, leaving requires clearing ``threshold * SELF_CONSUME_EXIT_FACTOR``.
    Without that hysteresis, a plan sitting right at the boundary would flip
    the inverter's mode on every single recalculation.
    """
    threshold = config.battery.self_consume_threshold_w
    limit = threshold * SELF_CONSUME_EXIT_FACTOR if in_self_consume else threshold

    if row.p_grid is not None and abs(row.p_grid) <= limit:
        return (
            MODE_SELF_CONSUME,
            0.0,
            [f"|p_grid|={abs(row.p_grid):.0f}W within self-consume limit ({limit:.0f}W)"],
        )

    action, power = _battery_action(row.p_batt, config)
    if row.p_grid is None:
        rules = ["no p_grid on this row; falling back to p_batt sign"]
    else:
        rules = [f"|p_grid|={abs(row.p_grid):.0f}W beyond self-consume limit ({limit:.0f}W)"]
    return action, power, rules


def decide_curtailment(row: PlanRow) -> tuple[bool, float, list[str]]:
    """Whether to curtail export right now, and the export ceiling to apply.

    The returned ``curtail_w`` is the allowed *export* in watts -- what an
    ``export_limit`` profile (the common case; see
    docs/inverter_profile_template.yaml) writes straight to hardware as a
    feed-in cap, PV still serving load and battery underneath it. That is not
    the same number as the plan's ``P_PV_curtailment`` column, which is how
    much PV the optimiser chose not to *produce* -- a plan shedding 500 W off
    an 8 kW array is still exporting most of the rest, not capping export at
    500 W.

    One rule, and it is the plan's: the ``P_PV_curtailment`` column, which
    exists only when EMHASS is asked to optimise curtailment (the
    ``compute_curtailment`` setting on the grid step). The cap is read off
    ``P_grid`` (``max(0, -p_grid)``) rather than off ``P_PV_curtailment``
    itself, because ``P_grid`` already is the post-curtailment balance -- the
    same reason ``decide_battery`` reads ``P_grid`` instead of reconstructing
    the balance from ``P_PV``. Skipped when the row carries no ``P_grid``:
    translating a shed amount into an export cap without it means recomputing
    the PV/load/battery balance by hand, which is exactly the
    misclassification the module docstring warns about.

    There was a second, opt-in rule here -- curtail to zero whenever the sell
    price was negative and the battery was full -- from back when
    ``compute_curtailment`` could only be set by editing the add-on's own
    configuration, so a Companion-side heuristic was the only curtailment most
    installs could reach. It is gone: EMHASS prices negative export properly
    once asked to, including choosing *not* to curtail when exporting beats
    shedding, and a hardcoded threshold running after it could only overrule
    that. See docs/inverter_control.md for the one hardware case
    (``zero_export_switch``) that would want something like it back.

    Otherwise, uncurtail.
    """
    if row.p_pv_curtailment is not None and row.p_pv_curtailment > 0 and row.p_grid is not None:
        allowed_export_w = max(0.0, -row.p_grid)
        return (
            True,
            allowed_export_w,
            [
                f"plan curtails {row.p_pv_curtailment:.0f}W PV; capping export at "
                f"{allowed_export_w:.0f}W (p_grid={row.p_grid:.0f}W)"
            ],
        )

    return False, 0.0, []


def _battery_action(p_batt: float, config: EmhassConfig) -> tuple[str, float]:
    """Map planned battery power onto an action.

    EMHASS's convention: positive is discharge, negative is charge. Getting
    that backwards inverts every decision the optimiser makes, so it is stated
    here rather than assumed at the call site.
    """
    if p_batt > DEFAULT_POWER_DEADBAND_W:
        return MODE_FORCE_DISCHARGE, min(p_batt, config.battery.discharge_power_max_w)
    if p_batt < -DEFAULT_POWER_DEADBAND_W:
        return MODE_FORCE_CHARGE, min(-p_batt, config.battery.charge_power_max_w)
    return MODE_IDLE, 0.0


__all__ = ["decide_battery", "decide_curtailment"]

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


def decide_curtailment(
    row: PlanRow, config: EmhassConfig, soc_percent: float | None
) -> tuple[bool, float, list[str]]:
    """Whether to curtail export right now, and the export ceiling to apply.

    The returned ``curtail_w`` is the allowed *export* in watts -- what an
    ``export_limit`` profile (the common case; see
    docs/inverter_profile_template.yaml) writes straight to hardware as a
    feed-in cap, PV still serving load and battery underneath it. That is not
    the same number as the plan's ``P_PV_curtailment`` column, which is how
    much PV the optimiser chose not to *produce* -- a plan shedding 500 W off
    an 8 kW array is still exporting most of the rest, not capping export at
    500 W.

    Two independent rules, in priority order, both plan-driven:

    1. The plan's own ``P_PV_curtailment`` column, when EMHASS's curtailment
       is enabled. This is the primary rule -- ``None`` whenever it is not,
       which is most installs, including the reference one this was built
       against. The cap is read off ``P_grid`` (``max(0, -p_grid)``) rather
       than off ``P_PV_curtailment`` itself, because ``P_grid`` already is
       the post-curtailment balance -- the same reason ``decide_battery``
       reads ``P_grid`` instead of reconstructing the balance from ``P_PV``.
       Skipped when the row carries no ``P_grid``: translating a shed amount
       into an export cap without it means recomputing the PV/load/battery
       balance by hand, which is exactly the misclassification the module
       docstring warns about.
    2. Opt-in: the plan's sell price is negative and the battery has no
       headroom (real-time SOC at or above the configured maximum) to absorb
       the surplus instead of exporting it into a negative price. Off by
       default -- it is a second optimiser competing with EMHASS's own
       curtailment cost function, which already prices this in when enabled.
       Caps export at 0 outright, since the whole point is that nothing
       should be exported.

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

    if (
        config.grid.curtail_on_negative_price
        and row.unit_prod_price is not None
        and row.unit_prod_price < 0
        and soc_percent is not None
        and soc_percent >= config.battery.soc_max * 100
    ):
        return (
            True,
            0.0,
            [
                f"unit_prod_price={row.unit_prod_price:.4f} < 0 and battery at "
                f"{soc_percent:.0f}% (>= soc_max) has no headroom"
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

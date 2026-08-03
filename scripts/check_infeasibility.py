#!/usr/bin/env python3
"""Diagnose "EMHASS reported the optimisation problem as infeasible".

Standalone and dependency-free on purpose: this is meant to be copy-pasted
and run by anyone hitting the repair issue, not just from a dev checkout.

Where to get the input
-----------------------
* Settings -> Devices & services -> EMHASS Companion -> the device page ->
  the three-dot menu -> Download diagnostics. Point this script at the
  downloaded file.
* Or enable the (disabled-by-default) "Last request to EMHASS" sensor and
  save its ``payload`` attribute as JSON.

Usage
-----
    python3 check_infeasibility.py path/to/diagnostics.json
    python3 check_infeasibility.py path/to/payload.json
    cat diagnostics.json | python3 check_infeasibility.py -

What this is, and is not
-------------------------
This does not run EMHASS's solver and cannot prove a plan is feasible -- that
would mean re-implementing the whole MILP. What it does is check the specific
patterns that repeatedly turn out to be the real cause behind "infeasible"
once you dig into EMHASS's optimisation.py: a deferrable load whose window is
narrower than its own run time, a battery that can only ever be charged by
PV and nothing else, and forecast/grid/inverter limits that make an
unavoidable power deficit or surplus at some timestep. Every check below
names the constraint in EMHASS it mirrors. A clean report is good evidence,
not a guarantee -- the two power-balance checks in particular look at each
timestep in isolation and ignore the battery's state of charge carrying over
between timesteps, so they can miss infeasibilities that only appear once
SOC is tracked through the whole horizon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path
import sys


class Severity(Enum):
    CRITICAL = "CRITICAL"  # would make EMHASS's own constraints unsatisfiable
    WARNING = "WARNING"  # plausible cause, needs the data to judge
    INFO = "INFO"  # not a cause by itself, but shapes how the others read


@dataclass
class Finding:
    severity: Severity
    title: str
    detail: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, severity: Severity, title: str, detail: str) -> None:
        self.findings.append(Finding(severity, title, detail))

    def by_severity(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity is severity]


# -- loading ------------------------------------------------------------------


def load_payload(raw: dict) -> tuple[dict, list[str], dict]:
    """Pull the EMHASS payload, companion warnings, and last_run info out of
    whatever was handed in.

    Accepts a full diagnostics download (payload under ``last_payload``) or a
    bare payload dict (from the sensor attribute), so either export path
    works without the user having to pick the right shape by hand.
    """
    if "last_payload" in raw:
        payload = raw.get("last_payload") or {}
        warnings = raw.get("warnings") or []
        last_run = raw.get("last_run") or {}
    elif "payload" in raw and isinstance(raw["payload"], dict):
        payload = raw["payload"]
        warnings = raw.get("warnings") or []
        last_run = {}
    else:
        payload = raw
        warnings = []
        last_run = {}
    return payload, warnings, last_run


# -- checks ---------------------------------------------------------------


def check_companion_warnings(payload: dict, warnings: list[str], report: Report) -> None:
    """Surface the integration's own warnings first.

    These already catch a load whose window doesn't fit its run time and a
    forecast that runs out before the horizon does (payload.py's own
    resolve_load_window and the forecast-coverage check) -- no need to
    recompute what the integration already told you at request time.
    """
    for message in warnings:
        report.add(Severity.WARNING, "Flagged when the request was built", message)


def check_deferrable_windows(payload: dict, report: Report) -> None:
    """Re-derive whether each load's window can actually fit its run time.

    Mirrors EMHASS's own reading of ``start_timesteps_of_each_deferrable_load``
    / ``end_timesteps_of_each_deferrable_load`` against
    ``operating_hours_of_each_deferrable_load``: a semi-continuous load can
    only draw 0 or exactly its nominal power, so it needs the full run time to
    fit inside its window with nothing left over (payload.py's
    operating_timesteps / resolve_load_window). Kept as a second, independent
    pass alongside check_companion_warnings in case this payload came from
    somewhere other than the integration's own warnings (EMHASS's own logs,
    for instance).
    """
    step_minutes = payload.get("optimization_time_step")
    n = payload.get("number_of_deferrable_loads", 0)
    if not n or not step_minutes:
        return
    hours = payload.get("operating_hours_of_each_deferrable_load", [])
    starts = payload.get("start_timesteps_of_each_deferrable_load", [])
    ends = payload.get("end_timesteps_of_each_deferrable_load", [])
    powers = payload.get("nominal_power_of_deferrable_loads", [])
    completed = payload.get("def_current_operating_timesteps", [0] * n)

    for i in range(n):
        h = hours[i] if i < len(hours) else 0
        if not h:
            continue
        needed = max(1, round(h * 60 / step_minutes)) - (completed[i] if i < len(completed) else 0)
        start = starts[i] if i < len(starts) else 0
        end = ends[i] if i < len(ends) else 0
        window = end - start
        power = powers[i] if i < len(powers) else "?"
        if window < needed:
            report.add(
                Severity.CRITICAL,
                f"Deferrable load #{i} ({power:g} W) doesn't fit its window",
                f"Needs {needed} timestep(s) to run {h:g} h, but its window "
                f"(timesteps {start}-{end}) only has {max(window, 0)}. EMHASS "
                "reports this as a whole-problem infeasibility with no hint "
                "which load caused it.",
            )


def check_array_lengths(payload: dict, report: Report) -> None:
    """A length mismatch is a raw crash/infeasible cause, not a subtle one.

    EMHASS indexes several of these lists directly with no bounds check
    (``deferrable_load_max_cost`` in particular -- see the comment in
    payload.py's _deferrable_settings), so a short list throws before the
    solver even runs.
    """
    forecast_keys = [
        "pv_power_forecast",
        "load_power_forecast",
        "load_cost_forecast",
        "prod_price_forecast",
    ]
    lengths = {k: len(payload[k]) for k in forecast_keys if isinstance(payload.get(k), list)}
    if len(set(lengths.values())) > 1:
        report.add(
            Severity.CRITICAL,
            "Forecast arrays disagree on length",
            f"{lengths}. These are meant to describe the same horizon at the "
            "same timestep and must be the same length.",
        )
    for key, arr in ((k, payload.get(k)) for k in forecast_keys):
        if not isinstance(arr, list):
            continue
        bad = [i for i, v in enumerate(arr) if v is None or (isinstance(v, float) and v != v)]
        if bad:
            report.add(
                Severity.CRITICAL,
                f"{key} has {len(bad)} null/NaN value(s)",
                f"First at index {bad[0]}. A forecast source that returned "
                "nothing for part of the horizon produces exactly this.",
            )

    n = payload.get("number_of_deferrable_loads", 0)
    if n:
        per_load_keys = [
            "nominal_power_of_deferrable_loads",
            "minimum_power_of_deferrable_loads",
            "operating_hours_of_each_deferrable_load",
            "start_timesteps_of_each_deferrable_load",
            "end_timesteps_of_each_deferrable_load",
            "deferrable_load_max_cost",
        ]
        for key in per_load_keys:
            arr = payload.get(key)
            if isinstance(arr, list) and len(arr) != n:
                report.add(
                    Severity.CRITICAL,
                    f"{key} has {len(arr)} entries, expected {n}",
                    "number_of_deferrable_loads and every per-load array must "
                    "agree on length; EMHASS indexes some of these with no "
                    "bounds check.",
                )


def check_battery_soc(payload: dict, report: Report) -> None:
    """Sanity-check soc_init/soc_final against the configured band.

    Not a hard failure by itself: EMHASS 0.17.9 added a recovery mechanism
    that tolerates a starting SOC outside [min, max] and lets the plan work
    its way back in, rather than refusing outright. Reported as WARNING context,
    not CRITICAL.
    """
    if not payload.get("set_use_battery"):
        return
    soc_min = payload.get("battery_minimum_state_of_charge")
    soc_max = payload.get("battery_maximum_state_of_charge")
    for label in ("soc_init", "soc_final"):
        value = payload.get(label)
        if value is None or soc_min is None or soc_max is None:
            continue
        if value < soc_min or value > soc_max:
            report.add(
                Severity.WARNING,
                f"{label}={value:g} is outside [{soc_min:g}, {soc_max:g}]",
                "EMHASS will try to recover back into band rather than "
                "refuse outright, but this is worth confirming against the "
                "real battery reading -- a stale or misread SOC sensor "
                "produces exactly this.",
            )


def check_hybrid_inverter(payload: dict, report: Report) -> None:
    """Flag when the battery has no path to grid charging.

    inverter_ac_input_max only gates the AC-to-DC path (grid -> battery); PV
    charges the battery directly on the DC bus and is unaffected (EMHASS
    optimization.py's _add_hybrid_inverter_constraints: p_ac_dc is capped at
    inverter_ac_input_max * efficiency, independent of p_sto/p_pv). Not a
    fault by itself -- many systems are deliberately configured this way --
    but it means the power-balance checks below have zero grid-charging
    headroom to fall back on when PV is thin.
    """
    if not payload.get("inverter_is_hybrid"):
        return
    ac_input = payload.get("inverter_ac_input_max")
    if ac_input is not None and ac_input <= 0:
        report.add(
            Severity.INFO,
            "Battery can only be charged by PV, never by the grid",
            "inverter_ac_input_max is 0. If the battery is low and the PV "
            "forecast for the rest of the horizon is thin, expect the power "
            "deficit check below to fire.",
        )


def check_power_balance(payload: dict, report: Report) -> None:
    """Per-timestep check: can the configured limits physically supply the
    forecast load, and physically absorb the forecast PV surplus?

    This ignores state of charge carried over between timesteps -- it asks
    "even with every source/sink maxed out *this instant*, is there enough
    headroom" -- so it can only prove infeasibility, never feasibility. It
    mirrors the AC-bus balance in EMHASS optimization.py
    (``p_hybrid_inverter - p_def_sum - p_load + p_grid_neg + p_grid_pos == 0``)
    and the DC-bus balance (``p_pv - p_pv_curtailment + p_sto_pos + p_sto_neg
    == p_dc_ac - p_ac_dc``), collapsed to worst-case power caps.
    """
    pv = payload.get("pv_power_forecast")
    load = payload.get("load_power_forecast")
    if not isinstance(pv, list) or not isinstance(load, list):
        return
    n = min(len(pv), len(load))

    grid_import_max = payload.get("maximum_power_from_grid")
    grid_export_max = payload.get("maximum_power_to_grid")
    hybrid = bool(payload.get("inverter_is_hybrid"))
    inverter_output_max = payload.get("inverter_ac_output_max") if hybrid else None
    use_battery = bool(payload.get("set_use_battery"))
    charge_max = payload.get("battery_charge_power_max", 0.0) if use_battery else 0.0
    discharge_max = payload.get("battery_discharge_power_max", 0.0) if use_battery else 0.0

    deficits = []
    surpluses = []
    for t in range(n):
        pv_t, load_t = pv[t], load[t]
        if pv_t is None or load_t is None:
            continue

        # Deficit: can the AC bus meet this timestep's uncontrollable load at
        # all, ignoring deferrables entirely (a necessary condition -- adding
        # deferrable demand can only make a deficit worse, never better)?
        dc_side_supply = pv_t + discharge_max
        inverter_output = min(dc_side_supply, inverter_output_max) if hybrid else dc_side_supply
        supply = (grid_import_max or 0) + inverter_output
        if grid_import_max is not None and load_t - supply > 1e-6:
            deficits.append((t, load_t - supply, load_t, pv_t))

        # Surplus: after the battery soaks up what it can, can what's left of
        # the PV forecast get to the AC bus and either serve load or export?
        # Assumes PV curtailment is off (EMHASS's own default) -- if
        # "Compute PV curtailment" is enabled on the EMHASS add-on side, this
        # check does not apply and can be ignored.
        dc_surplus = max(0.0, pv_t - charge_max)
        inverter_headroom = inverter_output_max if hybrid else dc_surplus
        ac_available = min(dc_surplus, inverter_headroom) if hybrid else dc_surplus
        export_needed = ac_available - load_t
        cap = grid_export_max
        if hybrid and inverter_output_max is not None:
            cap = min(cap, inverter_output_max) if cap is not None else inverter_output_max
        if cap is not None and export_needed - cap > 1e-6:
            surpluses.append((t, export_needed - cap, pv_t, load_t))

    if deficits:
        worst = max(deficits, key=lambda d: d[1])
        report.add(
            Severity.CRITICAL,
            f"Unavoidable power deficit at {len(deficits)} timestep(s)",
            f"Worst at timestep {worst[0]}: load {worst[2]:g} W, PV {worst[3]:g} W, "
            f"short by {worst[1]:g} W even with grid import and battery discharge "
            "maxed out. This alone makes the problem infeasible, independent of "
            "any deferrable load.",
        )
    if surpluses:
        worst = max(surpluses, key=lambda s: s[1])
        report.add(
            Severity.CRITICAL,
            f"Unavoidable PV surplus at {len(surpluses)} timestep(s)",
            f"Worst at timestep {worst[0]}: PV {worst[2]:g} W, load {worst[3]:g} W, "
            f"{worst[1]:g} W over what battery charging + inverter/grid export "
            "can absorb. Infeasible unless PV curtailment is enabled on the "
            "EMHASS add-on side.",
        )


CHECKS = [
    check_companion_warnings,
    check_array_lengths,
    check_deferrable_windows,
    check_battery_soc,
    check_hybrid_inverter,
    check_power_balance,
]


def run_checks(payload: dict, warnings: list[str]) -> Report:
    report = Report()
    check_companion_warnings(payload, warnings, report)
    for check in CHECKS[1:]:
        check(payload, report)
    return report


# -- reporting ------------------------------------------------------------


def print_report(report: Report, last_run: dict) -> None:
    if last_run:
        status = last_run.get("status")
        infeasible = last_run.get("infeasible")
        print(f"Last run: status={status!r} infeasible={infeasible!r}")
        if last_run.get("error_message"):
            print(f"  error_message: {last_run['error_message']}")
        print()

    order = [Severity.CRITICAL, Severity.WARNING, Severity.INFO]
    if not report.findings:
        print(
            "No issues found by any check. See the module docstring: this can "
            "only prove infeasibility, not feasibility."
        )
        return

    for severity in order:
        findings = report.by_severity(severity)
        if not findings:
            continue
        print(f"=== {severity.value} ({len(findings)}) ===")
        for f in findings:
            print(f"- {f.title}")
            print(f"  {f.detail}")
        print()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    source = argv[1]
    if source == "-":
        text = sys.stdin.read()
    else:
        with Path(source).open(encoding="utf-8") as handle:
            text = handle.read()
    raw = json.loads(text)
    payload, warnings, last_run = load_payload(raw)
    if not payload:
        print(
            "No EMHASS payload found in the input. Expected either a "
            "diagnostics download (with a 'last_payload' key) or the "
            "'payload' attribute of the Last request to EMHASS sensor."
        )
        return 1
    report = run_checks(payload, warnings)
    print_report(report, last_run)
    return 1 if report.by_severity(Severity.CRITICAL) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

#!/usr/bin/env python3
"""Diagnose "EMHASS reported the optimisation problem as infeasible".

Standalone and dependency-free on purpose: stdlib only, no Home Assistant
imports and no relative imports. The integration imports it to name the cause
in its repair issue, and anyone hitting that issue can download this one file
and run it against a diagnostics download, with no Home Assistant or repo
install. ``scripts/check_infeasibility.py`` is a thin wrapper around it.

Where to get the input
-----------------------
* Settings -> Devices & services -> EMHASS Companion -> the device page ->
  the three-dot menu -> Download diagnostics. Point this script at the
  downloaded file.
* Or enable the (disabled-by-default) "Last request to EMHASS" sensor and
  save its ``payload`` attribute as JSON.

Usage
-----
    python3 infeasibility.py path/to/diagnostics.json
    python3 infeasibility.py path/to/payload.json
    cat diagnostics.json | python3 infeasibility.py -

What this is, and is not
-------------------------
This does not run EMHASS's solver and cannot prove a plan is feasible -- that
would mean re-implementing the whole MILP. What it does is check the specific
patterns that repeatedly turn out to be the real cause behind "infeasible"
once you dig into EMHASS's optimisation.py: a load pinned at timestep 0 that
the grid, PV and an empty battery cannot supply, a deferrable load whose
window is narrower than its own run time, a battery that can only ever be
charged by PV, and forecast/grid/inverter limits that make an unavoidable
power deficit or surplus at some timestep. Every check below names the
constraint in EMHASS it mirrors. A clean report is good evidence, not a
guarantee -- the power-balance checks look at each timestep in isolation and
only track the battery's stored energy at timestep 0, so they can miss
infeasibilities that only appear once SOC is tracked through the horizon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path
import sys
from typing import Any


class Severity(Enum):
    CRITICAL = "CRITICAL"  # would make EMHASS's own constraints unsatisfiable
    WARNING = "WARNING"  # plausible cause, needs the data to judge
    INFO = "INFO"  # not a cause by itself, but shapes how the others read


@dataclass(frozen=True)
class Finding:
    """One thing a check found.

    ``code`` is stable and picks the repair issue's translation; the
    ``placeholders`` fill that translation, already formatted, so the text
    stays translatable. ``title`` and ``detail`` are English, for the log and
    the CLI only.
    """

    severity: Severity
    code: str
    title: str
    detail: str
    placeholders: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "title": self.title,
            "detail": self.detail,
            "placeholders": dict(self.placeholders),
        }


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(
        self,
        severity: Severity,
        code: str,
        title: str,
        detail: str,
        placeholders: dict[str, str] | None = None,
    ) -> None:
        self.findings.append(Finding(severity, code, title, detail, placeholders or {}))

    def by_severity(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity is severity]


# The order headline() picks a CRITICAL in: most specific cause first. A
# timestep-0 deficit caused by pins says exactly what to change; a generic
# per-timestep deficit says only "something is short somewhere".
HEADLINE_ORDER = (
    "timestep0_deficit",
    "battery_energy",
    "pin_vs_window",
    "window_too_short",
    "power_deficit",
    "pv_surplus",
    "array_length",
    "forecast_nan",
)


# -- formatting ---------------------------------------------------------------


def format_power(watts: float) -> str:
    """Watts as people read them on a meter: W below 1 kW, kW with one decimal above."""
    if abs(watts) < 1000:
        return f"{watts:.0f} W"
    return f"{watts / 1000:.1f} kW"


def format_percent(fraction: float) -> str:
    return f"{fraction * 100:.1f} %"


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


def load_names_from(raw: dict) -> list[str] | None:
    """Load names in ``P_deferrable{k}`` order, when a diagnostics download has them.

    ``deferrable_order`` lists subentry ids; the ``loads`` section maps those
    to the names the user gave each load. Anything missing falls back to
    ``Deferrable k`` in :func:`_load_name`.
    """
    order = raw.get("deferrable_order")
    loads = raw.get("loads")
    if not isinstance(order, list) or not isinstance(loads, list):
        return None
    by_id = {
        item.get("subentry_id"): item.get("name")
        for item in loads
        if isinstance(item, dict) and item.get("name")
    }
    return [by_id.get(subentry_id) or f"Deferrable {k}" for k, subentry_id in enumerate(order)]


# -- payload helpers ----------------------------------------------------------


def _load_name(load_names: list[str] | None, k: int) -> str:
    if load_names and k < len(load_names) and load_names[k]:
        return load_names[k]
    return f"Deferrable {k}"


def _at(value: Any, t: int) -> float | None:
    """A scalar setting, or one entry of a per-timestep list of it.

    ``maximum_power_from_grid`` is either, depending on whether a network
    profile turned it into a per-timestep capacity array.
    """
    if isinstance(value, list):
        if t < len(value) and isinstance(value[t], (int, float)):
            return float(value[t])
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _item(values: Any, k: int, default: Any = 0) -> Any:
    if isinstance(values, list) and k < len(values) and values[k] is not None:
        return values[k]
    return default


def _is_mpc(payload: dict) -> bool:
    """Whether this is a naive-mpc-optim request.

    Only MPC sends ``prediction_horizon``. Its timestep 0 is "now": the pins
    describe what is running and ``soc_init`` is the live battery reading. A
    day-ahead run has no pins, and its start is not now, so the timestep-0
    checks are skipped there.
    """
    return "prediction_horizon" in payload


def _horizon_length(payload: dict) -> int | None:
    for key in ("pv_power_forecast", "load_power_forecast", "load_cost_forecast"):
        if isinstance(payload.get(key), list):
            return len(payload[key])
    horizon = payload.get("prediction_horizon")
    return horizon if isinstance(horizon, int) else None


def _battery_energy_cap_w(payload: dict) -> float | None:
    """The most the battery can discharge over timestep 0, from what it holds.

    EMHASS's SOC dynamics (``soc[t+1] = soc[t] - p_sto_pos / eta_dis * dt /
    capacity``) and its ``soc >= soc_min`` bound together cap the discharge
    power at ``(soc_init - soc_min) * capacity * eta_dis / dt``. At 1 % SOC
    that is a few hundred watts, whatever the inverter's power rating says.
    None when the payload does not say enough to work it out.
    """
    soc_init = payload.get("soc_init")
    soc_min = payload.get("battery_minimum_state_of_charge", 0.0)
    capacity = payload.get("battery_nominal_energy_capacity")
    step_minutes = payload.get("optimization_time_step")
    if soc_init is None or capacity is None or not step_minutes:
        return None
    efficiency = payload.get("battery_discharge_efficiency", 1.0) or 1.0
    stored_wh = max(0.0, soc_init - (soc_min or 0.0)) * capacity
    return stored_wh * efficiency / (step_minutes / 60)


def _battery_discharge_w(payload: dict, *, t: int) -> float:
    """Battery discharge available at timestep ``t``.

    The power rating, and at timestep 0 of an MPC run also the stored-energy
    cap from :func:`_battery_energy_cap_w`. Later timesteps keep the rating
    alone: the SOC by then depends on what the solver chose before.
    """
    if not payload.get("set_use_battery"):
        return 0.0
    rated = float(payload.get("battery_discharge_power_max", 0.0) or 0.0)
    if t == 0 and _is_mpc(payload):
        cap = _battery_energy_cap_w(payload)
        if cap is not None:
            return min(rated, cap)
    return rated


def _inverter_output_w(payload: dict, dc_supply_w: float) -> float:
    """What reaches the AC bus from PV and battery together.

    A hybrid inverter caps it at ``inverter_ac_output_max``; otherwise PV and
    battery each have their own path and the sum passes through.
    """
    if payload.get("inverter_is_hybrid"):
        output_max = payload.get("inverter_ac_output_max")
        if output_max is not None:
            return min(dc_supply_w, output_max)
    return dc_supply_w


def _pv_at(payload: dict, t: int) -> float:
    pv = payload.get("pv_power_forecast")
    value = _at(pv, t) if isinstance(pv, list) else None
    return value or 0.0


def _load_at(payload: dict, t: int) -> float | None:
    """The house load forecast at ``t``, or None when it was not sent.

    Not sent is the normal shape when EMHASS builds the load forecast itself
    (its ML forecaster, for instance), so a check reading this has to treat
    the house load as unknown rather than zero.
    """
    load = payload.get("load_power_forecast")
    return _at(load, t) if isinstance(load, list) else None


# -- checks ---------------------------------------------------------------


def check_companion_warnings(payload: dict, warnings: list[str], report: Report) -> None:
    """Surface the integration's own warnings first.

    These already catch a load whose window doesn't fit its run time and a
    forecast that runs out before the horizon does (payload.py's own
    resolve_load_window and the forecast-coverage check) -- no need to
    recompute what the integration already told you at request time.
    """
    for message in warnings:
        report.add(
            Severity.WARNING, "companion_warning", "Flagged when the request was built", message
        )


def check_timestep0_deficit(
    payload: dict, report: Report, load_names: list[str] | None = None
) -> None:
    """Can timestep 0 supply the loads pinned there, on top of the house?

    ``def_current_power`` is a hard equality on timestep 0 in EMHASS
    (``p_deferrable[k][0] == def_current_power[k]``), so a pinned load is not
    something the solver can move out of the way. If the pins plus the house
    load exceed grid import + PV + what the battery can actually deliver right
    now, nothing else in the problem matters. This is what happened on
    2026-10-01 22:00: car 10 kW and dishwasher 1.1 kW pinned against a 7.1 kW
    live fuse limit, no PV, and a battery at 1 %.

    MPC only: a day-ahead request carries no pins.
    """
    if not _is_mpc(payload):
        return
    powers = payload.get("def_current_power")
    if not isinstance(powers, list):
        return
    pins = [(k, float(p)) for k, p in enumerate(powers) if isinstance(p, (int, float)) and p > 0]
    if not pins:
        return
    grid = _at(payload.get("maximum_power_from_grid"), 0)
    if grid is None:
        return  # left to EMHASS's own stored configuration; nothing to compare against

    pv = _pv_at(payload, 0)
    house = _load_at(payload, 0)
    battery = _battery_discharge_w(payload, t=0)
    available = grid + _inverter_output_w(payload, pv + battery)
    pinned = sum(p for _k, p in pins)
    needed = pinned + (house or 0.0)
    if needed - available <= 1e-6:
        return

    pin_list = ", ".join(f"{_load_name(load_names, k)} {format_power(p)}" for k, p in pins)
    soc_init = payload.get("soc_init")
    if payload.get("set_use_battery") and soc_init is not None:
        battery_text = f"{format_power(battery)} (SOC {format_percent(soc_init)})"
    else:
        battery_text = format_power(battery)
    house_text = format_power(house) if house is not None else "?"
    at_least = "" if house is not None else "at least "
    report.add(
        Severity.CRITICAL,
        "timestep0_deficit",
        "Timestep 0 cannot supply the loads pinned to it",
        f"Timestep 0 needs {at_least}{format_power(needed)} ({pin_list} pinned as "
        f"already running, house {house_text}), but at most {format_power(available)} "
        f"is available (grid {format_power(grid)}, PV {format_power(pv)}, battery "
        f"{battery_text}). EMHASS holds def_current_power fixed at timestep 0, so "
        "it cannot move these loads out of the way.",
        {
            "needed": format_power(needed),
            "pins": pin_list,
            "house": house_text,
            "available": format_power(available),
            "grid": format_power(grid),
            "pv": format_power(pv),
            "battery": battery_text,
        },
    )


def check_battery_energy(payload: dict, report: Report) -> None:
    """Is timestep 0 short only because the battery is nearly empty?

    The plain power-balance check used to credit the battery with its full
    discharge rating at every timestep. At timestep 0 of an MPC run the
    stored energy is known, and it is the tighter limit whenever SOC is low.
    This fires only when that difference decides it: the house load fits
    with the rating, but not with what the battery actually holds.

    MPC only: a day-ahead run's soc_init is not "now".
    """
    if not _is_mpc(payload) or not payload.get("set_use_battery"):
        return
    house = _load_at(payload, 0)
    grid = _at(payload.get("maximum_power_from_grid"), 0)
    cap = _battery_energy_cap_w(payload)
    if house is None or grid is None or cap is None:
        return
    rated = float(payload.get("battery_discharge_power_max", 0.0) or 0.0)
    pv = _pv_at(payload, 0)
    battery = min(rated, cap)
    with_rating = grid + _inverter_output_w(payload, pv + rated)
    with_energy = grid + _inverter_output_w(payload, pv + battery)
    if house - with_energy <= 1e-6 or house - with_rating > 1e-6:
        return

    soc_init = payload["soc_init"]
    soc_min = payload.get("battery_minimum_state_of_charge", 0.0) or 0.0
    report.add(
        Severity.CRITICAL,
        "battery_energy",
        "The battery is too empty to cover timestep 0",
        f"At {format_percent(soc_init)} SOC (minimum {format_percent(soc_min)}) the "
        f"battery can deliver at most {format_power(battery)} over timestep 0, not its "
        f"rated {format_power(rated)}. With grid {format_power(grid)} and PV "
        f"{format_power(pv)}, the house load of {format_power(house)} cannot be covered.",
        {
            "soc": format_percent(soc_init),
            "soc_min": format_percent(soc_min),
            "battery": format_power(battery),
            "rated": format_power(rated),
            "grid": format_power(grid),
            "pv": format_power(pv),
            "house": format_power(house),
        },
    )


def check_pin_vs_window(payload: dict, report: Report, load_names: list[str] | None = None) -> None:
    """A load pinned at timestep 0 that the same request also says may not run.

    The contradiction ``payload._park`` exists to prevent, so this catches a
    regression rather than a user setting:

    * Zero hours with a pin: EMHASS must deliver the pinned power at
      timestep 0 *and* total zero energy. No solution exists -- CRITICAL.
    * A window that starts after timestep 0: EMHASS widens a pinned load's
      window mask to let timestep 0 through, so this is not fatal by itself,
      but Companion should have parked the load -- WARNING.
    """
    powers = payload.get("def_current_power")
    if not isinstance(powers, list):
        return
    hours = payload.get("operating_hours_of_each_deferrable_load")
    timesteps = payload.get("operating_timesteps_of_each_deferrable_load")
    starts = payload.get("start_timesteps_of_each_deferrable_load")
    for k, power in enumerate(powers):
        if not isinstance(power, (int, float)) or power <= 0:
            continue
        name = _load_name(load_names, k)
        h = _item(hours, k, None)
        steps = _item(timesteps, k, None)
        start = _item(starts, k, 0)
        placeholders = {
            "name": name,
            "power": format_power(power),
            "start": str(start),
            "hours": f"{h or 0:g}",
        }
        if h == 0 or steps == 0:
            report.add(
                Severity.CRITICAL,
                "pin_vs_window",
                f"{name} is pinned at timestep 0 but asked for no run time",
                f"def_current_power holds {name} at {format_power(power)} for timestep 0, "
                "but its operating hours are 0, so EMHASS must also deliver zero energy. "
                "No plan satisfies both. This is a Companion bug: a parked load must "
                "report itself off.",
                placeholders,
            )
        elif start > 0:
            report.add(
                Severity.WARNING,
                "pin_vs_window",
                f"{name} is pinned at timestep 0 but its window starts at timestep {start}",
                f"def_current_power holds {name} at {format_power(power)} for timestep 0, "
                f"while its window only opens at timestep {start}. EMHASS widens the "
                "window to let a pinned load through, so this is not fatal on its own, "
                "but Companion would normally have parked the load.",
                placeholders,
            )


def check_deferrable_windows(
    payload: dict, report: Report, load_names: list[str] | None = None
) -> None:
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

    An end timestep of 0 is EMHASS's sentinel for "no end constraint", so the
    window then runs to the end of the horizon.
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
    horizon = _horizon_length(payload)

    for i in range(n):
        h = _item(hours, i, 0)
        if not h:
            continue
        needed = max(1, round(h * 60 / step_minutes)) - _item(completed, i, 0)
        start = _item(starts, i, 0)
        end = _item(ends, i, 0)
        if end == 0:
            if horizon is None:
                continue
            end = horizon
        window = end - start
        power = _item(powers, i, None)
        name = _load_name(load_names, i)
        label = f"{name} ({power:g} W)" if isinstance(power, (int, float)) else name
        if window < needed:
            report.add(
                Severity.CRITICAL,
                "window_too_short",
                f"{label} doesn't fit its window",
                f"Needs {needed} timestep(s) to run {h:g} h, but its window "
                f"(timesteps {start}-{end}) only has {max(window, 0)}. EMHASS "
                "reports this as a whole-problem infeasibility with no hint "
                "which load caused it.",
                {
                    "name": name,
                    "hours": f"{h:g}",
                    "needed": str(needed),
                    "start": str(start),
                    "end": str(end),
                    "available": str(max(window, 0)),
                },
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
        described = ", ".join(f"{k} {v}" for k, v in lengths.items())
        report.add(
            Severity.CRITICAL,
            "array_length",
            "Forecast arrays disagree on length",
            f"{lengths}. These are meant to describe the same horizon at the "
            "same timestep and must be the same length.",
            {"details": described},
        )
    for key, arr in ((k, payload.get(k)) for k in forecast_keys):
        if not isinstance(arr, list):
            continue
        bad = [i for i, v in enumerate(arr) if v is None or (isinstance(v, float) and v != v)]
        if bad:
            report.add(
                Severity.CRITICAL,
                "forecast_nan",
                f"{key} has {len(bad)} null/NaN value(s)",
                f"First at index {bad[0]}. A forecast source that returned "
                "nothing for part of the horizon produces exactly this.",
                {"key": key, "count": str(len(bad)), "first": str(bad[0])},
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
                    "array_length",
                    f"{key} has {len(arr)} entries, expected {n}",
                    "number_of_deferrable_loads and every per-load array must "
                    "agree on length; EMHASS indexes some of these with no "
                    "bounds check.",
                    {"details": f"{key} {len(arr)}, expected {n}"},
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
                "soc_band",
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
            "hybrid_inverter",
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
    headroom" -- so it can only prove infeasibility, never feasibility. The
    one exception is timestep 0 of an MPC run, where the battery's stored
    energy is known and caps its discharge (see _battery_discharge_w). It
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

    hybrid = bool(payload.get("inverter_is_hybrid"))
    inverter_output_max = payload.get("inverter_ac_output_max") if hybrid else None
    use_battery = bool(payload.get("set_use_battery"))
    charge_max = payload.get("battery_charge_power_max", 0.0) if use_battery else 0.0

    deficits = []
    surpluses = []
    for t in range(n):
        pv_t, load_t = pv[t], load[t]
        if pv_t is None or load_t is None:
            continue
        grid_import_max = _at(payload.get("maximum_power_from_grid"), t)
        grid_export_max = _at(payload.get("maximum_power_to_grid"), t)

        # Deficit: can the AC bus meet this timestep's uncontrollable load at
        # all, ignoring deferrables entirely (a necessary condition -- adding
        # deferrable demand can only make a deficit worse, never better)?
        dc_side_supply = pv_t + _battery_discharge_w(payload, t=t)
        inverter_output = _inverter_output_w(payload, dc_side_supply)
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
            "power_deficit",
            f"Unavoidable power deficit at {len(deficits)} timestep(s)",
            f"Worst at timestep {worst[0]}: load {worst[2]:g} W, PV {worst[3]:g} W, "
            f"short by {worst[1]:g} W even with grid import and battery discharge "
            "maxed out. This alone makes the problem infeasible, independent of "
            "any deferrable load.",
            {
                "count": str(len(deficits)),
                "timestep": str(worst[0]),
                "house": format_power(worst[2]),
                "pv": format_power(worst[3]),
                "short": format_power(worst[1]),
            },
        )
    if surpluses:
        worst = max(surpluses, key=lambda s: s[1])
        report.add(
            Severity.CRITICAL,
            "pv_surplus",
            f"Unavoidable PV surplus at {len(surpluses)} timestep(s)",
            f"Worst at timestep {worst[0]}: PV {worst[2]:g} W, load {worst[3]:g} W, "
            f"{worst[1]:g} W over what battery charging + inverter/grid export "
            "can absorb. Infeasible unless PV curtailment is enabled on the "
            "EMHASS add-on side.",
            {
                "count": str(len(surpluses)),
                "timestep": str(worst[0]),
                "pv": format_power(worst[2]),
                "house": format_power(worst[3]),
                "excess": format_power(worst[1]),
            },
        )


def run_checks(payload: dict, warnings: list[str], load_names: list[str] | None = None) -> Report:
    report = Report()
    check_companion_warnings(payload, warnings, report)
    check_array_lengths(payload, report)
    check_timestep0_deficit(payload, report, load_names)
    check_battery_energy(payload, report)
    check_pin_vs_window(payload, report, load_names)
    check_deferrable_windows(payload, report, load_names)
    check_battery_soc(payload, report)
    check_hybrid_inverter(payload, report)
    check_power_balance(payload, report)
    return report


def diagnose(
    payload: dict,
    *,
    load_names: list[str] | None = None,
    warnings: list[str] | None = None,
) -> list[Finding]:
    """Every finding for an infeasible request, in check order.

    ``load_names`` maps ``P_deferrable{k}`` to the names the user gave the
    loads; without it findings say ``Deferrable k``.
    """
    return run_checks(payload, warnings or [], load_names).findings


def headline(findings: list[Finding]) -> Finding | None:
    """The CRITICAL finding that best explains the failure, if any.

    Picked by :data:`HEADLINE_ORDER`, most specific cause first; a CRITICAL
    with a code not listed there comes after every listed one.
    """
    critical = [f for f in findings if f.severity is Severity.CRITICAL]
    if not critical:
        return None

    def rank(finding: Finding) -> int:
        try:
            return HEADLINE_ORDER.index(finding.code)
        except ValueError:
            return len(HEADLINE_ORDER)

    return min(critical, key=rank)


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

    top = headline(report.findings)
    if top is not None:
        print(f"Most likely cause: {top.title}")
        print()

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
    report = run_checks(payload, warnings, load_names_from(raw))
    print_report(report, last_run)
    return 1 if report.by_severity(Severity.CRITICAL) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

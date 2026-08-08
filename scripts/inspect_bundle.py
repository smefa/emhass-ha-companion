#!/usr/bin/env python3
"""Triage a support bundle downloaded from EMHASS Companion.

Standalone and dependency-free on purpose: this is meant to be copy-pasted
and run by anyone attaching a bundle to an issue, not just from a dev
checkout -- the same reasoning as ``check_infeasibility.py``, which this
mirrors in style deliberately rather than sharing an implementation with (the
in-bundle ``triage`` section computes a handful of the same cheap facts, at
dump time; the coupling that sharing code between an add-on-side dict and
this offline script would create is not worth it for checks this cheap).

Where to get the input
-----------------------
Settings -> Devices & services -> EMHASS Companion -> the device page -> the
three-dot menu -> Download diagnostics. Point this script at the downloaded
file.

Usage
-----
    python3 inspect_bundle.py path/to/diagnostics.json
    cat diagnostics.json | python3 inspect_bundle.py -

What this is, and is not
-------------------------
This reads the bundle a maintainer would otherwise read by eye: is the
backend reachable, does every entity id the config points at actually exist
and hold a sane value, has a run ever succeeded, is the plan stale, does a
forecast source stop short of the others, did a custom profile fail to load,
and is a deferrable load or load group internally consistent. It does not
run EMHASS's solver -- for "the optimisation problem is infeasible"
specifically, run ``check_infeasibility.py`` against the same bundle (its
``last_payload`` key is exactly what that script wants); this script points
at it rather than re-deriving the same analysis.

Every check below reads defensively: a bundle from a slightly older release
that does not yet have one of the newer sections just produces no findings
for that check, not a crash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from enum import Enum
import json
from pathlib import Path
import re
import sys

# Mirrors const.MIN_EMHASS_VERSION -- duplicated rather than imported because
# this script must keep working outside a full checkout, and const.py pulls
# in the rest of the integration's package layout.
MIN_EMHASS_VERSION = "0.17.9"

# Mirrors const.DEFAULT_MPC_INTERVAL / const.STALE_PLAN_FACTOR, used only as
# the fallback when a bundle's own entry.options does not carry a configured
# mpc_interval_minutes (an older bundle, or a battery-less config that never
# set it explicitly).
DEFAULT_MPC_INTERVAL_MINUTES = 15
STALE_PLAN_FACTOR = 2

# Config-path field names that EMHASS reads as a number. power_sensor is
# deliberately excluded here even though it usually is one -- it also
# accepts a plain on/off binary_sensor (deferrable.state_to_power), so a
# non-numeric state there is not by itself a fault.
NUMERIC_STRICT_FIELDS = {"soc_entity", "pv_entity", "house_load_total_entity", "temperature_sensor"}
# Fields EMHASS expects in watts. A entity reporting kW here is the "off by
# 1000" bug the support-bundle plan calls out by name.
NUMERIC_POWER_FIELDS = {"pv_entity", "house_load_total_entity", "power_sensor"}

LOAD_SUBENTRY_TYPES = ("deferrable_load", "thermal_load")

# A forecast falling this far short of the horizon end is treated as "stops
# short" rather than as the ordinary off-by-one-timestep difference between a
# source that lands on the closing boundary and one that stops just inside it.
FORECAST_GAP_THRESHOLD = timedelta(hours=1)

# Mirrors const.DEFAULT_HORIZON_HOURS, restated rather than imported so this
# script keeps running outside a checkout. Only reached for a bundle whose
# options predate the horizon being configurable.
DEFAULT_HORIZON_HOURS = 24

# Mirrors const.ML_MIN_HISTORY_DAYS: how long a load sensor must have been
# recorded before mlforecaster can be fitted against it at all.
ML_MIN_HISTORY_DAYS = 9


class Severity(Enum):
    CRITICAL = "CRITICAL"  # a run will fail, or already has
    WARNING = "WARNING"  # plausible cause, needs the data to judge
    INFO = "INFO"  # not a fault, context worth stating anyway


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


# -- helpers ------------------------------------------------------------


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in re.split(r"[.\-+]", version):
        match = re.match(r"\d+", chunk)
        if not match:
            break
        parts.append(int(match.group()))
    return tuple(parts)


def _version_at_least(candidate: str, minimum: str) -> bool:
    """Whether ``candidate`` is at least ``minimum``.

    A parse failure (a development build's odd version string) is treated as
    acceptable, same call as util.version_at_least makes on the integration
    side -- refusing to judge is better than a false alarm.
    """
    try:
        return _version_tuple(candidate) >= _version_tuple(minimum)
    except (TypeError, ValueError):
        return True


def _fields_referenced(referenced_by: list[str]) -> set[str]:
    """The config field name(s) an entity id was found under.

    ``referenced_by`` paths look like ``options.soc_entity`` or
    ``subentries[thermal_load:Heat pump].data.temperature_sensor[0]`` -- the
    trailing identifier, stripped of any list index, is the field name.
    """
    fields: set[str] = set()
    for path in referenced_by:
        match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[\d+\])?$", path)
        if match:
            fields.add(match.group(1))
    return fields


def _window_hours(earliest: str, latest: str) -> float | None:
    try:
        start = time.fromisoformat(earliest)
        end = time.fromisoformat(latest)
    except ValueError:
        return None
    start_minutes = start.hour * 60 + start.minute
    end_minutes = end.hour * 60 + end.minute
    if end_minutes <= start_minutes:  # window crosses midnight
        end_minutes += 24 * 60
    return (end_minutes - start_minutes) / 60


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _horizon_end(bundle: dict) -> datetime | None:
    """When the window the last optimisation solved runs out.

    Anchored on the plan's own generation time rather than on the clock: the
    bundle may be read days after it was downloaded, and every series in it
    was gathered relative to that run, not to whenever someone gets round to
    triaging the issue.
    """
    generated = _parse_timestamp((bundle.get("plan") or {}).get("generated_at") or "")
    if generated is None:
        return None
    options = (bundle.get("entry") or {}).get("options") or {}
    try:
        hours = float(options.get("horizon_hours", DEFAULT_HORIZON_HOURS))
    except (TypeError, ValueError):
        hours = DEFAULT_HORIZON_HOURS
    return generated + timedelta(hours=hours)


# -- checks ---------------------------------------------------------------


def check_backend(bundle: dict, report: Report) -> None:
    backend = bundle.get("backend") or {}
    error = backend.get("version_error") or backend.get("config_error")
    if error:
        report.add(Severity.CRITICAL, "EMHASS backend unreachable", str(error))
        return
    version = backend.get("version")
    if version and not _version_at_least(str(version), MIN_EMHASS_VERSION):
        report.add(
            Severity.CRITICAL,
            f"EMHASS reports version {version}, below the minimum this integration supports",
            f"Needs at least {MIN_EMHASS_VERSION} -- the JSON plan API this integration relies "
            "on (GET /api/v1/plan, /api/v1/last-run, /healthz) was added there.",
        )


def check_entities(bundle: dict, report: Report) -> None:
    """Re-derive entity health from the bundle rather than trusting its own
    ``triage`` section verbatim, and add the checks that section deliberately
    keeps out (non-numeric states, the kW/W unit mismatch) since those need
    more than the cheap pass diagnostics.py itself affords.
    """
    for record in bundle.get("entities") or []:
        if "error" in record:
            continue
        entity_id = record.get("entity_id", "?")
        referenced_by = ", ".join(record.get("referenced_by") or []) or "?"

        if not record.get("exists"):
            report.add(
                Severity.CRITICAL, f"{entity_id} does not exist", f"Referenced by {referenced_by}."
            )
            continue

        state = record.get("state")
        if state in ("unavailable", "unknown"):
            report.add(
                Severity.WARNING, f"{entity_id} is {state}", f"Referenced by {referenced_by}."
            )
            continue

        fields = _fields_referenced(record.get("referenced_by") or [])
        if fields & NUMERIC_STRICT_FIELDS and state is not None:
            try:
                float(state)
            except (TypeError, ValueError):
                report.add(
                    Severity.CRITICAL,
                    f"{entity_id} has a non-numeric state",
                    f"State is {state!r}; referenced by {referenced_by} as a field EMHASS reads "
                    "as a number.",
                )
        if fields & NUMERIC_POWER_FIELDS and record.get("unit_of_measurement") == "kW":
            report.add(
                Severity.WARNING,
                f"{entity_id} reports kW where EMHASS expects W",
                f"Referenced by {referenced_by}. Every value from this entity will be read "
                "1000x low unless something else compensates.",
            )


def check_last_run(bundle: dict, report: Report) -> None:
    last_run = bundle.get("last_run") or {}
    status = last_run.get("status")
    if not status or status == "no-run":
        report.add(
            Severity.WARNING,
            "No optimisation has ever completed",
            "last_run.status is empty or 'no-run'.",
        )
        return
    if status != "ok":
        report.add(
            Severity.CRITICAL,
            f"Last run did not succeed (status={status!r})",
            last_run.get("error_message") or "No error message reported.",
        )
        return
    if last_run.get("infeasible"):
        report.add(
            Severity.WARNING,
            "Last run reported the optimisation problem as infeasible",
            "Run check_infeasibility.py against this same bundle for the specific cause -- "
            "its last_payload key is exactly what that script wants.",
        )


def check_plan(bundle: dict, report: Report) -> None:
    plan = bundle.get("plan") or {}
    rows = plan.get("rows")
    if not rows:
        report.add(
            Severity.CRITICAL, "The plan has zero rows", "Nothing for the executor to act on."
        )
        return

    generated = _parse_timestamp(plan.get("generated_at") or "")
    if generated is None:
        return
    options = ((bundle.get("entry") or {}).get("options")) or {}
    mpc_interval = options.get("mpc_interval_minutes", DEFAULT_MPC_INTERVAL_MINUTES)
    stale_after = timedelta(minutes=mpc_interval * STALE_PLAN_FACTOR)
    age = datetime.now(UTC) - generated
    if age > stale_after:
        report.add(
            Severity.WARNING,
            f"The plan is {age} old",
            f"Stale relative to the configured MPC interval ({mpc_interval} min x "
            f"{STALE_PLAN_FACTOR} = {stale_after}). Measured against the clock this script runs "
            "on, not against when the bundle was downloaded -- a bundle inspected long after "
            "collection will always show as stale here, which is itself the honest answer to "
            "'is this plan still any good'.",
        )


def check_forecast_coverage(bundle: dict, report: Report) -> None:
    """Flag a forecast source that does not reach the end of the horizon.

    Measured against the horizon, never against the other sources. Sources
    legitimately run to different lengths -- Solcast offers four days of PV
    where a day-ahead market has published perhaps two of prices -- so
    comparing them to each other reports the healthy, ordinary case as a
    fault on most installs, and a checker that cries wolf is one nobody
    reads. What matters is only whether each source covers the window the
    optimisation actually solves.
    """
    series = bundle.get("series") or {}
    horizon_end = _horizon_end(bundle)
    if horizon_end is None:
        return

    for name, info in sorted(series.items()):
        end = (info or {}).get("end")
        parsed = _parse_timestamp(end) if end else None
        if parsed is None:
            continue
        gap = horizon_end - parsed
        if gap > FORECAST_GAP_THRESHOLD:
            report.add(
                Severity.WARNING,
                f"{name} stops {gap} short of the horizon",
                "EMHASS forward-fills a forecast that stops short, so the plan can look healthy "
                "while being built on a value that flat-lined for the tail of the horizon.",
            )


def check_inputs_reached_emhass(bundle: dict, report: Report) -> None:
    """Flag an input EMHASS was given no way to obtain.

    Not the same question as "is the series empty". This integration does not
    always compute a forecast itself: several profiles contribute *settings*
    instead of points and let EMHASS build the forecast server-side -- the
    load/sensor profile sends sensor_power_load_no_var_loads and a method
    name, pv/emhass_native sends weather_forecast_method: open-meteo. An
    empty local series is the ordinary, correct shape for those, and calling
    it a fault is the same cried-wolf mistake as comparing forecast lengths
    to each other.

    What actually matters is whether EMHASS ended up with the input at all,
    from either side of the wire: an array in the payload, or a method it can
    satisfy on its own. Neither is the real fault, and it leaves EMHASS
    falling back to whatever its own stored configuration happens to say --
    which is nothing like what the integration would have asked for.
    """
    payload = bundle.get("last_payload") or {}
    # (payload array, the method key that would let EMHASS build it itself)
    supplied_elsewhere = {
        "load_forecast": ("load_power_forecast", "load_forecast_method"),
        "pv_forecast": ("pv_power_forecast", "weather_forecast_method"),
        "buy_price": ("load_cost_forecast", "load_cost_forecast_method"),
        "sell_price": ("prod_price_forecast", "production_price_forecast_method"),
    }
    # "No solar" is a deliberate answer, not a missing PV forecast.
    if payload.get("set_use_pv") is False:
        supplied_elsewhere.pop("pv_forecast")

    series = bundle.get("series") or {}
    for name, (array_key, method_key) in sorted(supplied_elsewhere.items()):
        # Only judge a source the bundle actually reports on -- an older
        # bundle predating one of these names says nothing about it either way.
        if name not in series or (series[name] or {}).get("points"):
            continue
        if payload.get(array_key) or payload.get(method_key):
            continue
        report.add(
            Severity.CRITICAL,
            f"EMHASS was given no {name}",
            f"This integration sent no {array_key} array and named no {method_key}, so EMHASS "
            "fell back to whatever its own stored configuration says -- which is not what the "
            "integration would have asked for. Call emhass_companion.test_profile against the "
            "profile behind this source to see what it actually returned.",
        )


def check_forecast_method(bundle: dict, report: Report) -> None:
    """Flag a load forecast method that was asked for but not used.

    mlforecaster is negotiated rather than obeyed: it falls back to naive
    until a fit has succeeded against enough recorder history, and the only
    outward sign is that the payload names a different method than the
    options do.
    """
    requested = (
        (((bundle.get("entry") or {}).get("options") or {}).get("load") or {}).get(
            "profile_options"
        )
        or {}
    ).get("method")
    sent = (bundle.get("last_payload") or {}).get("load_forecast_method")
    if requested and sent and requested != sent:
        report.add(
            Severity.INFO,
            f"Load forecast method '{requested}' was configured, '{sent}' was used",
            "Expected, and self-resolving, while a newly configured mlforecaster waits for "
            f"{ML_MIN_HISTORY_DAYS} days of recorder history on its load sensor -- runs keep "
            "succeeding on the fallback meanwhile. Only worth pursuing if it persists past "
            "that, which would mean the fit itself is failing rather than waiting.",
        )


def check_profile_errors(bundle: dict, report: Report) -> None:
    errors = ((bundle.get("profiles") or {}).get("errors")) or {}
    if not errors:
        return
    custom_profiles = [cp for cp in (bundle.get("custom_profiles") or []) if "path" in cp]

    for path, reason in sorted(errors.items()):
        normalised = path.replace("\\", "/")
        match = next(
            (cp for cp in custom_profiles if normalised.endswith(cp["path"].replace("\\", "/"))),
            None,
        )
        detail = str(reason)
        if match and match.get("content"):
            detail = f"{reason}\n\n{match['content']}"
        report.add(Severity.CRITICAL, f"Profile failed to load: {path}", detail)


def check_subentries(bundle: dict, report: Report) -> None:
    subentries = bundle.get("subentries") or []
    entities = {
        record["entity_id"]: record
        for record in bundle.get("entities") or []
        if "entity_id" in record
    }
    load_ids = {
        item.get("subentry_id")
        for item in subentries
        if item.get("subentry_type") in LOAD_SUBENTRY_TYPES
    }

    for item in subentries:
        subentry_type = item.get("subentry_type")
        title = item.get("title", "?")
        data = item.get("data") or {}

        if subentry_type in LOAD_SUBENTRY_TYPES:
            power = data.get("nominal_power_w")
            if power is not None and power <= 0:
                report.add(
                    Severity.CRITICAL,
                    f"'{title}' has zero or negative nominal power",
                    "nominal_power_of_deferrable_loads must be positive for EMHASS to schedule "
                    "anything for this load.",
                )

            control_entity = data.get("control_entity")
            if (
                control_entity
                and control_entity in entities
                and not entities[control_entity].get("exists")
            ):
                report.add(
                    Severity.WARNING,
                    f"'{title}'s control_entity {control_entity} does not exist",
                    "The load is advisory only until this is fixed -- nothing will actually "
                    "switch it on or off.",
                )

            hours = data.get("operating_hours")
            earliest = data.get("earliest_start")
            latest = data.get("latest_end")
            if data.get("use_time_window") and hours and earliest and latest:
                window = _window_hours(earliest, latest)
                if window is not None and window < hours:
                    report.add(
                        Severity.CRITICAL,
                        f"'{title}'s window is narrower than its own run time",
                        f"Needs {hours:g}h but the window {earliest}-{latest} only has "
                        f"{window:g}h. EMHASS reports this as a whole-problem infeasibility with "
                        "no hint which load caused it.",
                    )

        elif subentry_type == "load_group":
            for member in data.get("load_subentry_ids") or []:
                if member not in load_ids:
                    report.add(
                        Severity.WARNING,
                        f"Load group '{title}' names a load that no longer exists",
                        f"subentry id {member} is not any current deferrable or thermal load.",
                    )


def check_nothing_to_optimise(bundle: dict, report: Report) -> None:
    options = ((bundle.get("entry") or {}).get("options")) or {}
    battery_enabled = bool((options.get("battery") or {}).get("use_battery"))
    has_loads = any(
        item.get("subentry_type") in LOAD_SUBENTRY_TYPES for item in bundle.get("subentries") or []
    )
    if not battery_enabled and not has_loads:
        report.add(
            Severity.INFO,
            "Nothing to optimise",
            "No battery and no deferrable/thermal loads configured -- EMHASS has nothing to "
            "schedule beyond following the forecast as-is.",
        )


CHECKS = [
    check_backend,
    check_entities,
    check_last_run,
    check_plan,
    check_forecast_coverage,
    check_inputs_reached_emhass,
    check_forecast_method,
    check_profile_errors,
    check_subentries,
    check_nothing_to_optimise,
]


def run_checks(bundle: dict) -> Report:
    report = Report()
    for check in CHECKS:
        check(bundle, report)
    return report


# -- reporting ------------------------------------------------------------


def print_summary(bundle: dict) -> None:
    env = bundle.get("environment") or {}
    backend = bundle.get("backend") or {}
    last_run = bundle.get("last_run") or {}
    plan = bundle.get("plan") or {}
    subentries = bundle.get("subentries") or []
    entities = bundle.get("entities") or []
    broken = sum(
        1
        for e in entities
        if "error" not in e
        and (not e.get("exists") or e.get("state") in ("unavailable", "unknown"))
    )

    print("=== Summary ===")
    print(
        f"Integration {env.get('integration_version', '?')} on Home Assistant "
        f"{env.get('home_assistant_version', '?')}"
    )
    print(
        f"EMHASS backend: {backend.get('url', '?')} ({backend.get('source', '?')}), "
        f"version {backend.get('version') or backend.get('version_error') or '?'}"
    )
    print(f"Last run: status={last_run.get('status')!r} infeasible={last_run.get('infeasible')!r}")
    print(f"Plan: {plan.get('rows', 0)} row(s), generated {plan.get('generated_at') or 'never'}")
    print(f"Subentries: {len(subentries)}, entities with problems: {broken}/{len(entities)}")
    print()


def print_report(report: Report) -> None:
    order = [Severity.CRITICAL, Severity.WARNING, Severity.INFO]
    if not report.findings:
        print("No issues found by any check.")
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
    bundle = json.loads(text)

    report = run_checks(bundle)
    print_summary(bundle)
    print_report(report)
    return 1 if report.by_severity(Severity.CRITICAL) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

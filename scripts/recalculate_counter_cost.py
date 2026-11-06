"""Preview legacy counter overcounts and optionally export corrected daily cost.

This offline recovery tool never edits Home Assistant storage or its database.
See docs/savings.md for assumptions, tariff format, and retirement criteria.
Retire this wrapper and its tests when affected versions are no longer supported;
the shared energy-counter arithmetic and its runtime regressions must remain.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime
import importlib.util
from itertools import pairwise
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

_COUNTER_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "emhass_companion"
    / "energy_counter.py"
)
_spec = importlib.util.spec_from_file_location("companion_energy_counter", _COUNTER_PATH)
_counter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_counter)


@dataclass(frozen=True)
class Sample:
    row: dict[str, str]
    when: datetime
    value: float | None


@dataclass(frozen=True)
class Excess:
    start: datetime
    when: datetime
    kwh: float


def timestamp(value: str) -> datetime:
    """Require explicit offsets so local dates and tariff boundaries are unambiguous."""
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError(f"Timestamp needs a UTC offset: {value}")
    return parsed


def read_history(path: Path) -> tuple[list[str], list[Sample]]:
    """Read one entity's chronological Home Assistant history CSV."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        if not {"entity_id", "state", "last_changed"} <= set(fields):
            raise ValueError(f"{path}: expected entity_id,state,last_changed columns")
        rows = list(reader)
    if not rows or len({row["entity_id"] for row in rows}) != 1:
        raise ValueError(f"{path}: expected exactly one entity with history")
    samples = []
    for row in rows:
        when = timestamp(row["last_changed"])
        if samples and when <= samples[-1].when:
            raise ValueError(f"{path}: timestamps must be strictly increasing")
        try:
            value = float(row["state"])
        except ValueError:
            value = None
        if value is not None and not math.isfinite(value):
            value = None
        samples.append(Sample(row, when, value))
    return fields, samples


def counter_excess(samples: list[Sample], day: date, zone: ZoneInfo, scale: float) -> list[Excess]:
    """Compare legacy reset-on-any-decrease with the shared corrected arithmetic."""
    previous = None
    accepted = None
    since = None
    excess = []
    for sample in samples:
        if sample.value is None:
            continue
        reading = sample.value * scale
        if reading < 0:
            raise ValueError("Negative counter readings need manual investigation")
        accepted, corrected = _counter.advance_counter(accepted, reading)
        if previous is not None:
            legacy = reading if reading < previous else reading - previous
            difference = legacy - corrected
            if abs(difference) > 1e-9 and sample.when.astimezone(zone).date() == day:
                if since.astimezone(zone).date() != day:
                    raise ValueError(
                        "An affected interval crosses midnight; reconstruct it manually"
                    )
                excess.append(Excess(since, sample.when, difference))
        previous = reading
        since = sample.when
    return excess


def price_excess(excess: list[Excess], path: Path) -> list[float]:
    """Price only intervals wholly covered by one explicitly supplied buy tariff."""
    tariffs = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"start", "end", "buy"} <= set(reader.fieldnames or []):
            raise ValueError("Prices CSV needs start,end,buy columns")
        for row in reader:
            start, end, buy = timestamp(row["start"]), timestamp(row["end"]), float(row["buy"])
            if end <= start or not math.isfinite(buy):
                raise ValueError("Prices must be finite with start < end")
            tariffs.append((start, end, buy))
    tariffs.sort()
    if any(left[1] > right[0] for left, right in pairwise(tariffs)):
        raise ValueError("Price intervals must not overlap")
    amounts = []
    for event in excess:
        matches = [buy for start, end, buy in tariffs if start <= event.start and event.when < end]
        if len(matches) != 1:
            raise ValueError(
                f"No single buy tariff covers {event.start.isoformat()} -> "
                f"{event.when.isoformat()}; supply complete tariffs or reconstruct manually"
            )
        amounts.append(event.kwh * matches[0])
    return amounts


def corrected_rows(
    costs: list[Sample],
    energy: list[Sample],
    excess: list[Excess],
    amounts: list[float],
    day: date,
    zone: ZoneInfo,
) -> list[dict[str, str]]:
    """Subtract only this day's excess cost; retain other rows and all columns."""
    daily = [sample for sample in costs if sample.when.astimezone(zone).date() == day]
    if not daily:
        raise ValueError("Cost history does not contain the requested local day")
    if excess and (daily[0].when > excess[0].start or daily[-1].when < excess[-1].when):
        raise ValueError("Cost history must bracket all affected intervals")
    if energy[0].when > daily[0].when or energy[-1].when < daily[-1].when:
        raise ValueError("Energy history must cover the cost export")
    result = []
    for sample in costs:
        row = dict(sample.row)
        if sample.value is not None and sample.when.astimezone(zone).date() == day:
            adjustment = sum(
                amount
                for event, amount in zip(excess, amounts, strict=True)
                if event.when <= sample.when
            )
            row["state"] = f"{sample.value - adjustment:.4f}"
        result.append(row)
    return result


def main(argv: list[str] | None = None) -> int:
    """Print a dry-run report unless a separate corrected CSV is explicitly requested."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--energy", required=True, type=Path)
    parser.add_argument("--day", required=True, type=date.fromisoformat)
    parser.add_argument("--timezone", required=True, type=ZoneInfo)
    parser.add_argument("--unit", choices=("Wh", "kWh", "MWh"), default="kWh")
    parser.add_argument("--prices", type=Path)
    parser.add_argument("--cost", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--confirm-legacy-tracking", action="store_true")
    args = parser.parse_args(argv)
    try:
        _, energy = read_history(args.energy)
        if not any(
            sample.value is not None and sample.when.astimezone(args.timezone).date() == args.day
            for sample in energy
        ):
            raise ValueError("Energy history does not contain the requested local day")
        excess = counter_excess(
            energy, args.day, args.timezone, {"Wh": 0.001, "kWh": 1, "MWh": 1000}[args.unit]
        )
        amounts = price_excess(excess, args.prices) if args.prices else None
        if args.output:
            if not args.cost or amounts is None or not args.confirm_legacy_tracking:
                raise ValueError(
                    "Export requires --cost, --prices and --confirm-legacy-tracking; "
                    "verify the old tracker saw every source reading without rebaselining"
                )
            fields, costs = read_history(args.cost)
            rows = corrected_rows(costs, energy, excess, amounts, args.day, args.timezone)
            with args.output.open("x", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
        report = {
            "day": args.day.isoformat(),
            "assumption": "Legacy tracker processed every reading without restart/rebaseline; import counter only",
            "excess_import_kwh": sum(event.kwh for event in excess),
            "excess_import_cost": sum(amounts) if amounts is not None else None,
            "events": [
                {
                    "start": event.start.isoformat(),
                    "at": event.when.isoformat(),
                    "excess_kwh": event.kwh,
                }
                for event in excess
            ],
            "corrected_csv": str(args.output) if args.output else None,
            "home_assistant_modified": False,
        }
        print(json.dumps(report, indent=2))
    except (OSError, ValueError) as err:
        parser.error(str(err))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

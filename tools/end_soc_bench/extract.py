"""Pull this house's own history out of Home Assistant's long-term statistics.

The end-SOC candidates are heuristics about *money*, and the only honest way to
rank them is against real prices, real load and real sun -- especially in
winter, which no summer week of live ``tests`` history can stand in for.
Recorder's hourly statistics keep years of it (states are purged after 36
days), so this writes one tidy CSV of hourly truth that the backtest replays.

Usage::

    python tools/end_soc_bench/extract.py --out data/house.csv

Reads ``mariadb_url`` from ``/config/secrets.yaml`` and shells out to the
``mysql`` client, which is what the VS Code add-on actually has -- there is no
MySQL driver installed for Python here.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import UTC, datetime
from pathlib import Path
import re
import subprocess

import yaml

SECRETS = Path("/config/secrets.yaml")

# Nordpool's entity id encodes precision, additional cost and VAT, so the same
# market lands in the recorder several times over. `3_06_025` is 1.25x spot at
# three decimals and reaches back to 2024-01 -- the longest, most precise
# series of the real market this house has. Everything else is derived from
# spot by the tariff, exactly as the companion's own tariff engine does it.
PRICE_ENTITY = "sensor.nordpool_kwh_se2_sek_3_06_025"
PRICE_DIVISOR = 1.25

PV_ENTITY = "sensor.total_dc_power"
LOAD_ENTITY = "sensor.load_power"

# Statistics rows put a mean in `mean` for measurements and the last value in
# `state` for anything the recorder treats as a total. The price sensor is
# stored as the latter (has_sum=1), the two power sensors as the former.
COLUMNS = {PRICE_ENTITY: "state", PV_ENTITY: "mean", LOAD_ENTITY: "mean"}


def _credentials() -> tuple[str, str, str, str]:
    url = yaml.safe_load(SECRETS.read_text())["mariadb_url"]
    match = re.match(r"mysql://([^:]+):([^@]+)@([^/]+)/([^?]+)", url)
    if match is None:
        raise SystemExit(f"Unrecognised recorder db_url: {url.split('@')[-1]}")
    return match.groups()  # type: ignore[return-value]


def _query(sql: str) -> list[list[str]]:
    user, password, host, database = _credentials()
    result = subprocess.run(
        ["mysql", "-h", host, "-u", user, f"-p{password}", database, "-N", "-B", "-e", sql],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise SystemExit(f"mysql failed: {result.stderr.strip()[:400]}")
    return [line.split("\t") for line in result.stdout.splitlines() if line]


def fetch(start: str, end: str) -> dict[datetime, dict[str, float]]:
    rows: dict[datetime, dict[str, float]] = defaultdict(dict)
    for entity, column in COLUMNS.items():
        sql = (
            f"select s.start_ts, s.{column} from statistics s "
            "join statistics_meta sm on s.metadata_id = sm.id "
            f"where sm.statistic_id = '{entity}' "
            f"and s.start_ts >= unix_timestamp('{start}') "
            f"and s.start_ts < unix_timestamp('{end}') order by s.start_ts;"
        )
        for stamp, value in _query(sql):
            if value in ("NULL", ""):
                continue
            when = datetime.fromtimestamp(float(stamp), UTC)
            rows[when][entity] = float(value)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2024-01-26")
    parser.add_argument("--end", default="2026-08-12")
    parser.add_argument("--out", default=str(Path(__file__).parent / "data" / "house.csv"))
    args = parser.parse_args()

    rows = fetch(args.start, args.end)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "spot", "pv_w", "load_w"])
        for when in sorted(rows):
            row = rows[when]
            # A gap in any of the three makes the hour unusable as truth, and
            # an interpolated price is a made-up market. Skip it; the backtest
            # only takes days that are complete anyway.
            if not all(entity in row for entity in COLUMNS):
                continue
            writer.writerow(
                [
                    when.isoformat(),
                    round(row[PRICE_ENTITY] / PRICE_DIVISOR, 4),
                    round(row[PV_ENTITY], 1),
                    round(row[LOAD_ENTITY], 1),
                ]
            )
            written += 1
    print(f"{written} complete hours -> {out}")


if __name__ == "__main__":
    main()

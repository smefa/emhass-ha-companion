"""Pull hourly battery SOC maxima out of long-term statistics.

Curtailment leaves no record of its own, but it can only happen with the
battery full. Hours whose SOC reached the top are dropped from the backtest
so a thrown-away surplus does not read as a forecast miss.

Usage (on the HA host, like ``end_soc_bench/extract.py``)::

    python -m tools.pv_caution_bench.extract --entity sensor.battery_soc
"""

from __future__ import annotations

import argparse
import csv
from datetime import UTC, datetime
from pathlib import Path

from tools.end_soc_bench.extract import _query

from .data import SOC


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entity", required=True, help="battery SOC sensor (percent)")
    parser.add_argument("--start", default="2024-09-01")
    parser.add_argument("--end", default="2030-01-01")
    parser.add_argument("--out", type=Path, default=SOC)
    args = parser.parse_args()

    sql = (
        "select s.start_ts, s.max from statistics s "
        "join statistics_meta sm on s.metadata_id = sm.id "
        f"where sm.statistic_id = '{args.entity}' "
        f"and s.start_ts >= unix_timestamp('{args.start}') "
        f"and s.start_ts < unix_timestamp('{args.end}') order by s.start_ts;"
    )
    rows = [(stamp, value) for stamp, value in _query(sql) if value not in ("NULL", "")]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "soc_max"])
        for stamp, value in rows:
            writer.writerow([datetime.fromtimestamp(float(stamp), UTC).isoformat(), value])
    print(f"{len(rows)} hours -> {args.out}")


if __name__ == "__main__":
    main()

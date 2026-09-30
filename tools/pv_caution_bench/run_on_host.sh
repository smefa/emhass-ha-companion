#!/usr/bin/env bash
# Run the PV auto-caution backtest on the Home Assistant host.
#
#   cd /config/emhass-ha-companion-dev
#   bash tools/pv_caution_bench/run_on_host.sh [sensor.battery_soc]
#
# Run it from the Studio Code Server terminal: the house.csv and SOC extracts
# need the mysql client and PyYAML that add-on has. Passing a battery SOC
# sensor is optional; with it, hours the battery was full (possible
# curtailment) are left out.
#
# Everything is written to tools/pv_caution_bench/data/ (gitignored); the
# report ends up in data/report.txt.

set -euo pipefail

cd "$(dirname "$0")/../.."
BENCH=tools/pv_caution_bench
DATA=$BENCH/data
HOUSE=tools/end_soc_bench/data/house.csv
SOLCAST_SRC=/config/solcast_solar/solcast.json
SOC_ENTITY=${1:-}

if [ -x .venv-test/bin/python ]; then
    PY=.venv-test/bin/python
else
    PY=python3
fi

mkdir -p "$DATA"

echo "== Copying Solcast cache"
cp "$SOLCAST_SRC" "$DATA/solcast.json"

# house.csv is shared with the end-SOC bench. Refresh it so it reaches today;
# its default --end is a fixed date.
echo "== Extracting hourly PV from long-term statistics"
"$PY" tools/end_soc_bench/extract.py --end "$(date -u -d tomorrow +%F)" --out "$HOUSE"

if [ -n "$SOC_ENTITY" ]; then
    echo "== Extracting battery SOC ($SOC_ENTITY)"
    "$PY" -m tools.pv_caution_bench.extract --entity "$SOC_ENTITY"
else
    rm -f "$DATA/soc.csv"
fi

echo "== Running backtest"
{
    echo "# run $(date -Iseconds) on $(git rev-parse --short HEAD 2>/dev/null || echo '?')"
    "$PY" -m tools.pv_caution_bench.bench
    echo
    echo "################ grid ################"
    "$PY" -m tools.pv_caution_bench.bench --grid --only 3
} 2>&1 | tee "$DATA/report.txt"

echo
echo "Report: $DATA/report.txt"

# PV auto-caution backtest

Phase 1 of [the PV auto-caution plan](../../docs/plan/pv_auto_caution_plan.md):
decide go/no-go before building anything user-facing. This is a
forecast-accuracy test, not a cost test. It replays the real controller,
`custom_components/emhass_companion/pv_caution.py`, over this house's Solcast
history.

The quick way, from the Studio Code Server terminal on the HA host (it writes
`data/report.txt`):

```bash
cd /config/emhass-ha-companion-dev
bash tools/pv_caution_bench/run_on_host.sh [sensor.<battery_soc>]
```

Step by step:

```bash
# on the HA host, in /config/emhass-ha-companion-dev
mkdir -p tools/pv_caution_bench/data
cp /config/solcast_solar/solcast.json tools/pv_caution_bench/data/
./.venv-test/bin/python tools/end_soc_bench/extract.py            # house.csv, if not already there
./.venv-test/bin/python -m tools.pv_caution_bench.extract --entity sensor.<battery_soc>   # optional
./.venv-test/bin/python -m tools.pv_caution_bench.bench
./.venv-test/bin/python -m tools.pv_caution_bench.bench --grid --only 3
```

Only the standard library is needed (plus `tzdata` on Windows). The controller
is loaded by file path, so Home Assistant does not have to be installed.
`data/` is gitignored.

## Inputs

- **Forecast:** `data/solcast.json`, the Solcast integration's cache. Sites
  are summed and the 30-minute periods averaged to hours. Use `--solcast` to
  point elsewhere.
- **Actual PV:** `pv_w` from `tools/end_soc_bench/data/house.csv` (hourly mean
  of the DC production sensor). Use `--house` to point elsewhere.
- **Curtailment (optional):** `data/soc.csv` from `extract.py`. Hours where
  the battery reached 97 % are dropped. Without it, curtailed hours count as
  forecast misses.

Actual PV is divided by the overall actual/P50 ratio over sunny hours
(printed as `raw actual/P50`). This removes the DC-vs-AC offset. Use `--scale`
to set the ratio yourself, for example `--scale 1.0` to keep raw values.

## Output

1. **Bias.** Actual/P50 and actual/P10 by season and local hour. `pos` is
   where actual lands on average: 0 = at P50, 1 = at P10. `<P10` and `>P90`
   are the share of hours outside the band (about 10 % each if calibrated).
2. **Persistence.** Days are split by their morning ratio (sunrise + 1 h to
   12:00). The table shows the median afternoon ratio and afternoon
   over-forecast share for "behind" and other days, plus the correlation of
   morning and afternoon ratios. Weak or no difference means **no-go**.
3. **Replay.** Each hour of each day, the controller steps on the hours so
   far. The rest of the day is then blended with that bias and scored:
   - `over`: kWh of planned sun that did not come (costly).
   - `under`: kWh of sun the plan did not count on (the cost of caution).
   - `trade`: over-forecast removed per kWh of under-forecast added,
     against P50. A fixed blend sets the bar.
   - `cost`: `2 x over + under` (`--over-weight`). It ranks the `--grid`
     rows.

   Figures are per decision, for all decisions and for afternoon decisions.

**Go** needs both: test 2 shows clear persistence, and in test 3
auto-caution trades clearly better than the fixed blends. It must cut
afternoon over-forecast without a similar rise in under-forecast.

## Limitations

- The cache holds the *last* forecast issued before each period, close to a
  nowcast. Errors are smaller than live, and P10 sits closer to P50. A go
  means "worth building", not "proven". Check it again against live P10/P50
  snapshots (shadow mode) before turning the switch on by default.
- It is unknown whether cached values carry past or current dampening.
- The controller measures against P50 and runs once per hour. That matches
  its live rate limit (`min_step_interval`).

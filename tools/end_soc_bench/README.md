# End-SOC backtest

A terminal-SOC rule cannot be judged by reading the number it returns. This
replays the candidates in `custom_components/emhass_companion/terminal.py`
against years of this house's own recorded history and scores each one in
kronor.

```bash
cd /config/emhass-ha-companion-dev
./.venv-test/bin/python tools/end_soc_bench/extract.py          # once, or to refresh
./.venv-test/bin/python -m tools.end_soc_bench.bench --season winter
```

## How it works

`extract.py` pulls hourly spot price, PV production and household load out of
Home Assistant's long-term statistics — the only place that keeps years rather
than the recorder's 36 days — and writes `data/house.csv`. The file is this
house's own consumption history and is deliberately **not** committed.

`world.py` turns a moment in that history into a decision: the truth of the
next 72 hours, and separately *what the run could actually have seen* at that
moment. That split is the point. Nordpool publishes tomorrow around 13:00, so a
06:00 run works from a price curve that stops tonight; the load series the live
entry passes is one horizon long and is a forecast, so a same-hour mean of the
previous week stands in for it. A backtest that hands the rule the truth
measures nothing anybody will ever run.

`dispatch.py` finds the cheapest possible battery dispatch by dynamic
programming over a discretised SOC, and splits it at the pin:

    total(pin) = cheapest way to reach it + cheapest way to live with it

So one forward and one backward pass price *every* possible pin at that moment.
The best of them is the oracle, each candidate's answer is a lookup, and

    regret = total(candidate's pin) - min(total)

is what the rule's mistake cost, in kronor, at that moment. The evaluation
window runs 48 h past the pin so that the arbitrary value put on whatever is
left in the battery at the far end sits two days away from the decision being
measured.

## Reading the table

`perfect` is the oracle's own pin scored through the same path. It must read
0.00; anything else means the harness is broken.

Mean regret says how a rule does on an ordinary day. **p90, p99 and max say
whether it ever ruins one**, which for a terminal condition matters more — the
failure mode of a bad rule is not being slightly wrong every day, it is
arriving empty at 17:00 on the coldest evening of the year. The `>2 SEK` column
is the share of decisions where the rule was more than two kronor worse than
perfect.

`mean spread between best and worst pin` is how much the choice was worth at
all. On a flat summer day every pin costs the same and no rule can win or lose;
those days dilute a mean, which is why the seasonal breakdown is the one to
read.

## What it does not model

- PV comes from the DC-side production sensor, so it is a few percent higher
  than what reaches the AC bus. Every candidate is flattered equally.
- Recorded load is what the house actually drew, including whatever the live
  EMHASS deferrable loads shifted at the time. It is treated as fixed.
- Curtailment is free and unlimited, exports never pay a negative price, and
  the inverter is a single efficiency per direction rather than a curve.
- EMHASS's own objective (dwell costs, stress cost, deferrable loads, its
  soft-constraint handling of `soc_final`) is not reproduced. This measures the
  *terminal condition*, holding everything else optimal, which is exactly the
  question the candidates disagree about.

See `PROPOSALS.md` for how to add a candidate without touching the integration.

## Scoring the End SOC reward factor

    ./.venv-test/bin/python -m tools.end_soc_bench.bench --rewards 0.3,0.5,0.8,1.0 --by calendar

For the active rule, `same_as_start` and `fixed_reserve`, this adds a
`<rule>@<factor>` row per factor. It models EMHASS 0.18.5's
`battery_soc_final_reward_factor`: the pin becomes a floor, each kWh above it
earns `factor x` the last visible import price, and the SOC the optimiser then
picks is charged its real cost-to-go (`Dispatcher.evaluate_with_reward`).

2026-10-10, 1620 decisions over 540 days, mean regret in SEK:

| | 0 | 0.3 | 0.5 | 0.8 | 1.0 |
|---|---|---|---|---|---|
| `shadow_centred` (shipped) | 0.68 | 0.70 | 0.88 | 1.34 | 2.55 |
| `same_as_start` | 1.38 | 1.16 | 1.17 | 1.50 | 2.61 |
| `fixed_reserve` | 1.91 | 1.18 | 1.03 | 1.37 | 2.56 |

No factor beats 0 for the shipped rule, so the default stays 0 and the field
is a manual knob. A factor of 1 is always bad: it pays the full import price
for energy that is only worth that if the house will certainly import it.

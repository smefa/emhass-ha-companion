# Writing a new end-SOC candidate

Everything a candidate needs is in `custom_components/emhass_companion/terminal.py`
— read its module docstring first, then `_Tail`, `_Sample`, `_required_soc`,
`_sale_credit` and the rules that already ship. A proposal is scored by
exactly the same harness, on exactly the same moments, as those rules.

## The contract

Create one module in `tools/end_soc_bench/proposals/`, e.g. `peak_guard.py`:

```python
KEY = "peak_guard"
LABEL = "Test 7 -- peak guard"


def compute(tail) -> EndSocDecision: ...
```

Rules the harness assumes and the integration will enforce later:

- **Pure Python, standard library only.** No numpy, no pandas. This runs inside
  Home Assistant on a Raspberry Pi every 15 minutes; budget a few tens of
  milliseconds, and say so in the docstring if you spend more.
- **Read-only on `tail`.** Every candidate shares one `_Tail` per run.
  Mutating it corrupts the rules that run after yours.
- **Never plan below `tail.reserve`, never outside `tail.clamp()`.** The reserve
  is the user's comfort floor and is not a variable to optimise.
- **Proxied inputs may not be trusted in your favour.** `sample.price_proxied`
  marks a price copied from yesterday because tomorrow's market has not opened;
  `sample.pv_proxied` marks PV inferred from the previous day. Both may make you
  *more* cautious and neither may make you less — see `_build_tail` and
  `_sale_credit` for the shape this takes.
- **Return an explanation.** `EndSocDecision.reason` is one sentence a user
  reads in the sensor once the rule is promoted; `details` is structured
  evidence. Write both for someone deciding whether to trust the number.
- **Deterministic.** Same tail in, same number out.

## Running the backtest

```bash
cd /config/emhass-ha-companion-dev
./.venv-test/bin/python -m tools.end_soc_bench.bench --proposals --every 8 \
    --hours 6,12,18,22 --socs 0.25,0.5,0.75
```

Useful flags: `--season winter|shoulder|summer`, `--by bench|calendar|month`
(how the report is sliced; `calendar` gives winter/spring/summer/autumn),
`--variants` (also score the knob sweeps in `variants.py`), `--start/--end`
(ISO dates), `--every N` to subsample decision moments, `--json out.json` for
per-decision detail. **Iterate with `--every 8` or a single season**; a full
sweep is over 3000 moments and takes about a minute and a half without
`--variants`.

Every candidate registered in `terminal.CANDIDATES` is scored, not only the
active one, so the shipped rules stay comparable after one is promoted.

`perfect` must always read 0.00 — it is the oracle scored through the same code
path, and a non-zero row there means the harness is broken, not that you won.

## What the numbers mean

`regret` is kronor lost against a controller that knew the future, for that one
decision. The mean says how a rule does on an average day; **p90/p99/max say
whether it ever ruins one**, which for a terminal condition matters more. A rule
that wins the mean by hoarding and loses 12 SEK on the coldest evening of the
year is not a better rule.

Beating the shipping rule (`night_cover`) on the mean *and* not being worse in
the tail is the bar. Report both. If your rule only wins in one season, say so
— that is a real finding, not a failure.

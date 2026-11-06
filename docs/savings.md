# Cost and savings

Two questions this answers that EMHASS on its own does not:

- **What did today actually cost, and what did the system save me?** Measured
  from your own meters and your own prices, not from the plan.
- **What will the next 24 hours cost, and save?** Priced off the current plan.

Turn it on under **Settings → Devices & services → EMHASS Companion →
Configure → Cost and savings**. There is one switch. The meters are read from
your Energy dashboard and listed on the screen, so for most people it is a
matter of glancing at the list and pressing Submit.

## What "savings" means here

A saving is always a comparison, so the thing being compared to has to be
stated. This settles three versions of your house, against the same prices, at
the same instants:

| | Solar | Battery |
|---|---|---|
| **Grid only** | no | no | Every kWh the house uses is bought. |
| **Solar only** | yes | no | Sun offsets load as it arrives; the surplus is exported. |
| **Actual** | yes | yes | What your meters recorded. |

With a battery enabled, the difference between each pair is one component:

```
Solar savings   = grid only  −  solar only
Battery savings = solar only −  actual
Savings today   = grid only  −  actual        (= the two above, added)
```

So **solar savings** is the sun you used yourself, valued at what you would
otherwise have paid for it, plus what you were paid for the rest. **Battery
savings** is everything the battery added on top: buying cheap and using it
dear, storing sun for the evening instead of exporting it — minus the export
income it gave up, and minus round-trip losses.

With **I have a battery** disabled, battery savings and storage carry are zero.
The solar-only world is the actual installation, so its baseline uses the
**measured grid cost**, including actual import/export transactions and their
prices. It does not simulate a second set of grid transactions from independent
house and solar measurements:

```
Solar-only baseline = actual grid cost
Savings today = solar savings          (battery savings = 0)
```

The independent load/PV model is retained as **`modeled_cost_solar_only`**.
Its signed difference from actual cost is **`accounting_difference`**, a
diagnostic rather than an additional savings component. For example, costs of
63.08 grid-only, 53.55 modelled solar-only and 46.23 actual now give a 46.23
solar-only baseline, **16.85 solar/total savings**, **0 battery savings**, and
the original **7.32 model discrepancy**. This does not claim that all 16.85
has been independently validated: the grid-only baseline still depends on
house-load measurement and price coverage. Negative discrepancies remain too.
For battery-enabled installations, the existing battery attribution remains
an estimate and can still include measurement and timing errors.

Round-trip losses need no term of their own. The battery meters show more
energy going in than coming out, and that gap flows straight through into the
number.

### One thing worth knowing

`Savings today` depends on your **house load**, your **grid meters** and your
**prices**. With a measured house-load source, it does not depend on solar
measurement. With a battery, solar measurement affects the component split;
without one, it affects only the independent model diagnostic. If house load
is instead derived from the energy balance, it inherits PV errors, including
upward bias when asynchronous events imply negative load that is clamped to
zero. In that configuration the total also depends on PV measurement.

## Days that end with a charged battery

A battery charged at 03:00 and emptied at 19:00 is one transaction that fits
inside a day. A battery charged at 23:00 and emptied at 07:00 is one
transaction that does not. Charged naively, the first day pays for energy it
never used and looks terrible, and the second day gets that energy free and
looks wonderful.

So the battery has a **cost account**, exactly like stock in a warehouse.
Charging adds energy at what it cost; discharging takes it out at the account's
running average price. A day is credited or debited with the change in that
account's value, published as the `storage_carry` attribute.

The consequence is the one that matters: the credit one day takes is the
identical debit the next day pays, so **any run of days sums to the same total
whether or not the term is there**. It fixes single days without distorting the
month. `savings_excl_carry` is published alongside if you want to see it
without.

## Next 24 hours

The battery setting applies to forecasts too. Without a battery, the solar-only
baseline uses the plan's grid cost, battery savings and storage carry are zero,
and the independent model remains available as `modeled_cost_solar_only`.
Unlike recorded cost, this baseline is planned rather than measured.

`Forecast cost next 24 h` and `Forecast savings next 24 h` price the current
plan through the same three worlds. They use the plan's own `unit_load_cost`
and `unit_prod_price` — the prices EMHASS actually optimised against.

If your horizon is shorter than 24 hours, they report what the plan *does*
cover rather than extrapolating; `hours_covered` and `covers_full_window` say
so. The `hourly_cost` and `hourly_savings` attributes carry the cost and the
saving against a no-solar-no-battery baseline hour by hour, for a card to
draw. Savings here omit the storage-carry term, which is a window-level
adjustment rather than something that belongs to one hour.

## Entities

| Entity | |
|---|---|
| `sensor.*_energy_cost_today` | Net cost of the grid today: imports minus export income |
| `sensor.*_savings_today` | Total saving today, with every input in its attributes |
| `sensor.*_solar_savings_today` | The solar component. Only if a solar meter is configured |
| `sensor.*_battery_savings_today` | The battery component. Only if a solar meter is configured |
| `sensor.*_forecast_cost_24h` | Planned cost of the next 24 hours |
| `sensor.*_forecast_savings_24h` | Planned saving over the same window |

All in your Home Assistant currency. The four daily ones reset at local
midnight and are recorded in Home Assistant's own long-term statistics, so
per-day, per-month and per-year history comes from the Statistics card without
this integration keeping any history of its own.

## Which meters, and why it matters

Five meters, which are exactly your Energy dashboard's grid, solar and battery
sources — which is why the screen can resolve them instead of asking. They are
**stored when you submit**, so reorganising that dashboard months later cannot
silently repoint a savings figure at a different meter.

| Quantity | Needed for | If absent |
|---|---|---|
| Grid import + export | Everything. The one requirement | No cost tracking at all |
| Solar production | Splitting solar from battery | Your configured live PV sensor |
| Battery charge + discharge | The battery component | Your configured battery power sensor |
| House consumption | Accuracy of the baseline | Your house load sensor, else derived |

House consumption is never asked for here: the integration already has a
whole-house power sensor if you set one up for the load forecast, and otherwise
derives it from the energy balance
(`solar + import + discharge − export − charge`). The derivation works, but it
inherits every other meter's error — and mixes a DC-side solar meter with an
AC-side grid meter, which biases it upward by the inverter's conversion losses.
A real whole-house sensor is worth having.

**Energy counters are preferred over power sensors.** A counter was integrated
by the meter itself, at its own resolution; a power sensor can only be
integrated as fast as Home Assistant sees it change. Both work. The `sources`
attribute on `savings_today` says which entity answered for each quantity and
which of the two shapes it was, so a suspicious number can always be traced.

There is no field for a *grid power* sensor. A house with no import/export
counters cannot use Home Assistant's own Energy dashboard either, so it already
needs an integration (Riemann sum) helper — and a second, less accurate
integration of the same signal here would only be a way to be quietly wrong.

## Checking the numbers

Every input is on the sensors' attributes, so the headline can be reconstructed
by hand. Three attributes are worth looking at first when something seems off:

- **`unpriced_kwh`** — energy that flowed while no price was known, so it was
  counted but not costed. Non-zero after a restart, before the first successful
  optimisation. If the recorder still holds history for that gap, it clears
  itself back out shortly after the first successful run once real prices
  exist. A value that stays non-zero for longer means recorder history
  couldn't cover it (purged, disabled, or a reconfigured entity), not "just
  restarted" — a large value means the day is understated.
- **`balance_residual_kwh`** — how far your measured house load drifts from
  what the other meters imply it should be. This is your own sensors
  disagreeing with each other, not an error in this integration, and it is the
  usual explanation for a baseline that looks wrong.
- **`sources`** — which entity answered for what.

The full ledger is also in the diagnostics download, under `savings`.

## Asynchronous measurements and attribution limits

Settlement currently reads every source whenever any source updates. It does
**not** reconstruct a common measurement interval for independent energy
counters and integrated power. The savings and battery-savings attributes
explicitly report **`source_intervals_reconciled: false`**. Actual grid cost
uses the measured import/export increments. With a battery configured, the
solar-only baseline uses `max(load - solar, 0)` and the corresponding simulated
export. Without a battery, the baseline instead uses actual grid cost:
**`solar_only_baseline_method: measured_grid_without_battery`** (otherwise
`load_minus_pv_model`). The independent model is diagnostic only in this case.

A PV counter reporting several minutes of generation in one step can therefore
be compared against only seconds of integrated house consumption. Earlier
events simulate import, followed by an artificial export on the PV update.
Because buying and selling usually have different prices, these errors need
not cancel, even if the accumulated `balance_residual_kwh` is zero. Simply
netting a whole hour is not a general fix either: genuine import/export
changes and tariff boundaries would then be lost.

The measured-grid baseline removes this artificial buy/sell spread from
no-battery savings without interpolating PV or cancelling real exports. A
delayed PV update changes the model diagnostic, not the baseline. It does not
repair uncertainty in measured grid increments crossing a tariff boundary,
or align sources for battery-enabled counterfactuals. Comparable, sufficiently
frequent AC-side measurements reduce uncertainty but do not prove
synchronisation. `unpriced_kwh` and the signed `balance_residual_kwh` remain
separate diagnostics; neither is cleared. Total and solar savings remain
estimates because the grid-only baseline depends on the house-load measurement.

### Existing no-battery ledgers

Reloading the integration with the corrected code reapplies the current battery
setting to the restored current-day ledger. The solar-only baseline is derived
from its already stored actual cost; solar savings are recomputed from that
baseline. Previously reported phantom battery savings stay visible as
`accounting_difference` against the retained model. Raw costs, energy,
unpriced energy and meter baselines are not overwritten. This also applies
to restart-gap replay and daily rollover, with no need to replay the whole
day. Disabled installations ignore stale battery-meter selections.
This is permanent topology-aware accounting, not temporary recalculation or
migration code, so there is nothing to flag for later removal.

The current battery setting applies to the whole restored day; the ledger has
no per-interval configuration history. If hardware really changes mid-day,
the component split for that mixed day is not a reliable historical attribution.
Enabling a battery after collecting no-battery intervals can expose their raw
model discrepancy in that day's battery component. Prefer a full new accounting
day for before/after comparisons. Previously recorded battery energy, SOC and
book fields are retained as historical diagnostics when disabling a battery;
they are not evidence of ongoing battery activity or used for savings/carry.

Already recorded Home Assistant sensor states and statistics are not rewritten.
For a closed no-battery day with a saved ledger, the corrected solar-only
baseline is its actual cost and corrected solar savings are grid-only minus
actual cost. Recovering missing prices or correcting measured house load still
requires the historical sources and tariffs. This change does not perform
bulk edits of recorder statistics. Do not use the counter-reset
recovery script below for this different problem: it repairs false grid-counter
increments, not asynchronous savings attribution. Do not delete unrelated
Home Assistant history to remove these estimates.

## Counter corrections and resets

Energy counters occasionally revise a reading downwards. A decrease of up to
10% of the last accepted reading is treated as a correction: no new energy is
booked, and that accepted high-water reading is retained until recovery. For
example, `19.438 -> 19.433 -> 19.446` counts only `0.008 kWh`, not the daily
counter total and not the already-counted `0.005 kWh` recovery.

A decrease exceeding 10% starts a new counter cycle and counts its post-reset
reading, including daily resets to zero or a small nonzero value. This threshold
is a heuristic: a large device correction can still look like a reset, and a
very shallow genuine reset can look like a correction. Negative, unavailable,
and non-finite counter readings are ignored. The retained baseline survives
reloads, and historical restart-gap replay uses the same rules. Recorder history
also restores the gap's final high-water mark before live tracking resumes,
even if pricing the gap must wait for tariffs. The priced replay reuses those
same observations, so it cannot book energy that live recovery already counted.
If history cannot be prepared at startup, that gap is not retried later against
an unaligned live baseline. Power-sensor integration is unchanged.

### Recovering a day affected by the old reset rule

Installing the fix prevents future overcounts; it does **not** automatically
rewrite an existing ledger, recorder history, or statistics. The old code
treated every decrease as a reset. Its extra energy includes both the false
reset reading and any already-counted recovery, not just the small decrease.

The repository includes an **offline, dry-run-first** recovery tool for a
single affected **grid-import energy counter and daily electricity-cost
sensor**. Run it from a checkout with Python 3.11+ and timezone data installed:

```sh
python scripts/recalculate_counter_cost.py \
  --energy history-84.csv \
  --day 2026-11-01 --timezone Europe/Stockholm
```

It compares the legacy and corrected counter arithmetic, reporting excess
import energy without guessing its price. History exports need the usual
`entity_id,state,last_changed` columns, one entity per file, strictly increasing
timestamps with explicit UTC offsets, and counter values in kWh (or pass
`--unit Wh` / `--unit MWh`). Keep sufficient history before the correction to
establish the accepted baseline, through recovery, and through the final cost
sample to be corrected. More complete history is better than a tightly cropped
event. The first reading is only a baseline, not an extra energy charge.

For the reported November 1 sequence, the fully recovered excess is
`19.438 kWh`. To price it, provide the **actual historical all-in buy tariff**
in currency/kWh (including the same tax, markup and network energy charges
used by Companion), not the day's average price or the whole cost jump divided
by the reset reading. Supply a CSV with this header:

```csv
start,end,buy
```

Each row describes a half-open tariff interval `[start,end)` with offset-aware
timestamps and a numeric buy price. Zero and negative prices are supported.
All affected source intervals must fit wholly inside one supplied tariff
interval each; gaps, overlaps and ambiguous boundary-straddling intervals are
rejected instead of guessed. Only affected intervals need tariffs. Add
`--prices prices.csv` for a priced dry run, or explicitly request a new export:

```sh
python scripts/recalculate_counter_cost.py \
  --energy history-84.csv --cost history-83.csv \
  --day 2026-11-01 --timezone Europe/Stockholm \
  --prices prices.csv --confirm-legacy-tracking \
  --output corrected-cost.csv
```

`--confirm-legacy-tracking` acknowledges that the affected readings were
processed by the old tracker with prices available, without a restart,
reconfiguration, missed updates, or rebaselining. A CSV alone cannot prove
this. If these conditions are uncertain, use the report for investigation,
not as an authoritative repair. The tool leaves source files untouched,
refuses to overwrite an existing output, preserves other local days and extra
CSV columns, and does not connect to Home Assistant. The output is a corrected
cost-state export for review, **not an automatically importable HA backup**.

Back up Companion storage and the recorder database before any actual repair.
For a closed day, the current `.storage/emhass_companion_<entry_id>_savings`
file is not that day's archive: editing it cannot fix the past day. Corrected
recorder states and long-term statistics are separate concerns. For an ongoing
day, changing only `actual_cost` or `import_spend` is insufficient: the ledger
also contains imported energy, house-load accounting and counterfactual costs.
Reconstruct all affected ledger fields with the original sources and tariffs
before replacing it while the tracker is stopped. Do not delete unrelated
history, delete all integration storage, or blindly subtract the entire
published cost jump (which also includes legitimate consumption).

The tool does not rebuild savings, sensor attributes, battery cost accounts,
or export-counter corrections. Those need a full source-and-tariff replay;
the runtime replay currently covers restart gaps, not arbitrary past days.

**Maintenance/removal note:** the offline legacy-comparison tool is recovery
code, not a runtime migration. Keep it while versions with reset-on-any-decrease
remain supported. Once that recovery window closes, remove
`scripts/recalculate_counter_cost.py`, its dedicated tests, and this recovery
section together. Keep the shared counter fix and live/replay regression tests
permanently. There is no startup migration or temporary runtime path to remove.

## Why not `planned_cost`?

`sensor.*_planned_cost` is EMHASS's `total_cost_fun_value`, and it is not
money you can total up:

- it is the solver's **objective**, so with `costfun: profit` it is the
  *negative* of cost, with `cost` it drops export income entirely, and with
  `self-consumption` it stops being currency at all;
- it looks **forward**, never at the day behind;
- its **window moves** — an MPC run covers the horizon from the next timestep,
  a day-ahead run covers to the end of tomorrow — so two consecutive values do
  not measure the same span;
- it is a **plan**, not what happened.

It is left exactly as it is, because it is the right number for debugging a
solve. Everything on this page is computed separately.

## What is deliberately not measured

**Load shifting.** Running the dishwasher at 02:00 instead of 18:00 saves real
money, and it is not in these figures. Half of it is computable — an on-demand
load's request time is known, so "what it would have cost run immediately" is
well defined. But a surplus load has no honest counterfactual (a pool pump that
only ever runs on spare sun would not have run at all, so crediting it with a
saving invents one), and moving a load also changes what the battery does, so
the component would not add cleanly to the other two. Left out rather than
shipped as a number that cannot be defended.

**Capacity and demand charges.** A [network tariff profile](network_tariffs.md)
tracks and prices these, but this page still doesn't. Everything above prices
energy per kWh; a demand charge is billed on a peak *power*, once a period,
which does not decompose into a daily figure the way an energy cost does — so
the daily cost sensor understates a bill that has one, and neither the "what
the plan saved" comparison nor the forecast credits demand-charge shaving at
all. The [period peak and peak headroom sensors](network_tariffs.md#entities)
report the demand-charge side directly; they are just not folded into the
number on this page.

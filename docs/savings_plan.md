# Cost and savings

*Design note for the cost-tracking feature. The user-facing page is
[savings.md](savings.md).*

## Why `planned_cost` is not the answer

`sensor.*_planned_cost` publishes EMHASS's `total_cost_fun_value`: the sum of
the `cost_fun_*` columns over the plan's whole horizon. Four separate things
stop it being "what this costs me".

1. **It is the objective, not money.** With `costfun: profit` the column is
   `-0.001 * Δt * (unit_load_cost * P_grid_pos + unit_prod_price * P_grid_neg)`
   — the *negative* of net cost, because the solver maximises. With
   `costfun: cost` the export term is dropped entirely. With
   `self-consumption` it stops being currency at all. The number's meaning
   changes when the user changes a dropdown.
2. **It is forward-looking.** It describes the horizon ahead, never the day
   behind.
3. **The window moves.** An MPC run covers `horizon_hours` from the next
   timestep boundary; a day-ahead run covers to the end of tomorrow. Two
   consecutive values are not measuring the same span, so the series cannot be
   summed, averaged, or compared with itself.
4. **It is a plan.** The house does not follow it exactly, EMHASS's forecasts
   are not what happens, and the executor's own margins move real power around.

So `planned_cost` stays exactly as it is — it is the EMHASS number, useful for
debugging a solve — and everything here is computed separately.

## The three worlds

Savings only mean anything against a counterfactual. This feature settles three
versions of the same house against the same prices, at the same instants:

| | PV | Battery | |
|---|---|---|---|
| **W0** grid only | no | no | Every kWh the house consumes is bought. |
| **W1** solar only | yes | no | PV offsets load as it arrives; the excess is exported. |
| **W2** actual | yes | yes | What the meters actually recorded. |

with

```
C0 = Σ  load · buy
C1 = Σ  max(load − pv, 0) · buy  −  max(pv − load, 0) · sell
C2 = Σ  imported · buy           −  exported · sell
```

and the savings decomposing exactly, with no overlap and no double counting:

```
solar savings   = C0 − C1
battery savings = C1 − C2   (+ storage carry, below)
total savings   = C0 − C2   (+ storage carry)
```

`solar savings` is self-consumed PV valued at the retail buy price plus
exported PV valued at the sell price. `battery savings` is everything the
battery added on top: arbitrage, the extra self-consumption it bought by
time-shifting PV into the evening, and the export revenue it gave up — net of
round-trip losses, which show up on their own because charge exceeds discharge.

**A useful property of this split.** `C0` depends only on house load and the
buy price; `C2` is read straight off the grid meter. So **total savings is
unaffected by any error in the PV measurement** — a DC-side PV meter, an
inverter that reports optimistically, a clipped string. Only the
solar/battery *split* carries that uncertainty, because only `C1` reads `pv`.
This is worth stating in the docs: the headline number is the robust one.

### House load

Measured directly when a whole-house sensor is configured (the same one the
"Create a house load sensor" step already asks for). Otherwise derived from the
energy balance:

```
load = pv + imported + discharge − exported − charge
```

Direct measurement is preferred because the derived form inherits every
meter's error, and in particular mixes DC-side PV/battery meters with an
AC-side grid meter, which biases `load` upward by the inverter's conversion
losses. When both are available the residual between them is published as
`balance_residual_kwh` — a free consistency check on the user's own meters.

### Storage carry

A day that ends with the battery fuller than it started has bought energy it
has not yet used. Charged to that day alone, the day looks expensive and the
next day looks free.

**The obvious fix is wrong.** Valuing the day's SOC delta at "the price the
battery charged at today" works on a charging day and silently yields *zero* on
a day that only discharges — because there is no charging to take a price from.
A day that only discharges is the second half of every arbitrage cycle, so a
cycle split across midnight books the purchase as a pure loss and the sale as
pure profit. This was the first implementation and it was caught by the
telescoping test, not by inspection.

So the battery gets a **weighted-average cost account**, exactly as a warehouse
values stock:

```
charge c at price p:   book_kwh += c        book_value += c · p
discharge d:           unit = book_value / book_kwh
                       book_kwh -= d        book_value -= d · unit
carry (for the day) =  book_value_end − book_value_at_day_open
```

The account is opened on the first priced interval, marking whatever is already
in the battery to the price then in force — wrong once, by less than one
charge's worth, and self-correcting from the first real cycle. It crosses
midnight intact, and is reconciled against `soc% · capacity` at the boundary
(keeping the unit price, correcting the quantity) so meter drift and round-trip
losses cannot let it drift away from the real battery.

Because this is the change in a *running account* rather than a re-valuation of
a delta, it telescopes **exactly**: the credit one day takes is the identical
debit the next day pays. Any run of days sums the same with the term or without
it. That property is asserted directly in `tests/test_savings.py`, and it is
what the whole daily figure rests on. Published separately as `storage_carry`,
with `savings_excl_carry` beside it, so nobody has to take it on trust.

## Realised: how the numbers are accumulated

Not by polling. The tracker subscribes to its source entities and settles a
micro-interval on **every state change**, at whatever resolution the meters
themselves report. That matters for two reasons:

- Clipping (`max(net, 0)`) is only exact at fine resolution. Over a 15-minute
  bucket a house that imported for 5 minutes and exported for 10 nets out to
  one direction and the other's cost vanishes. Over a few seconds it does not.
- Price is piecewise constant per hour, so multiplying an interval's kWh by the
  price at that instant *is* the exact integral, provided intervals are short
  against an hour.

The screen itself is a **single switch**. The five meters it needs are the
Energy dashboard's own grid, solar and battery sources, so they are resolved
from it, listed in the description, and only offered as pickers inside a
collapsed section. Resolution happens once, on submit, and the result is
stored: re-reading the dashboard at runtime would let a user reorganising it
later silently repoint a figure at a different meter. Two fields an earlier
draft had are gone — a grid *power* fallback (a house without counters cannot
use the Energy dashboard either, so it has already built a Riemann helper) and
a house-consumption counter (already answered by the load-forecast sensor, or
by the energy balance).

Two source shapes are accepted, per quantity:

- **Energy meter** (kWh, `total_increasing` or `total`) — preferred. The device
  integrated it at full resolution; we only difference it. Resets (including
  the daily reset on `sensor.daily_*` meters) are handled as
  `delta = new if new < old else new − old`.
- **Power sensor** (W) — fallback, integrated left-Riemann between state
  changes. Accuracy is then bounded by the source's own update rate, which for
  a Modbus inverter is seconds and for a cloud-polled sensor may not be good
  enough. The tracker records which shape each quantity came from and publishes
  it, so a suspicious number can be traced.

When no price is known for an instant (before the first successful run after a
restart, or a gap in the price series) the energy is still counted but its cost
is not: it accrues to `unpriced_kwh`, published as an attribute, so an
incomplete day is visibly incomplete rather than quietly understated.

The ledger persists through `Store`, saved on a delay and on stop, and rolls
over at local midnight — both on a timer and defensively on every settle, so a
restart at 00:05 does not merge two days.

## Forecast: the next 24 hours

The same three worlds, evaluated over the plan instead of the meters. Per plan
row, within `[now, now + 24 h)` and clipped to the horizon:

```
load  = (P_Load + Σ P_deferrable{k}) / 1000      kW
pv    = P_PV / 1000
grid  = P_grid / 1000                            (+ import, − export)
buy   = unit_load_cost   (the row's own, falling back to our price series)
sell  = unit_prod_price
```

`P_Load` excludes deferrables in EMHASS's schema, which is why they are added
back — W0 has to buy that energy too.

The last row is prorated when the 24-hour window ends inside it, and the
storage carry uses planned end-of-window SOC against **measured SOC now**, not
the plan's first row, so the number answers "from here".

If the plan does not reach 24 hours the sensors report what it does cover and
publish `hours_covered`; they do not extrapolate.

## Entities

| Entity | |
|---|---|
| `sensor.*_energy_cost_today` | C2 — what the grid actually cost today, net of export income |
| `sensor.*_savings_today` | C0 − C2 + carry |
| `sensor.*_solar_savings_today` | C0 − C1 |
| `sensor.*_battery_savings_today` | C1 − C2 + carry |
| `sensor.*_forecast_cost_24h` | C2 over the next 24 h |
| `sensor.*_forecast_savings_24h` | C0 − C2 over the next 24 h |

All monetary, in `hass.config.currency`. The four daily sensors are
`state_class: total` with `last_reset` at local midnight, which is what makes
Home Assistant's own long-term statistics keep the per-day history — no ring
buffer of our own, and the user gets month and year sums from the statistics
card for free.

Rejected: a `savings_total` lifetime counter. It is `sum()` over the statistics
Home Assistant already keeps, and a second accumulator that can drift from the
first is a support burden for no new information.

## Deliberately not included

- **Load-shifting savings.** Tempting, and half-computable: for an on-demand
  load the request time is known, so "what it would have cost run immediately"
  is well defined. But surplus loads have no honest counterfactual — a pool
  pump that only ever runs on spare PV would not have run at all, so crediting
  it with savings is inventing them — and shifting a load also changes what the
  battery does, so the component would not be additive with the other two. Left
  out rather than shipped as a number that cannot be defended.
- **A flat-price baseline** ("same energy at the day's average price"). It
  overlaps both other components and rewards nothing for self-consumption.
- **Capacity/demand charges.** `capacity_cost_per_kw` is already a term EMHASS
  optimises against, but realising it needs the tariff's own peak window and
  billing period, which vary by network operator. Out of scope here.

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

and the difference between each pair is one component:

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

Round-trip losses need no term of their own. The battery meters show more
energy going in than coming out, and that gap flows straight through into the
number.

### One thing worth knowing

`Savings today` depends only on your **house load**, your **grid meters** and
your **prices**. It does not depend on the solar measurement at all. So if your
inverter reports DC-side production, or clips, or is simply optimistic, the
headline is unaffected — only the split between the solar and battery
components moves. Trust the total more than the split.

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

`Forecast cost next 24 h` and `Forecast savings next 24 h` price the current
plan through the same three worlds. They use the plan's own `unit_load_cost`
and `unit_prod_price` — the prices EMHASS actually optimised against.

If your horizon is shorter than 24 hours, they report what the plan *does*
cover rather than extrapolating; `hours_covered` and `covers_full_window` say
so. The `hourly_cost` attribute carries the cost hour by hour, for a card to
draw.

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
  optimisation. A large value means the day is understated.
- **`balance_residual_kwh`** — how far your measured house load drifts from
  what the other meters imply it should be. This is your own sensors
  disagreeing with each other, not an error in this integration, and it is the
  usual explanation for a baseline that looks wrong.
- **`sources`** — which entity answered for what.

The full ledger is also in the diagnostics download, under `savings`.

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

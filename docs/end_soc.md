# End SOC

*Backend requirement: EMHASS **≥ v0.18.0**. From 0.18 the `soc_final`
constraint is soft (deviation penalised in currency, ~100× the dearest
import slot), so an over-ambitious target degrades with a warning instead
of failing the run. On ≤ 0.17.x the same constraint is a hard equality and
an unreachable target makes the whole optimisation infeasible.*

Every optimisation plans toward a battery level at the end of its horizon —
**End SOC** is how that level is chosen, set under **Configure → Battery**.

| Mode | Behaviour |
|---|---|
| **Optimized** *(default)* | Computed fresh each run from the solar, price and load forecasts *beyond* the horizon — see below. Target charge level acts as a reserve floor the computed target never plans below. |
| **Same as start** | Always plans back to whatever level the battery is at now. The old fixed behaviour, and the one-click way back if you distrust the computed plans. |
| **Fixed % slider** | Always aims for **Target charge level** exactly, feasibility-clamped. |

## How Optimized decides

A kWh held in the battery at horizon end only has value if it displaces a
kWh you'd otherwise buy after the horizon — so *Optimized* looks past the
horizon at the coming solar and price shape, and picks a target that:

- **Covers the bridge** — never plans to arrive at the horizon end with less
  than the house needs to get through to the next cheap grid hour or solar
  refill, on top of your configured reserve (Target charge level).
- **Leaves room for solar** — when a big surplus is coming, the target backs
  off so tomorrow's sun charges the battery instead of being exported and
  bought back.
- **Sells only when it clearly pays** — a kWh may leave before the horizon
  ends only when the spread between a published cheap buy hour and the
  current sell price covers the round trip, including cycle wear.
- **Holds steady under jitter** — a target within ±2 percentage points of
  the last run's is kept as-is, so plans don't thrash between cycles.

When solar or price data past the horizon is missing or short, the
heuristic falls back to safer assumptions (the price forecast if you have
configured one, otherwise today's price shape, zero PV, or
ultimately *Same as start* if nothing usable is available at all) — that
fallback, and which inputs were assumed, shows up in the sensor's
attributes below.

Solcast users: adding `sensor.solcast_pv_forecast_forecast_day_3` to your
solar forecast entities (**Configure → Solar forecast**) lets *Optimized*
see one more day of sun past the horizon, instead of inferring it from the
previous day's shape.

## Value of leftover energy

*Backend requirement: EMHASS **≥ v0.18.5**. Advanced section of
**Configure → Battery**, off by default.*

By default the End SOC target is a two-sided aim: ending the plan below it
**or above it** is penalised, so the optimiser sells any spare energy to land
on the target. **Value of leftover energy** (0 to 1) changes the upper side.
Each kWh the plan ends *above* the target is rewarded at that share of the
import price in the plan's last timestep, so the plan keeps a kWh instead of
selling it whenever that reward beats the sell price. The lower side is
unchanged: ending below the target still costs.

| End SOC mode | 0 (default) | Above 0 |
|---|---|---|
| Optimized | Plans to land on the computed target | The computed target is the least it ends at |
| Same as start | Lands on the starting level | Never ends below the start; may end above |
| Fixed % slider | Lands on the slider | The slider becomes a minimum |

So with this on, `end_soc_target` is *the least the plan ends at*, not where
it ends. The sensor says so: its `reason` gains an "At least this" sentence
and a `reward_factor` attribute appears. If a value is set but the add-on is
older than 0.18.5, nothing is sent and the sensor shows
`reward_inactive: needs EMHASS 0.18.5` instead. The setting is kept and takes
effect when the add-on is upgraded.

Things to know when you raise it:

- **The price is the plan's last step only.** If that step's price is
  negative or very low, the reward is too, and holding energy stops being
  attractive. Past the day-ahead close the last price is a copy of today's,
  so the reward then rests on a guess.
- **It pulls against the high-charge holding cost.** *Cost of sitting above
  it* charges time spent near full; this pays for ending high. If you set
  both, expect the plan to sit lower during the day and charge late.
- **A value of 1 means a kWh is worth the full import price.** Anything above
  that is not accepted: it would pay more for energy than buying it costs.
- **Leave it at 0 with Optimized unless you have a reason.** In a replay of
  this house's history (see `tools/end_soc_bench`) the computed target did
  best or equal at 0, and every higher value made it worse. The setting
  helps with *Same as start* and *Fixed %*, where around 0.3 to 0.5 cuts the
  cost of a badly placed target by a quarter to a half.

## The sensor

`sensor.<name>_end_soc_target` — the level the plan aims for at the
horizon's end, as a percentage.

| Attribute | Meaning |
|---|---|
| `mode` | `optimized` / `same_as_start` / `fixed_50` |
| `reason` | One sentence explaining the target, e.g. *"Holding 3.2 kWh to bridge evening load until cheap prices at 02:00; solar expected to refill from 09:30."* |
| `replenishment_kind` | `solar` / `cheap_grid` / `none` — what the target is bridging to |
| `cover_energy_wh` / `cover_target` | What the bridge needs on top of the reserve, and the SOC that represents |
| `cover_until` | When the battery would otherwise touch the reserve |
| `sale_energy_wh` / `sale_margin` / `sale_sell_price` / `sale_buy_price` | Present when the priced sell-before-horizon exception fired |
| `sale_blocked` | Present when it didn't, with which reason (no sell price / prices unpublished / no spread) |
| `reserve` | The floor used (your Target charge level setting) |
| `clamped_by` | Which bound actually set the value: nothing bit / `profitable_sale` / `reserve` / `range` / `hysteresis` |
| `unreachable` | Present and `true` when the target likely exceeds what charge/discharge power can deliver within the horizon — including the SOC charge taper, if one is configured — EMHASS will aim for it and fall short, softly |
| `price_tail` | `known` / `forecast` (past your price source, filled by the [price forecast](setup.md#price-forecast)) / `assumed from today` |
| `load_tail` | `own forecast` / `borrowed from last plan` |
| `reward_factor` | Present when *Value of leftover energy* is on and sent: the target is then a floor |
| `reward_inactive` | Present when it is set but the add-on is too old to take it |
| `fallback` | Present with the cause when the heuristic couldn't run at all |

In the non-Optimized modes the sensor still reports (state, `mode`,
`reason`), so the dropdown and the sensor never disagree.

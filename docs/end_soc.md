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
heuristic falls back to safer assumptions (today's price shape, zero PV, or
ultimately *Same as start* if nothing usable is available at all) — that
fallback, and which inputs were assumed, shows up in the sensor's
attributes below.

Solcast users: adding `sensor.solcast_pv_forecast_forecast_day_3` to your
solar forecast entities (**Configure → Solar forecast**) lets *Optimized*
see one more day of sun past the horizon, instead of inferring it from the
previous day's shape.

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
| `unreachable` | Present and `true` when the target likely exceeds what charge/discharge power can deliver within the horizon — EMHASS will aim for it and fall short, softly |
| `price_tail` | `known` / `assumed from today` |
| `load_tail` | `own forecast` / `borrowed from last plan` |
| `fallback` | Present with the cause when the heuristic couldn't run at all |

In the non-Optimized modes the sensor still reports (state, `mode`,
`reason`), so the dropdown and the sensor never disagree.

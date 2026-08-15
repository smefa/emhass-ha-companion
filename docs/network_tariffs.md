# Network tariffs

`tariff.py`'s buy/sell composition is the *supplier* side of a bill — a pure
function of `(spot, time)`, per kWh. A growing number of network operators
bill a second, structurally different side of the same bill: a per-kWh fee
that depends on a calendar rather than the spot market, and a **demand
(capacity) charge** on the highest power you drew in a window, over a whole
billing period. A planner that only sees per-kWh prices has no reason to avoid
a spike during that window — the energy is the same energy — and will happily
start the EV, the water heater and the heat pump in the same cheap timestep,
handing you a demand charge on top.

A **network profile** (`kind: network`) describes that second side: a
calendar, a set of time-of-day/season price bands, and a demand charge. It
stacks with an ordinary price profile rather than replacing it — a Tibber
household with a time-differentiated network fee runs both, and the buy price
EMHASS sees is `tariff(spot) + band_adder`.

Pick one under **Configure → Network tariff**, the same config-flow step every
other profile kind uses. Two ship built in:

- **Göteborg Energi** (`network/goteborg_energi`) — a high-load season and
  window (Nov–Mar, weekdays 07:00–20:00, red days excluded via the
  [Workday](https://www.home-assistant.io/integrations/workday/) integration)
  and a demand charge on the mean of the three highest hourly peaks, on three
  distinct days, inside it.
- **Amber Electric** (`network/amber_demand`) — a DNSP demand tariff as billed
  through Amber: the single highest 30-minute interval inside a daily window
  sets a per-kW charge for the whole month. Contributes no energy bands at all
  — Amber's per-kWh side is already the spot series `tariff.py` composes.

Nothing on this page is specific to those two utilities; see
[Writing a profile](profiles.md) to describe your own operator's tariff sheet
the same way.

## Energy bands

`energy_bands:` is a list of windows — a season (`months:`), a day-type
(`days:`, referencing a named `calendar:` entry), an hour range (`hours:`,
which may wrap midnight) — each carrying a `buy`/`sell` `multiplier`/`adder`.
First match wins, so the list reads like the tariff sheet does: exceptions
first, the unconditional fallback band last.

After the price profile composes `(buy, sell)`, the first matching band's
adder/multiplier is applied to every point — same series in, same series out.
Matching runs against **local** wall-clock time: "07:00–20:00" means your own
clock, not UTC, and the boundary correctly follows daylight saving in both
directions.

A day-type reference (`days: business_day`) resolves against Home Assistant's
[Workday](https://www.home-assistant.io/integrations/workday/) integration,
configured for your own country, subdivision and extra dates — this
integration ships no holiday calendar of its own. Every distinct local date in
the horizon is resolved once per run and cached for the lifetime of the
process (a public holiday calendar doesn't change once the year has passed).
If the Workday entity is unavailable or unconfigured, every day is treated as
a business day — the safe direction, since it over-prices a high-load band
rather than under-pricing it.

The **Network tariff band** sensor reports which band is in force right now,
with the adder, the next change, and the next band's name in its attributes.

## The demand charge

Two things make a sheet's demand-charge rate unsafe to send to EMHASS as-is.

**The rate needs converting.** EMHASS charges `rate × peak_import` **once per
optimisation** as `capacity_cost_per_kw`, so the number sent has to be the
*marginal cost, this horizon, of raising the billing period's measured peak by
one kW* — a different number for each tariff shape:

| Tariff | Sheet rate | Aggregation | Effective rate |
|---|---|---|---|
| Göteborg | 135 kr/kW/month | mean of top 3, distinct days | `135 / 3` = 45 kr/kW |
| Amber | $0.30/kW/day | single monthly max | `0.30 × days_in_month` ≈ $9/kW |

Sending Göteborg's undivided 135 would over-price today's peak threefold: a
new peak only displaces the current third-highest, so the bill only moves by a
third of the difference. Sending Amber's raw per-day figure would understate
the marginal cost by a factor of ~30: a new monthly max is charged for every
remaining day of the month. `rate_basis` (`month` or `day`) plus
`measure.aggregate`/`n` in the profile is what lets the integration compute
this instead of asking you to. The **Demand charge rate** diagnostic sensor
publishes the effective figure, with the sheet rate and the arithmetic in its
attributes — the answer to "why is the plan avoiding 17:00 so hard" is one
click away.

**The charge needs memory.** Sent alone, `capacity_cost_per_kw` prices the
peak import across the *entire* horizon with no memory of what has already
been incurred this billing period — so a run on the 20th of the month still
spends battery and defers loads to hold today under a level that was already
exceeded on the 3rd, buying nothing. A `peaks.py` tracker (shaped like
`metering.py`'s savings tracker: a `Store`, restore-on-restart, monthly
rollover at local midnight) maintains the billing period's qualifying
intervals and derives:

- **The billed aggregate** — `max` for a single-peak tariff (Amber), or the
  mean of the top *n* across distinct days for Göteborg's shape.
- **The floor** to send as `current_period_peak` — the level at which shaving
  starts to pay. For `mean_top_n`, that is `max(third_highest_across_days,
  today's_own_existing_peak)`: if today already holds one of the top three, a
  bigger peak today doesn't add a fourth entry, it replaces today's own, so
  fighting to stay under the (now superseded) third-highest would buy
  nothing.

The **Billing period peak** sensor reports the current aggregate, with every
contributing interval and days remaining in its attributes. The **Peak
headroom** sensor reports how much more can be drawn right now before a new
peak raises the bill — read off the plan's current grid-power row, so it lags
a real load spike by up to one MPC interval, but it's the one built for your
own automations to subscribe to (hold an EV charger back when the forecast
turns out wrong).

### Version gating

Both `current_period_peak` and the window that restricts the charge to its
own hours are runtime parameters EMHASS documents as MPC-only:

| Backend | What happens |
|---|---|
| **< 0.18.0** | Energy bands only; the demand-charge section of the config flow is shown disabled |
| **0.18.0** | Priced peak **only if the window is unrestricted** (all day, every day). A real window (Göteborg's, Amber's) has no `capacity_charge_window` to restrict it on this release, so pricing it unwindowed would over-shave every hour outside the window — instead, it falls back to the hard cap below |
| **0.18.1+** (what this add-on ships today) | Full: windowed priced peak on MPC (`capacity_cost_per_kw` + `capacity_charge_window` + `current_period_peak`), the capped array on day-ahead |

The window mask (`capacity_charge_window`) landed on EMHASS's `master` branch
in August 2026 and reached a release in **0.18.1** (PR #1066) — on that
version or newer, a real window like Göteborg's prices cleanly, masked, and
the hard cap below is inert for it. On 0.18.0 the profile still runs the
middle row: the hard cap protects the window, and **Peak target** is what to
adjust. `capacity_cost_per_kw` is also forced to `0` on **day-ahead** runs
regardless of backend — both gating parameters only mean anything to
`naive-mpc-optim`, so a day-ahead run prices no peak at all and shapes the day
purely through the cap below; the MPC runs that follow correct it.

## The windowed hard cap

The **v0.18.0 fallback**, and the mechanism behind an explicit
`capacity_limit:` block (a subscribed capacity tier you're contractually
inside, independent of whether it's priced): `maximum_power_from_grid` sent as
a **per-timestep list** rather than a scalar — see
[Dynamic grid limits](grid_limits.md) for how it composes with the ordinary
scalar import limit. Every timestep inside the relevant window is capped at
`subscribed_kw − headroom_kw` (an explicit `capacity_limit`) or at the **Peak
target** number (the demand-window fallback); every timestep outside it keeps
the plain scalar.

**Peak target** (`number.*_peak_target`) is the ceiling enforced whenever the
demand charge is configured but cannot currently be priced — on 0.18.1+ that
is only ever a backend-version mismatch (the add-on reports older than it was
set up against) rather than the everyday case a real window used to be on
0.18.0. It's seeded from the tracker's current period aggregate the first time
it's added — hold what you've already paid for — and is yours to adjust. It's
a worse answer than the priced peak, and stays inert once the window mask
prices it instead. `capacity_limit:` itself is unaffected by any of this — a
subscribed tier is a hard ceiling by nature, not a fallback for anything.

A cap below a timestep's own forecast load would make the whole run
infeasible — EMHASS answers infeasible for the entire problem, not for that
timestep — so each element is floored at its own forecast load, and a run
warning names any timestep that had to be raised above its target.

## Entities

| Entity | Type | Notes |
|---|---|---|
| `sensor.*_network_tariff_band` | sensor | Current band name; `adder`, `multiplier`, `next_change`, `next_band` attributes |
| `sensor.*_demand_charge_rate` | sensor, diagnostic | The effective `capacity_cost_per_kw`; `sheet_rate`, `rate_basis`, `aggregate`, `active`, `reason` attributes |
| `sensor.*_period_peak` | sensor (kW) | The billed aggregate so far this period; `period`, `days_remaining`, `floor_kw`, `contributing_intervals` attributes |
| `sensor.*_peak_headroom` | sensor (kW) | Current aggregate minus current draw |
| `number.*_peak_target` | number (kW) | The 0.18.0 fallback ceiling; also usable as a manual override |

The band and rate sensors exist whenever a network profile is selected; the
peak, headroom and peak-target entities exist only once that profile defines
a `demand_charge`.

## What this does not do

**None of this is real-time.** The optimiser plans against a load forecast
and re-plans every MPC interval; a kettle, an oven and a car on the same
half-hour will blow a demand window the plan believed was safe, and nothing
here stops it — the same boundary [Dynamic grid limits](grid_limits.md#smoothing)
draws for fuses: *anything that must be enforced in real time belongs in a
load-balancing device, not in a planner.* The plan avoids **scheduling**
peaks; the peak-headroom sensor lets your own automations react to
unscheduled ones. Neither is a guarantee.

**Savings don't credit it yet.** See
[Cost and savings, "What is deliberately not measured"](savings.md#what-is-deliberately-not-measured).

**End SOC doesn't know about it.** The [End SOC](end_soc_plan.md) heuristic
values a stored kWh by what it displaces after the horizon; it doesn't reason
about a demand window opening past the horizon's end, so it can hand that
window an empty battery.

**Surplus and on-demand loads don't see it either.** Both push power into
windows the optimiser chose for price reasons; once a demand charge is active
those choices carry a second cost neither load type accounts for.

**Two deferrable loads that used to share a cheap hour may separate on their
own** once a demand charge is active — peak shaving and load spreading are the
same lever. That's the feature working, not a regression, but it will look
like one if you don't know it's on.

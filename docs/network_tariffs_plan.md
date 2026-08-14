# Network tariffs (planned)

*Status: planned. Nothing here is implemented.*

*Backend requirement: mixed, and the mix is the single most important
constraint on this design. Of the three EMHASS runtime parameters this
feature needs, two are in the released **v0.18.0** and one is only on
master. See [What EMHASS already gives us](#what-emhass-already-gives-us).
This HA runs v0.18.0 today.*

---

## The problem

`tariff.py` composes a buy and a sell price out of a spot series, and says so
in its own docstring: three modes, all of them per-kWh, all of them a pure
function of `(spot, time)`. That is the *supplier* side of a bill, and for a
Tibber-or-Nordpool household with a flat network fee it is the whole story.

It is not the whole story for a growing share of users, because network
operators have spent the last few years moving cost off the per-kWh line and
onto two other lines:

1. **Time-differentiated energy fees** — the same kWh costs more between
   07:00 and 20:00 on a winter weekday than it does at 03:00 on a Sunday in
   July. Still per-kWh, but on a calendar the spot price knows nothing about:
   seasons, business days, public holidays.
2. **Demand (capacity) charges** — a charge on the *highest power you drew*
   during a window, over a whole billing period. Not per-kWh at all. A single
   bad half-hour on the 4th of the month can cost more than a week of energy.

The second one is the interesting one, and it is what "how do we best limit
high power draw during certain hours" actually reduces to. A planner that
only sees per-kWh prices has no reason whatsoever to avoid a 12 kW spike at
17:30 — the energy is the same energy. It will happily start the EV, the
water heater and the heat pump in the same timestep because that timestep is
cheap, and hand the user a bill with a demand charge on it.

## Two tariffs, in full

The two the feature is designed against. Both numbers and both calendars are
illustrations — rates change every year, and the whole point of the design
below is that they live in user-editable configuration, never in code.

### Göteborg Energi — *tidsindelad elnätsavgift*

| | |
|---|---|
| Energy fee | 6,5 öre/kWh transmission, year-round, all hours |
| High-load season | 1 Nov – 31 Mar |
| High-load hours | 07:00–20:00, weekdays only |
| Excluded from high load | Sat, Sun, red days, Christmas Eve, New Year's Eve |
| Demand charge | 135 kr/kW, high-load period only; 0 kr 1 Apr – 31 Oct |
| Demand measure | **mean of the three highest hourly peaks, on three different days** |
| Qualifying peaks | Nov–Mar: only hours inside the high-load window count |
| Subscribed capacity | 6 / 10 / 14 / 43 kW tiers, setting the fixed monthly fee |

Three properties matter for the design, and none of them are expressible
today:

- The window is a **calendar**, not a time range. "Weekday" and "not a red
  day" are questions about a date.
- The measure is an **aggregate of three separate days**, not a single max.
  That changes the marginal value of shaving a peak by a factor of three —
  see [Pricing the peak correctly](#pricing-the-peak-correctly).
- The peaks are **hourly averages**. A 12 kW burst for ten minutes inside an
  otherwise 2 kW hour meters as 3,67 kW, not 12 kW.

### Amber Electric — demand tariffs

| | |
|---|---|
| Demand measure | the single highest **30-minute** interval in the window |
| kW conversion | interval kWh × 2 |
| Window | a fixed daily range set by the DNSP, e.g. 15:00–20:00 |
| Rate | expressed per kW **per day**, billed across the days in the month |
| Period | monthly; one interval sets the charge for the whole month |
| Seasonality | DNSP-dependent; some have a summer-only window |

Structurally simpler — a plain max, one window, no holiday calendar — but the
*rate* is where it bites: a per-kW-per-day rate multiplied by a month means
the marginal cost of setting a new peak is roughly thirty times the number on
the tariff sheet. Again, see [Pricing the peak
correctly](#pricing-the-peak-correctly).

The two together span the design space well: one needs a rich calendar and a
top-N aggregate with a modest effective rate, the other needs a trivial
calendar and a plain max with a very large effective rate.

## What exists today, and where it falls short

**`tariff.py` template mode** can already express a time-of-day band —
`_apply_template` deliberately passes local wall-clock `time` for exactly
that. What it cannot reasonably express is a holiday calendar, and what it
cannot express *at all* is a demand charge, because a demand charge is not a
function of one timestep's price. It is also rendered once per point through
the HA template engine, which is fine for a band and wasteful for a calendar
we could evaluate directly.

**`capacity_cost_per_kw`** is already sent, unconditionally, on every run
([payload.py:581](../custom_components/emhass_companion/payload.py#L581)),
defaulting to 0. When a user sets it, EMHASS prices the peak import **across
the entire horizon** with **no memory of the billing period**. For both
tariffs above that is wrong in two directions at once:

- *Too broad.* It penalises the 03:00 battery charge that no tariff on earth
  charges demand for. With a large enough rate — and Amber's effective rate
  is large — the plan will refuse to charge the battery cheaply overnight in
  order to protect a peak that is not being measured overnight.
- *No memory.* If the month's peak is already locked in at 9 kW on the 3rd,
  every run for the remaining 28 days still spends battery and defers loads
  to hold today under 9 kW, buying nothing.

**Grid limits** are one scalar per run, and
[grid_limits.md:43-45](grid_limits.md#L43-L45) says that is all EMHASS
accepts. **That is now out of date** — see below. Fixing that sentence is
part of this work.

## What EMHASS already gives us

EMHASS issue #623 built the demand-charge machinery in three phases. The
phases did not all ship in the same release, and the split is the main thing
this plan has to route around.

| Capability | Runtime param | v0.18.0 | master | Actions |
|---|---|---|---|---|
| Price the horizon peak | `capacity_cost_per_kw` | ✅ | ✅ | all |
| Floor at the incurred peak | `current_period_peak` | ✅ | ✅ | **MPC only** |
| Restrict to a demand window | `capacity_charge_window` | ❌ | ✅ | **MPC only** |
| Per-timestep import cap | `maximum_power_from_grid` (list) | ✅ | ✅ | all |

Mechanics worth knowing, all verified against the source:

- `peak_import` is a **single scalar** cvxpy variable, created only when
  `capacity_cost_per_kw > 0`, constrained as the epigraph
  `peak_import >= mask ⊙ p_grid_pos`, and charged once in the objective at
  `rate × peak_import / 1000`. It is a *power* charge and is deliberately not
  scaled by the timestep.
- `current_period_peak` is a second lower bound on the same variable, in
  **watts**, reset to 0 on every call so a stale value cannot leak between
  MPC ticks. With it binding, shaving below it has zero marginal value and
  the solver stops spending flexibility on it. This is exactly the "memory"
  the feature needs, and it is already in the release we run.
- `capacity_charge_window` is a per-timestep weight vector in `[0, 1]`,
  length `prediction_horizon`, aligned like `load_cost_forecast`. Invalid or
  short vectors fall back to all-ones with a warning. Fractional weights pass
  through, though the docstring says 0/1 is the expected shape. **The caller
  owns the business-day / holiday / season calendar** — EMHASS is explicit
  about that, which is the right split and matches this integration's
  posture everywhere else.
- `maximum_power_from_grid` goes through `_parse_power_limit` in
  `treat_runtimeparams` (accepts a list, a tuple, or a stringified list) and
  then `_prepare_power_limit_array`, which broadcasts a scalar and validates
  a list's length. **A length mismatch does not error** — it logs a warning
  and silently collapses to the *first element* repeated. That failure mode
  is quiet and severe (a whole horizon pinned at the window's cap), so the
  companion must get the length right rather than trusting the backend.

## Design overview

One new profile kind carrying a calendar, feeding three mechanisms.

```
  network profile (YAML)
  ├── energy_bands ──────▶ per-timestep adder on buy/sell price
  │                        → load_cost_forecast / prod_price_forecast
  │
  ├── demand_charge ─────▶ capacity_cost_per_kw   (effective rate)
  │                        capacity_charge_window (0/1 mask, master only)
  │                        current_period_peak    (from the peak tracker)
  │
  └── capacity_limit ────▶ maximum_power_from_grid as a per-timestep list
                           (opt-in hard ceiling; the 0.18.0 fallback)

  peaks.py ──────────────▶ the billing-period peak actually incurred
                           (persisted, monthly rollover, mirrors metering.py)
```

The ordering is deliberate: **price first, cap second.** A priced peak lets
the optimiser trade peak reduction against energy cost and produce the
cheapest total; a hard cap asserts an answer and can make the problem
infeasible. The cap exists for the cases where the limit is genuinely hard
(a subscribed capacity tier you are contractually inside) and as the
degradation path on v0.18.0, not as the default.

## The `network` profile kind

A sixth `PROFILE_KINDS` entry alongside `price`, `pv`, `load`,
`temperature`, `inverter`. It reuses the whole existing profile apparatus —
builtin documents under
`custom_components/emhass_companion/profiles/builtin/network/`, user
overrides in `/config/emhass_companion/profiles/`, `options:` rendered as
config-flow fields by the profile engine, `detect:`, `notes:`, the
`test_profile` service.

It is a **fourth structural shape** for a profile, though. Today
`_validate_profile` knows two: `inverter` (control/actions) and everything
else (`source` and/or `emhass`). A network profile has neither — it fetches
nothing and delegates nothing. `_validate_profile` gains a `network` branch
requiring at least one of `energy_bands`, `demand_charge`, `capacity_limit`,
and the "contributes nothing" check stays as the fallthrough for source
kinds.

### Schema

```yaml
name: <display name>
kind: network
version: 1
description: >-
detect: {}            # e.g. country/currency hints; usually `always: true`
options: {}           # rendered as config-flow fields, referenced as {{ options.x }}

calendar:
  # Named date-sets referenced by bands and windows. Evaluated by the
  # companion, never by EMHASS.
  business_day:
    weekdays: [mon, tue, wed, thu, fri]
    exclude_holidays: true
    holiday_entity: "{{ options.workday_entity }}"   # binary_sensor.workday_*

energy_bands:
  # First match wins, top to bottom; the last band should be unconditional.
  - name: <band name>
    months: [11, 12, 1, 2, 3]      # optional; local calendar months
    days: business_day             # optional; a `calendar:` key
    hours: "07:00-20:00"           # optional; local wall clock, may wrap midnight
    buy:  {adder: 0.34}            # same shape as a tariff side: multiplier/adder
    sell: {adder: 0.0}
  - name: <fallback>
    buy:  {adder: 0.065}

demand_charge:
  rate_per_kw: 135                 # as it appears on the tariff sheet
  rate_basis: month                # month | day  -> drives the effective rate
  window:
    months: [11, 12, 1, 2, 3]
    days: business_day
    hours: "07:00-20:00"
  measure:
    interval: 60min                # the meter's averaging interval
    aggregate: mean_top_n          # max | mean_top_n
    n: 3
    distinct_days: true
  period: month                    # billing period; drives the tracker's rollover

capacity_limit:
  # Optional hard ceiling. Omitted by most profiles.
  subscribed_kw: "{{ options.subscribed_kw }}"
  window: same_as_demand_charge    # or an inline window block
  headroom_kw: 0.5                 # planned below the contractual number
```

Every number in that document is reachable from `options:`, so a rate change
is a config-flow edit, not a release — the same doctrine `tariff.py`'s
docstring already states, applied to a richer structure.

### `network/goteborg_energi.yaml`

```yaml
name: Göteborg Energi (tidsindelad elnätsavgift)
kind: network
version: 1
description: >-
  Göteborg Energi Nät's time-differentiated grid tariff: a high-load window on
  winter weekdays, and a demand charge on the mean of the three highest hourly
  peaks inside it.
notes: >-
  Rates change every year and this profile ships last year's as defaults --
  check them against your own invoice. The high-load window excludes red days
  and, unusually, Christmas Eve and New Year's Eve, which are not public
  holidays in Sweden; add them under "Add holidays" in the Workday integration.

detect:
  always: true

options:
  hoglast_ore:
    name: High-load energy fee
    description: öre/kWh during the high-load window.
    default: 34.0
    selector: {number: {min: 0, max: 500, step: any, mode: box}}
  laglast_ore:
    name: Low-load energy fee
    description: öre/kWh at all other times. Includes the transmission fee.
    default: 6.5
    selector: {number: {min: 0, max: 500, step: any, mode: box}}
  effekt_kr_per_kw:
    name: Demand charge
    description: kr/kW/month on the high-load peak.
    default: 135.0
    selector: {number: {min: 0, max: 2000, step: any, mode: box}}
  workday_entity:
    name: Workday sensor
    description: >-
      A binary_sensor from the Workday integration, configured for Sweden with
      weekends and holidays excluded. Supplies the red-day calendar.
    selector: {entity: {integration: workday, domain: binary_sensor}}
  subscribed_kw:
    name: Subscribed capacity
    required: false
    description: >-
      Your contracted tier. Leave blank unless you want the plan held under it
      as a hard ceiling.
    selector: {select: {options: ["6", "10", "14", "43"]}}

calendar:
  business_day:
    weekdays: [mon, tue, wed, thu, fri]
    exclude_holidays: true
    holiday_entity: "{{ options.workday_entity }}"

energy_bands:
  - name: höglast
    months: [11, 12, 1, 2, 3]
    days: business_day
    hours: "07:00-20:00"
    buy: {adder: "{{ options.hoglast_ore / 100 }}"}
  - name: låglast
    buy: {adder: "{{ options.laglast_ore / 100 }}"}

demand_charge:
  rate_per_kw: "{{ options.effekt_kr_per_kw }}"
  rate_basis: month
  window:
    months: [11, 12, 1, 2, 3]
    days: business_day
    hours: "07:00-20:00"
  measure:
    interval: 60min
    aggregate: mean_top_n
    n: 3
    distinct_days: true
  period: month

capacity_limit:
  subscribed_kw: "{{ options.subscribed_kw }}"
  window: same_as_demand_charge
  headroom_kw: 0.5
```

### `network/amber_demand.yaml`

```yaml
name: Amber Electric (demand tariff)
kind: network
version: 1
description: >-
  A DNSP demand tariff as billed through Amber: the single highest 30-minute
  interval inside a daily window sets a per-kW charge for the whole month.
notes: >-
  Amber shows the window and the rate on your plan page; they come from your
  DNSP, not from Amber, so they differ by distributor. The rate is usually
  quoted per kW per DAY -- enter it exactly as quoted and leave the basis on
  "per day"; the plan converts it.

options:
  demand_rate:
    name: Demand rate
    description: $/kW/day, as quoted on your plan.
    selector: {number: {min: 0, max: 10, step: any, mode: box}}
  window_start:
    name: Demand window start
    default: "15:00:00"
    selector: {time: {}}
  window_end:
    name: Demand window end
    default: "20:00:00"
    selector: {time: {}}
  window_months:
    name: Months the window applies
    description: Leave empty for all year.
    required: false
    selector: {select: {multiple: true, options: [...]}}

energy_bands: []        # Amber's energy price is the spot series; tariff.py owns it

demand_charge:
  rate_per_kw: "{{ options.demand_rate }}"
  rate_basis: day
  window:
    months: "{{ options.window_months }}"
    hours: "{{ options.window_start }}-{{ options.window_end }}"
  measure:
    interval: 30min
    aggregate: max
  period: month
```

Note that Amber contributes no energy bands at all — its per-kWh side is
already the spot series that `tariff.py` composes. The two features stack
rather than compete: **`tariff.py` keeps the supplier side, the network
profile adds the network side.** A user on Amber with a demand tariff runs
both, and the buy price EMHASS sees is `tariff(spot) + band_adder`.

## Mechanism 1 — energy bands

The easy one, and worth doing first because it is entirely companion-side
and cannot break a run.

After `Tariff.compose` returns `(buy, sell)`, a new `NetworkCalendar` walks
the series and applies the first matching band's `multiplier`/`adder` to each
point. Same `Series` in, same `Series` out; `payload.py` is untouched.

- Band matching runs against `dt_util.as_local(point.time)`, for the same
  reason `_apply_template` does: `Series` normalises to UTC internally, and
  "07:00–20:00" means the user's clock. Getting this wrong shifts every band
  by the UTC offset and, in Sweden, by a different amount in summer than in
  winter.
- `hours` may wrap midnight (`"22:00-06:00"`).
- Bands are ordered and first-match-wins, so the calendar reads like the
  tariff sheet does: exceptions first, the general case last.
- Holiday lookups are resolved **once per run, per distinct local date** in
  the horizon — two or three dates, not 96 timesteps.

The result is a strictly better version of what template mode does today.
Template mode stays: it is the escape hatch for a structure nobody
anticipated, exactly as the profile schema's docstring says a `template`
source is.

## Mechanism 2 — the priced peak

### Pricing the peak correctly

The number on the tariff sheet is almost never the number to send as
`capacity_cost_per_kw`. EMHASS charges `rate × peak_import` **once per
optimisation**, so the rate has to be the *marginal cost, over this horizon,
of raising the billing period's measured peak by one kW*. That is a different
number for each tariff shape:

| Tariff | Sheet rate | Aggregation | Effective `capacity_cost_per_kw` |
|---|---|---|---|
| Göteborg | 135 kr/kW/month | mean of top 3, distinct days | `135 / 3` = **45 kr/kW** |
| Amber | $0.30/kW/day | single monthly max | `0.30 × days_in_month` ≈ **$9/kW** |

The Göteborg division by three is not a fudge. If today sets a new entry in
the top three, it displaces the current third-highest, and the bill moves by
one third of the difference. Sending the undivided 135 would over-price
today's peak threefold and buy peak reduction with battery cycles worth more
than the saving.

The Amber multiplication is the same argument in the other direction: a new
monthly max is charged for every day of the month, so a rate quoted per day
understates the marginal cost by a factor of ~30. This is the case where
sending the sheet rate silently does nothing useful, and where getting it
right changes the plan the most.

`rate_basis` plus `measure.aggregate`/`n` in the profile is what lets the
companion compute this rather than asking the user to. A **Demand charge
rate** diagnostic sensor should publish the effective figure with the sheet
rate and the arithmetic in its attributes, because "why is the plan avoiding
17:00 so hard" needs an answer that is one click away.

Refinement to consider once the basic version is in: outside the demand
season the effective rate is zero (Göteborg, Apr–Oct), and near the end of a
month with a peak already well above anything the horizon can reach, it is
also effectively zero. Both fall out of the window mask and the incurred-peak
floor respectively, so neither needs special-casing in the rate.

### The incurred-peak floor

`current_period_peak`, in watts, from the peak tracker below. Available in
v0.18.0, MPC only.

The value is not simply "the highest interval so far this month". It is *the
level at which shaving starts to pay*, which depends on the aggregation:

- **`max`** (Amber): the highest qualifying interval so far this period.
  Straightforward.
- **`mean_top_n` with `distinct_days`** (Göteborg): the **third-highest**
  qualifying hour so far, taken across distinct days — that is the entry a
  new peak would displace. Below it, a new peak changes nothing.
  - With one subtlety worth getting right: if **today already holds one of
    the top three**, a bigger peak today does not add a fourth entry, it
    *replaces today's own*. So the floor is
    `max(third_highest_across_days, todays_existing_peak)`. Sending only the
    third-highest would have the plan fight to stay under a level it has
    already exceeded today, for nothing.

When fewer than `n` days have qualifying peaks (the first days of a month,
or the first days of the season), the third-highest is 0 and the floor is 0 —
correct, since any peak at all will enter the top three.

### The window mask

`capacity_charge_window`: a list of `0.0`/`1.0`, length `prediction_horizon`,
one per timestep, `1.0` where the timestep's local start falls inside
`demand_charge.window`. Built by the same `NetworkCalendar` that resolves the
energy bands, off the same per-date holiday lookups.

**Master only, and newer than the current release.** v0.18.0 was tagged
2026-08-03; the mask landed on master in `148e6272` on 2026-08-08 (PR #1066,
*"feat: mask the demand/capacity charge to a tariff demand window"*). It is
not a feature that was removed — it has simply never been in a release.
Verified against the v0.18.0 tarball: zero occurrences of
`capacity_charge_window` or `param_capacity_window` across the whole tree,
and the epigraph there is the plain `peak_import >= p_grid_pos`.

On v0.18.0 the parameter is therefore not read at all, and **nothing says
so**. Every `runtimeparams` access in that release is a named lookup plus the
fixed `associations.csv` loop — no iteration over the supplied key set, so no
whitelist check and no unknown-key warning. The mask is dropped in silence
and the peak is priced across the whole horizon. Silence is the problem: the
plan would look plausible and be wrong. So the companion gates on the backend
version it
already records (`emhass_version` in the config entry data,
`MIN_EMHASS_VERSION` in `const.py`) and takes the fallback below rather than
sending a parameter it knows will be ignored.

### Metering interval versus optimisation timestep

`peak_import` bounds **every in-window timestep** of `p_grid_pos`. The meter
bills an **average over its interval**. When the optimisation timestep is
finer than the metering interval, those differ: a 30-minute optimisation
timestep against Göteborg's hourly metering means the plan protects the worse
of two half-hours, when the bill charges their mean.

That direction is conservative — the plan is stricter than the tariff — so it
is safe, but it costs money by over-shaving. There is no way to express an
interval average through a scalar epigraph, so the answer is guidance plus a
warning, not arithmetic:

- When a demand charge is active, **set `optimization_time_step` to the
  metering interval** (60 for Göteborg, 30 for Amber). The config flow should
  say so on the network step and the health check should raise it when the
  two disagree.
- A timestep *coarser* than the metering interval is the dangerous direction:
  an hourly plan against 30-minute metering understates the peak, and the
  plan will quietly blow the demand window. Worth a hard warning.

### Day-ahead versus MPC

Both `current_period_peak` and `capacity_charge_window` are documented in
EMHASS as *"Runtime-only; only used by naive-mpc-optim"*, while
`capacity_cost_per_kw` reaches the objective on **every** action. Left alone,
a day-ahead run would price the peak with no window and no memory — the exact
distortion this feature exists to remove, reintroduced on the run that shapes
tomorrow.

The split is therefore explicit:

| Run | Peak shaping |
|---|---|
| **MPC** | Priced peak: `capacity_cost_per_kw` + window mask + incurred-peak floor |
| **Day-ahead** | `capacity_cost_per_kw` forced to **0**; shaping via the per-timestep cap array, which does work on `dayahead-optim` |

`payload.py` already branches on `inputs.action`, so this is a small change
in the right place. It does mean the day-ahead plan's shape comes from a
ceiling rather than a price, which is a real approximation — but the
day-ahead run is a fallback for when MPC has no fresh prices, and the MPC
runs that follow will correct it.

## Mechanism 3 — the windowed hard cap

`maximum_power_from_grid` as a list. Available in v0.18.0, both actions.
Opt-in via `capacity_limit:`, plus the automatic fallback on backends without
the window mask.

The value per timestep is `subscribed_kw − headroom_kw` inside the window and
the existing scalar limit outside it — i.e. it composes with, and can only
lower, the number from the grid step and the import-limit sensor, keeping
[grid_limits.md](grid_limits.md)'s "can only lower, never raise" rule intact
across all three sources.

The infeasibility floor from grid_limits.md becomes **per-timestep**, and
this is where the sharp edges are:

- Today the floor is the load forecast's **peak over the horizon**, one
  number. Per-timestep it must be that timestep's own forecast load —
  otherwise the horizon's worst hour raises the cap everywhere and the window
  is not capped at all.
- A cap below a timestep's forecast load makes the whole run infeasible, and
  EMHASS answers infeasible for the *entire* problem, not for that timestep.
  So each element is floored at its own forecast load, and any timestep that
  had to be raised is named in a run warning. Silently raising them all and
  saying nothing would turn a badly-set subscribed tier into "the demand
  charge feature doesn't work".
- **Array length.** `_prepare_power_limit_array` collapses a wrong-length
  list to its first element repeated, with only a log warning on the EMHASS
  side. For MPC the length is `prediction_horizon`. For day-ahead it is
  `delta_forecast_daily × steps_per_day`, which is *not* `horizon_steps` —
  the companion rounds the horizon up to whole days there
  ([payload.py:438-439](../custom_components/emhass_companion/payload.py#L438-L439)).
  Getting this wrong pins the entire day-ahead horizon at the window's cap.
  It needs a test per action, not a shared one.
- With `compute_curtailment` off and an export cap, the same infeasibility
  applies on the export side — already documented in grid_limits.md and
  unchanged here.

**grid_limits.md needs updating regardless of this feature.** Its lines 43–45
state that both limits are single values per run because "that is all EMHASS
accepts", which stopped being true when `_prepare_power_limit_array` landed.

## The billing-period peak tracker

A new `peaks.py`, deliberately shaped like `metering.py`: subscriptions to a
power or energy meter, a `Store` for persistence, a scheduled rollover, and
restore-on-restart. `metering.py`'s `SavingsTracker` is the working model for
all of that, down to the "settle on every state change" argument in its
docstring — which applies here for the same reason, since an interval average
built from sparse samples is wrong in the same way.

What it maintains, per configured `period`:

- The current interval's accumulating energy, bucketed on the tariff's
  `measure.interval` boundary in **local** time.
- A list of completed qualifying intervals — only those whose start fell
  inside the demand window, so out-of-window draw never enters the record.
- Derived from that list: the aggregate the tariff actually bills
  (`max`, or the mean of the top *n* across distinct days), and the floor
  value for `current_period_peak` per the rules above.

Details that will otherwise be found the hard way:

- **Energy, not power.** The interval value is `∫P dt / interval_hours`, so
  the source should be the grid import energy meter the savings feature
  already asks for, with the power sensor as a fallback. A power sensor
  sampled on state change and time-weighted is close enough; a power sensor
  sampled on a timer is not.
- **Rollover is monthly and local**, at local midnight on the 1st, not on a
  fixed 30-day cadence. `metering.py` already does the local-midnight
  rollover for days (`_async_midnight`, `_local_day`); this is the same shape
  one level up.
- **Restart mid-interval** must not lose or double-count the partial
  interval. `Meter.restore` is the existing precedent.
- **Seasonal boundaries** are not period boundaries. Göteborg's record resets
  monthly, but 1 Apr – 31 Oct simply has no qualifying intervals, so the
  record is empty and the floor is 0 without any special case.
- Backfilling from the recorder on first setup is tempting and should be
  resisted for v1 — the statistics API gives hourly means for the import
  sensor, but reconstructing "was this hour inside the window" across a
  partial month, across DST, is a lot of surface for a number that
  self-corrects within a month. Start at zero, say so in the sensor's
  attributes, and consider backfill later.

## Calendars: weekdays, seasons and holidays

Seasons and weekdays are date arithmetic. Holidays are not, and the
integration ships `"requirements": []` — no `holidays` package, no country
tables, no yearly maintenance burden. Consistent with "your existing
integrations are the data sources", the calendar comes from HA's **Workday**
integration, which the user configures for their own country, subdivision and
extra dates.

Two ways to read it, and the profile needs both:

- **`binary_sensor.workday_*`** answers for *today* only. Sufficient for an
  MPC horizon that never leaves today, useless the moment it crosses
  midnight.
- **`workday.check_date`** answers for an arbitrary date. This is the one
  that matters, and the profile engine already has a
  `SOURCE_TYPE_SERVICE` for service calls with response data.

So: resolve each distinct local date in the horizon through
`workday.check_date` once per run, cache per date, and fall back to
weekday-only matching with a run warning if the service is unavailable or the
entity is not configured. Falling back to weekday-only is the right failure —
it treats a red day as a working day, which over-prices the peak and
over-shaves, i.e. it errs expensive-but-safe rather than blowing the window.

Christmas Eve and New Year's Eve are not Swedish public holidays but *are*
excluded by Göteborg's tariff. The profile's `notes:` must say to add them
under Workday's "Add holidays". This is exactly the kind of local detail that
belongs in a per-operator YAML note and nowhere near the code.

## Version gating

| Backend | Behaviour |
|---|---|
| **master / next release** | Full: windowed priced peak on MPC, incurred-peak floor, capped array on day-ahead |
| **v0.18.0** | Priced peak **only if the window is all-day**; otherwise fall back to the windowed hard cap on both actions, with the incurred-peak floor still applied on MPC |
| **< v0.18.0** | Energy bands only; the demand-charge section of the config flow is shown disabled with the reason |

The v0.18.0 fallback deserves stating plainly, because it is what the user
will actually run at first: **on v0.18.0 a windowed demand charge is enforced
as a ceiling, not as a price.** That needs a target level, which a priced
peak derives for itself. Rather than iterate the solve, expose it as the
existing `capacity_limit` mechanism — a **Peak target** number entity,
defaulted from the tracker's current period aggregate (hold what you have
already paid for) and adjustable. It is a worse answer than the priced peak
and it should say so in the entity's description, but it is a working answer
today rather than a feature that waits on an upstream release.

`emhass_version` is already in the config entry data and `health.py` already
does backend checks, so the gate has a home.

## Entities

| Entity | Type | Purpose |
|---|---|---|
| `sensor.*_network_tariff_band` | sensor | Current band name; attributes: adder, next change, next band |
| `binary_sensor.*_in_demand_window` | binary_sensor | Inside the demand window now; attributes: window start/end, next window |
| `sensor.*_period_peak` | sensor (kW) | The billed aggregate so far this period; attributes: the contributing intervals, their timestamps, days remaining |
| `sensor.*_peak_headroom` | sensor (kW) | Current aggregate minus current draw — how much more can be drawn before a new peak is set |
| `sensor.*_demand_charge_rate` | sensor, diagnostic | The effective `capacity_cost_per_kw`, with the sheet rate and arithmetic in attributes |
| `number.*_peak_target` | number | The v0.18.0 fallback ceiling; also usable as a manual override |

`sensor.*_peak_headroom` is the one that pays for itself outside the
optimiser: it is the thing a user's own automation subscribes to in order to
hold the EV charger back when the forecast turns out wrong. Which leads to
the next section.

## What this does not do

**None of this is real-time.** The optimiser plans against a load forecast
and re-plans every MPC interval; a kettle, an oven and a car on the same
half-hour will blow a demand window that the plan believed was safe, and
nothing in this feature will stop it. That is the same boundary
[grid_limits.md:124-126](grid_limits.md#L124-L126) already draws for fuses,
and it should be drawn again here in the same words: *anything that must be
enforced in real time belongs in a load-balancing device, not in a planner.*

The difference from a fuse is that a demand window is more forgiving in one
respect and less in another. More: nothing trips, you just pay. Less: a fuse
forgives a brief excursion thermally, whereas a 30-minute metering interval
remembers a bad ten minutes at exactly a third of its weight, for a month.

The honest scope is therefore: the plan avoids *scheduling* peaks, the
headroom sensor lets the user's own automations react to unscheduled ones,
and neither is a guarantee. Documentation should not promise more.

## Interactions with existing features

- **Savings.** `savings.py` prices energy per kWh. A demand charge is not
  per-kWh, so the daily cost sensor will understate a bill that has one, and
  the "what the plan saved" comparison will not credit peak shaving at all.
  Out of scope for v1, but it should be named in the docs rather than
  discovered — and it is the strongest argument for the accrual approach
  (spread the period's demand charge across the period's days) whenever
  savings gets revisited.
- **End SOC** (`terminal.py`). The terminal heuristic values a stored kWh at
  what it displaces after the horizon. It knows nothing about a demand window
  starting an hour past the horizon end, so it can hand the window an empty
  battery. Deferring this is defensible while horizons cover a day; it should
  be listed as future work in [end_soc_plan.md](end_soc_plan.md).
- **Surplus loads** and **on-demand loads** both push power into windows the
  optimiser chose for price reasons. Once a demand charge is active those
  choices have a second cost that the surplus path, which synthesises its own
  window, does not see.
- **`typical_load.py`** uses `maximum_power_from_grid` as a scaling reference
  and its docstring already calls it "a poor" one. It must read the scalar
  nameplate, not a capped array element.
- **Deferrable loads.** Peak shaping and load spreading are the same lever.
  With a demand charge active, two deferrable loads that used to run in the
  same cheap hour will separate on their own — which is the feature working,
  and will look like a regression to anyone who does not know it is on. The
  status card's explanation of *why* a load moved matters more once this
  ships.

## Failure modes and guardrails

| Failure | Guard |
|---|---|
| Window mask sent to a backend that ignores it | Version gate; fall back to the ceiling, never send a parameter known to be dropped |
| Cap array length wrong for the action | Compute per action; assert length in tests for both MPC and day-ahead |
| Cap below a timestep's forecast load | Per-timestep floor, and name the raised timesteps in a run warning |
| Sheet rate sent raw as `capacity_cost_per_kw` | `rate_basis` + `aggregate` drive the conversion; diagnostic sensor shows the arithmetic |
| Optimisation timestep ≠ metering interval | Health check; hard warning when the timestep is *coarser* |
| Workday unavailable | Weekday-only fallback with a run warning; errs toward over-shaving |
| Tracker lost on restart | `Store` + restore, following `Meter.restore` |
| Tracker seeded at zero on a mid-month install | Stated in the sensor's attributes; self-corrects within a period |
| Band adders applied twice | Network profile documents that a price profile already including network fees should use `passthrough`, same warning `nordpool_custom.yaml` already carries for VAT |
| Demand charge left on out of season | Falls out of the mask; the rate sensor should read 0 and say why |

## Testing

Unit, no HA:

- `NetworkCalendar` band resolution: season edges, midnight-wrapping hours,
  first-match-wins ordering, and **DST transitions in both directions** — a
  07:00 band boundary on the spring-forward Sunday is the classic wrong
  answer, and Sweden's late-October transition lands inside Göteborg's
  season.
- Window mask: length equals `prediction_horizon` exactly, values in `{0,1}`,
  alignment against `load_cost_forecast` verified on a horizon that crosses
  local midnight.
- Effective rate: the `/3` and `×days_in_month` cases, month lengths of 28
  through 31, and February in a leap year.
- Peak tracker: top-3-distinct-days including the "today already holds an
  entry" case; monthly rollover; interval bucketing on local boundaries;
  restore mid-interval.
- Cap array: per-timestep floor, composition with the import limit sensor and
  the fixed scalar, length per action.

Integration, following `tests/integration/test_tariff_template.py`:

- A full Göteborg run: assert the payload's `capacity_cost_per_kw`, the mask,
  the floor, and that the buy series carries the right adder per timestep.
- The same entry on a v0.18.0 backend: assert no mask is sent and the cap
  array is present instead.
- A day-ahead run with a demand charge configured: assert
  `capacity_cost_per_kw == 0` and the cap array's length matches
  `delta_forecast_daily × steps_per_day`.
- An Amber run: `rate_basis: day`, no energy bands, mask from a plain time
  range.
- Profile round-trip: `test_profiles.py`-style validation of both builtin
  documents, plus a `network` profile that defines none of the three blocks
  failing validation.

## Phasing

Small versioned steps on `dev`, in the usual style.

1. **Energy bands.** `network` profile kind, `NetworkCalendar`, band
   application after `Tariff.compose`, the Workday date resolution, the band
   sensor, the Göteborg profile with its demand section omitted. No payload
   change at all, so nothing can regress a run. Ships useful on its own.
2. **The peak tracker.** `peaks.py`, the period-peak and headroom sensors,
   persistence and rollover. Still no payload change — it observes for a
   period before anything acts on it, which is the same posture as
   `switch.emhass_control_enabled` shipping off.
3. **The priced peak.** Effective-rate conversion, `current_period_peak` on
   MPC, `capacity_cost_per_kw` forced to 0 on day-ahead, the rate diagnostic
   sensor, the version gate. Works on v0.18.0 for an all-day window.
4. **The windowed cap.** `capacity_limit`, the per-timestep array and its
   floor, the peak-target number, the v0.18.0 fallback path. Update
   grid_limits.md's per-run-scalar claim.
5. **The window mask.** `capacity_charge_window` behind the version gate,
   once it is in a released EMHASS.
6. **Docs.** `docs/network_tariffs.md`, a guide page, the Amber profile, and
   the savings gap written down.

Steps 1 and 2 are independent of every EMHASS version question and can go in
immediately. Step 5 is the only one that waits on upstream.

## Open questions

- **Export demand charges.** Some operators are introducing them. The schema
  above is import-only. Worth leaving room in the profile shape (a `sell` side
  on `demand_charge`) even if nothing implements it.
- **Should the effective rate be tapered near the end of a period?** With
  three days left in a month and a peak already set, the value of protecting
  it is unchanged, but the value of *setting a new one* is technically
  proportional to remaining days for a `per day` basis that bills only the
  days after the peak. Amber's help pages do not say which. Assume the
  conservative reading (full month) for v1 and check against a real invoice.
- **Multiple simultaneous network profiles.** A user with both a
  time-differentiated energy fee and a separately-billed capacity charge from
  a different line on the invoice. One profile per entry is simpler and
  almost certainly enough; the band list can carry both.
- **Backfilling the tracker from the recorder** — deferred above; revisit if
  mid-month installs prove annoying in practice.
- **Verify the Göteborg numbers.** 135 kr/kW is read off their site as a
  high-load-period figure; whether it is monthly or seasonal changes the
  effective rate by a factor of five. The profile defaults must be checked
  against an actual invoice before release, and the `notes:` must tell every
  user to do the same.

# End SOC (planned)

*Status: implemented (2026-08-05) — the selector, `terminal.py`, the End SOC
target sensor, the Solcast day-3 default, the engine relaxation and the
`p_load`-borrowing fallback for settings-only load profiles are all in.
Still open: the candidate-sweep v2 and the upstream EMHASS terminal-value
objective under Future work.*

*Backend requirement: EMHASS **≥ v0.18.0**. From 0.18 the `soc_final`
constraint is soft (deviation penalised in currency, ~100× the dearest
import slot), so an over-ambitious target degrades with a warning
instead of failing the run. On ≤ 0.17.x the same constraint is a hard
equality and an unreachable target makes the whole optimisation
infeasible — the existing infeasibility repair issue would surface it,
but the answer is "upgrade the add-on", not companion-side workarounds.
Release notes and docs must state the floor; this HA runs v0.17.9
today, so upgrading the EMHASS add-on to v0.18.0 is part of rollout.*

## The problem

Every optimisation request currently pins `soc_final` to the live SOC
(`payload.py`, "soc_final = soc_init"). EMHASS enforces that as a hard
equality on net battery throughput, so every kWh discharged inside the
horizon must be bought back inside the horizon. Three ways this hurts:

1. **Arbitrage refusal at the horizon edge.** Battery full, prices high
   now, cheap hours starting just *past* the horizon end: the plan does
   nothing with the battery, because it cannot see where to buy the
   energy back.
2. **No pre-positioning for solar.** The plan can never aim to reach
   sunrise with an empty battery to absorb tomorrow's surplus, unless
   that sunrise happens to fall inside the horizon.
3. **The ratchet.** Each MPC cycle re-pins the target to the drifting
   live SOC, so sensor noise becomes a hard constraint and the target
   follows every excursion instead of correcting it.

The distortion is worst exactly when the horizon ends just before a big
price or solar event — with Nordpool publishing tomorrow's prices at
~13:00, a daily occurrence.

## Design overview

A kWh in the battery at horizon end has a value: the cost of the energy
it displaces *after* the horizon. EMHASS's API offers no terminal-value
objective — only the hard `soc_final` equality — so the companion picks
the end SOC and EMHASS executes. That split (terminal condition chosen
outside the solver) is the standard architecture for finite-horizon MPC,
not a workaround.

Three pieces:

- an **End SOC** selector in the battery settings step,
- a **terminal-SOC heuristic** (`terminal.py`) that computes the target
  from the forecast tails past the horizon,
- an **End SOC target sensor** whose attributes explain the number.

## The selector

Battery settings (`config_flow.battery_schema`, both setup and options
flow) gains one `SelectSelector`:

| Option | Stored value | Behaviour |
|---|---|---|
| **Optimized** *(default)* | `optimized` | Computed each run from PV, price and load forecasts — see below |
| **Same as start** | `same_as_start` | Today's behaviour: `soc_final = soc_init` |
| **Fixed % slider** | `fixed_50` | `soc_final = soc_target` (the Target charge level slider), feasibility-clamped |

- Stored under the existing `battery` options dict as `end_soc_mode`.
- `BatteryConfig` gains `end_soc_mode: str = DEFAULT_END_SOC_MODE`
  (`"optimized"`), parsed in `from_dict` with the default covering every
  existing install.
- Option labels via `strings.json` selector translations (plus the
  existing translation files), so no English literals in code.

> **Upgrade note:** existing installs silently move from *Same as start*
> to *Optimized* on update, because that default is the point of the
> feature. Release notes must say so, and *Same as start* is the one-click
> way back to the old behaviour if a user distrusts the new plans.

## The Optimized heuristic

New module `custom_components/emhass_companion/terminal.py` — a pure
function in the style of `surplus.py`, no I/O, fed by the series the
coordinator has already fetched. `Series.window()` / `value_at()` give
everything needed to look past the horizon.

```python
@dataclass(slots=True)
class EndSocDecision:
    soc: float                     # final fraction, all clamps applied
    reason: str                    # one human sentence, for the sensor
    details: dict[str, Any]        # structured attributes, see sensor below

def decide_end_soc(
    mode, soc_init, battery, now, horizon_end, step,
    pv, load, buy_price, sell_price,
    previous: EndSocDecision | None,
) -> EndSocDecision
```

For `same_as_start` and `fixed_50` it just returns the value with a
matching reason. For `optimized`:

1. **Collect the tails** — the slice of each series past `horizon_end`.
   The load profile is statistical (typical daily curve), so it extends
   as far as asked; PV extends as far as the selected profile provides
   (see *PV coverage past the horizon* below). Two per-input fallbacks
   when a tail is missing or short:
   - **Price** — before ~13:00 tomorrow's Nordpool prices don't exist,
     so when the price tail is shorter than ~2 h, substitute today's
     known curve shifted 24 h as a statistical proxy (the daily *shape*
     is structural), and mark the decision
     `price_tail: "assumed from today"`.
   - **PV** — where the PV series ends, reuse the previous day's shape at
     `PV_PROXY_CONFIDENCE` (50%) weight and mark
     `pv_tail: "assumed from the previous day"`; with no previous day
     either, assume **zero** and mark `pv_tail: "assumed zero"`.
     (2026-08-06: originally zero-only. Unlike prices, PV has huge
     day-to-day weather variance, and promising sun that never arrives
     strands the battery low — so the proxy is **one-sided**. It may
     lower the solar ceiling, where being wrong costs a partial charge
     and the next MPC cycle corrects it; it may never shorten the bridge,
     where being wrong costs an empty battery at dawn, and a guessed
     block may only push the refill event *later* than the price trough,
     never earlier. Without it the ceiling was dead on every evening run
     — with a 24 h horizon a today+tomorrow forecast runs out within
     hours of `horizon_end` — which is exactly when the overnight
     strategy is decided.)
   - **Load** — a slot the fetched series doesn't cover falls back to the
     same slot 24 h earlier, which for the default 24 h horizon lands
     inside the horizon itself: this quietly duplicates day 1's load
     shape into the tail with no extra code (see *Load coverage* below
     for where the series itself comes from).
2. **Find the next replenishment event** after `horizon_end` — solar
   outranks price outright, not whichever comes first chronologically:
   - *solar*: the first PV-surplus block (`pv − load > 0`) whose
     cumulative surplus reaches a material fraction of capacity
     (~10%), timestamped at the block's start. If found anywhere in the
     lookahead, it wins even when a cheap grid moment occurs earlier —
     paying anything, even at the cheap end of the tariff, to skip a
     short wait for free PV is a bad trade.
   - *cheap grid*: the first price at or below the 25th percentile of
     the full known buy-price series, used only when the lookahead has
     no material solar block at all (winter's short, weak days).
   (2026-08-06: originally "whichever comes first"; a live Nordpool
   night trough almost always falls within a couple of hours of any
   horizon end, which made the reserve dominate nearly every decision —
   see the session note below.)
3. **Bridge energy**: `E = Σ max(0, load − pv) · step` over
   `[horizon_end, replenishment)`, divided by discharge efficiency —
   the energy the house needs from the battery before it can be
   refilled cheaply or for free.
4. **The floor** — `reserve + E / capacity`, where `reserve` is the
   existing `soc_target` setting, repurposed to its natural meaning of
   "comfort/backup floor the plan should always end with". `soc_target`
   is still sent to EMHASS as `battery_target_state_of_charge`,
   unchanged: EMHASS only reads that key as the *default* end SOC when
   no `soc_final` is supplied, and we always supply one, so it is inert
   there — but the companion's convention is to assert every setting on
   every request so EMHASS's persisted config never silently decides,
   and "default end SOC" stays semantically compatible with the floor
   reading if that fallback ever fired.
5. **The ceiling** — `soc_max − max(0, S − E) / capacity`, where `S` is
   the absorbable energy of the next material surplus block (its
   integral × charge efficiency). This is the term that makes the
   feature about *self-consumption* rather than arbitrage: every kWh
   held past what the house needs is a kWh of tomorrow's sun that gets
   exported at the sell price and bought back at the buy price. The
   `− E` matters — the constraint binds at the moment the surplus
   *starts*, by which time the bridge has already been drawn down, so
   the target may sit that much higher than the raw surplus suggests.

   Ceiling ≥ floor reduces to `soc_max − reserve ≥ S / capacity`: the
   ceiling binds **only** when the coming sun genuinely exceeds the
   usable span above the reserve, so it is inert all winter and needs no
   seasonal switch. Unlike the floor, the ceiling accepts a *proxied*
   surplus block, at its already-discounted weight.

   (2026-08-06: replaces the original "solar bias" step, which only said
   "do not round the target up further" and was therefore a no-op. With
   nothing but the floor, the heuristic was one-directional — it could
   only ever add to the reserve — so it returned the reserve itself on
   any day whose next refill was close, and looked inert.)
6. **Bounds, in order** (the last one to bite is recorded in
   `details["clamped_by"]`, with the untouched floor kept as
   `raw_target`):
   - `solar_headroom`: the ceiling, when it sits below the floor;
   - `reserve`: the user's comfort floor. Solar headroom may push the
     target down *to* it but never through it. When this is what a
     summer day reports every time, the answer is to lower `soc_target`
     — it is the one setting that widens the band the computation can
     move in;
   - `range`: `[soc_min, soc_max]`;
   - `hysteresis`: if `previous` exists and the new target is within
     ±2 percentage points, keep the previous value — a pinned target
     that jitters run-to-run makes plans thrash.

   There is deliberately **no feasibility clamp**: from EMHASS 0.18 the
   `soc_final` constraint is soft, so a target the battery cannot reach
   in time makes the solver get as close as physics allows (the penalty
   is priced too high to trade away for energy cost) and log a warning.
   The solver knows the physics better than a companion-side bound
   would. Instead, `decide_end_soc` *annotates*: when
   `|target − soc_init|` exceeds the rough power-limit reach for the
   horizon, set `details["unreachable"] = true` so the sensor can say
   the plan will aim for the target but fall short.
7. **Fallback**: with no usable tails at all, behave as
   `same_as_start`, with `reason` saying which input was missing.

The `previous` decision lives on the coordinator in memory only; after a
restart the first run simply computes fresh (hysteresis needs no
persistence).

## Candidates (added 2026-08-06)

There is no ground truth for a terminal-value heuristic short of running
one against a real house for a season. So `terminal.py` computes
*several* on every run, against one shared view of the world (`_Tail`),
and reports them all:

| Key | Label | What it is |
|---|---|---|
| `bridge_only` | Test 1 — bridge to the next refill | The calculation as it shipped: `reserve + E / capacity`, no ceiling, no PV proxy |
| `solar_headroom` | Test 2 — bridge floor, solar ceiling | The design above. Active until 2026-08-07 |
| `daily_ratio` | Test 3 — daily-yield template | The Jinja template this feature replaced, ported: drift the live SOC by tomorrow-minus-today's forecast yield, on a sliding floor |
| `night_cover` | Test 4 — night cover, sold only when it pays | The night-cover rule below. **Active** |
| `priced_bridge` | Test 5 — priced bridge, curtailment ceiling | The two lowering mechanisms Test 4 refuses, kept where they cost nothing: a published cheap hour ends the night, and room is made only for surplus that would be clipped or given away |

Tests 1 to 3 are frozen. They are the yardsticks the recorded `tests`
history is measured against, and editing one silently rebases that
history, so improvements go in as a new candidate.

`ACTIVE_CANDIDATE` names the one whose number actually reaches EMHASS;
the rest ride along in the End SOC sensor's `tests` attribute:

```yaml
active_test: night_cover
tests:
  bridge_only:     {label: "Test 1 — …", soc: 0.70, reason: "Holding 4.0 kWh …"}
  solar_headroom:  {label: "Test 2 — …", soc: 0.30, reason: "Leaving room for 22.5 kWh …"}
  daily_ratio:     {label: "Test 3 — …", soc: 0.58, reason: "Template drift: 12 kWh …"}
  night_cover:     {label: "Test 4 — …", soc: 0.70, reason: "Covering 4.0 kWh on top …"}
  priced_bridge:   {label: "Test 5 — …", soc: 0.70, reason: "Covering 4.0 kWh on top …"}
```

Recorded over a month, that attribute says which one *should* have been
driving. The active candidate's entry is repeated in `tests` on purpose,
and holds the value the candidate computed **before** hysteresis, so a
recorded history compares like with like.

Test 3 carries `TEMPLATE_REFERENCE_YIELD_KWH = 40.0` — the plant-scale
constant from the original template. Hard-coding it is only acceptable
because the candidate is a shadow calculation; promoting it means
turning that into a setting first.

**Adding another is three lines**: write
`_my_idea(tail: _Tail) -> EndSocDecision`, add
`_Candidate("my_idea", "Test 6 — my idea", _my_idea)` to `CANDIDATES`,
and it appears in the sensor on the next run. `_Tail` already exposes
`reserve`, `clamp()`, `first_block()`, `last_block()`, `first_cheap()`
and `deficit_until()`, and `_required_soc()` / `_sale_credit()` are
reusable, so a new idea is usually a few lines of arithmetic over data
that has already been gathered.

## The night-cover rule (2026-08-07)

Test 2 shipped for three days and was replaced. What it did on a clear
August evening, live:

> *Leaving room for 41.9 kWh of expected solar surplus: ending at 20% so
> the sun charges the battery instead of being exported. Held at the 20%
> reserve floor.*

At 18:00 the pin lands at 18:00 tomorrow — **after** tomorrow's solar
peak, which is inside the horizon and already EMHASS's to plan. The
surplus being made room for is therefore the *day after tomorrow's*,
starting ~12 h past the pin with a whole night in between. The ceiling
then computed `soc_max − (41.9 − 11.2) / 22 = −40%`, an unsatisfiable
bound, and collapsed to the reserve — overriding a bridge floor of 71%,
i.e. the 11.2 kWh the house needs to get through that night.

The failure is structural, not a tuning error. The ceiling binds exactly
when `(soc_max − need)·C < surplus − drawdown` — that is, exactly when
the coming sun *more than* refills the battery from the bridge target.
In that regime the battery is full by mid-morning whichever target is
chosen, so the room buys nothing; the only real difference is that the
night was spent on grid import at `1.25 × spot + 0.80` instead of on
stored solar. When the sun genuinely cannot fill the battery, `room >
need` and the ceiling never binds at all. **Its binding region is its
refutation.** Nor was it needed for the imminent-sun case: with the sun
arriving at the pin the bridge is ~0 and Test 1 returns the reserve on
its own.

The replacement is one rule:

> Never plan to arrive at the pin with less than the house needs before
> the sun comes back — unless selling that energy first demonstrably
> pays for buying it again.

**The floor** is `_required_soc`, a backward pass over the lookahead: the
lowest SOC at the pin that never puts the battery under its reserve.
Deficits raise the requirement, surpluses lower it, the floor at the
reserve is what stops 40 kWh of Sunday from excusing an empty Saturday
evening, and the ceiling at `soc_max` keeps an unreachable requirement
from understating what a later surplus can pay for. Proxied PV counts as
darkness, as it does everywhere else here. It subsumes Test 1's bridge
and handles two cases the bridge does not: a pin landing *inside* a weak
morning (Test 1 sees sun at the pin, calls the bridge zero and sells off
the morning's own charge before the night behind it), and the credit a
surplus straddling the pin genuinely earns.

The walk stops at the **last** forecast surplus block, not the first.
Stopping at the first ignores a weak day that cannot pay for the night
behind it; running to the edge of the lookahead demands cover for the
darkness after the final sunrise, which is a property of a 24 h window
rather than of the world, and would pin every sunny day at `soc_max`.

**The exception** is priced, not judged. A kWh may leave before the pin
only when

```
sell × discharge_eff  >  buy / charge_eff + wear
```

with `buy` the cheapest hour before the battery would hit its reserve and
`wear` the configured cycle costs. On this house — buy `1.25 × spot +
0.80`, sell `spot`, 0.10/kWh wear — a 0.20 night needs a 1.15 evening to
clear, so the exception sleeps through summer and wakes on winter peaks.
The credit is an *energy*, capped by what can physically leave in the
profitable hours and by the cover itself, so one expensive slot releases
one slot's worth rather than the night.

Only **published** buy prices qualify. Tomorrow night's curve is a proxy
for part of the tail on most runs, and a spread computed against a guess
is precisely the calculation that talks a battery into arriving empty.
Refusing it fails towards carrying the energy, which is the recoverable
mistake — the same asymmetry already applied to proxied PV.

**One grid refill is accepted**, and only one: when the whole lookahead
holds no forecast surplus at all, the requirement stops at the cheapest
hour instead. The rule refuses to buy at night in order to make room for
sun, and a week of December overcast is exactly the case where there is
no sun to make room for; without this the cover would run the full 24 h
and pin every winter run at `soc_max`.

## PV coverage past the horizon

Optimized mode is only as good as its view of tomorrow's sun. With the
Solcast profile's current default of *today + tomorrow*, coverage past
a 24 h horizon shrinks from ~24 h just after midnight to roughly zero
by late evening — exactly when the next day's end SOC is being decided.

Naming note: Solcast has **no `forecast_day_2` sensor** — *tomorrow*
is day 2. The consecutive extra day is
`sensor.solcast_pv_forecast_forecast_day_3` (then `day_4`, …), each
carrying the same half-hourly `detailedForecast` attribute (verified on
this HA: today/tomorrow/day_3/day_4 are consecutive days, 48 entries
each).

Three changes:

1. **Solcast profile template** (`profiles/builtin/pv/solcast.yaml`):
   add `sensor.solcast_pv_forecast_forecast_day_3` to the default
   `entities` list and reword the option description: today + tomorrow
   covers the horizon; one more day lets End SOC's Optimized mode see
   past it. Costs nothing — the profile reads the integration's local
   cache, no extra Solcast API calls.
2. **Engine: missing entities stop being fatal**
   (`profiles/engine.py::_read_attributes`): today a single missing
   entity raises `ProfileError` and kills the whole run. Relax to: warn
   and skip missing entities, raise only when *no* requested entity
   resolves (or no records result). A day-3 sensor that is briefly
   unavailable (integration reload, user disabled it) then just
   shortens the series instead of failing the optimisation — and short
   coverage already has a safety net, `build_payload`'s
   horizon-coverage warning. The unavailable-state case (entity exists,
   attributes stripped) already degrades gracefully via the
   attribute-`None` skip.
3. **Actionable hint instead of silent migration**: changed template
   defaults only affect *new* profile selections — existing installs
   (like this one) keep `[today, tomorrow]` in their stored options.
   Don't mutate stored options behind the user's back. Instead, when
   Optimized mode finds the PV tail shorter than ~12 h past the horizon
   *and* the selected profile is Solcast *and* an unselected
   `forecast_day_3` entity exists, raise a repair issue (the
   codebase's established pattern) suggesting adding day 3 to the PV
   profile. Other profiles just get the honest
   `pv_tail: "assumed zero"` annotation on the sensor.

Non-Solcast sources: `emhass_native` contributes settings rather than a
series, so Optimized mode there always runs with
`pv_tail: "assumed zero"`; `generic_attribute` depends on whatever the
user points it at.

## Load coverage

Most PV/price sources are calendar-day-shaped (Solcast, Nordpool), so the
tail's main risk is running out of *real* data. Load is the opposite
problem for one common profile: **"House load sensor"** (`load/sensor`,
likely the most-picked load profile for anyone with a whole-house meter)
sets `sensor_power_load_no_var_loads`/`load_forecast_method` and lets
EMHASS compute the load forecast itself, server-side. It has no
`source:` block, so `profile.produces_series` is `False` and the
coordinator's own `load` is *always* `Series.empty()` for it — not
short, genuinely absent. Without a fix, every Optimized decision on that
profile hits the "missing inputs" fallback (this module's `missing`
check, above) and quietly behaves as same-as-start on every single run.

EMHASS still computes a real load forecast for that profile every run —
it just never sends it back to the companion as its own artifact. It is,
however, sitting in the `p_load` column of whatever plan EMHASS last
actually solved. `coordinator._load_for_terminal` borrows it:

```python
def _load_for_terminal(self, load: Series) -> tuple[Series | None, str]:
    if load:
        return load, LOAD_SOURCE_PROFILE
    if self.data is not None and self.data.plan is not None:
        borrowed = self.data.plan.series("p_load")
        if borrowed:
            return borrowed, LOAD_SOURCE_LAST_PLAN
    return None, LOAD_SOURCE_PROFILE
```

The borrowed series' timestamps land within the *previous* run's own
horizon (last cycle's horizon, shifted by roughly one MPC interval) —
not in the tail. That is exactly what the load fallback above already
expects (`when - 24h` lands inside the horizon for a 24 h default), so
the borrowed series slots in with no further changes: one cycle stale
at worst, the same kind of imperfect-but-reasonable proxy the price
tail already relies on. `decide_end_soc` records which happened —
`load_source` (`LOAD_SOURCE_PROFILE` / `LOAD_SOURCE_LAST_PLAN`) rides
through into the Optimized decision's `details["load_tail"]` — the
module cannot tell a borrowed series from a real one on its own, so the
caller has to say.

**Scan scope, settled**: the replenishment search stays anchored at
`horizon_end`, not `now`. `soc_final` only takes effect at
`horizon_end` — EMHASS already optimises everything before that on its
own, regardless of what pattern this module notices there, so widening
the search to include it couldn't produce a better target. What
actually matters is good *data* for the part EMHASS can't see, which
the load-borrowing fix above delivers without needing to touch the
search window itself.

## Wiring

- `PayloadInputs` gains `soc_final: float | None = None`.
  `build_payload` sends it when set, and keeps the current
  `soc_final = soc_init` fallback when not. The existing comment block
  moves to the fallback branch and gets rewritten while at it: it
  describes the pre-0.18 hard equality, which is now a softly-enforced
  pin (see appendix).
- `coordinator._build()` calls `decide_end_soc(...)` after the series
  gather (both actions — day-ahead gets the same target for
  consistency, though the MPC is where it changes behaviour at the
  meter), passes `decision.soc` into `PayloadInputs`, and stores the
  `EndSocDecision` on the coordinator data so entities can read it.
- `diagnostics.py` includes the last decision.

## The sensor

`sensor.<name>_end_soc_target` — one per config entry, on the main
device, description-based like the existing plan sensors.

- **State**: the target as a percentage. `device_class: battery` (right
  unit and icon); no `state_class` — it is a setpoint, not a
  measurement, and long-term statistics on it are noise.
- **Attributes** (the explanation the value needs to be trusted):
  - `mode` — `optimized` / `same_as_start` / `fixed_50`
  - `reason` — one human sentence, e.g. *"Holding 3.2 kWh to bridge
    evening load until cheap prices at 02:00; solar expected to refill
    from 09:30."*
  - `replenishment_kind` — `solar` / `cheap_grid` / `none`
  - `cover_energy_wh` / `cover_target` — what the night needs on top of
    the reserve, and the SOC that represents
  - `cover_until` — when the battery would otherwise touch the reserve
  - `sale_energy_wh` / `sale_margin` / `sale_sell_price` /
    `sale_buy_price` — present when the priced exception fired
  - `sale_blocked` — present when it did not, with which of the three
    reasons (no sell price / prices unpublished / no spread)
  - `reserve` — the floor used (from `soc_target`)
  - `clamped_by` — `night_cover` (nothing bit) / `profitable_sale` /
    `reserve` / `range` / `hysteresis`. `range` also covers a night
    longer than the battery: a bound that bites in silence is how a
    heuristic gets believed for the wrong reason
  - `unreachable` — present and `true` when the target likely exceeds
    what charge/discharge power can deliver within the horizon (EMHASS
    will aim for it and fall short, softly)
  - `price_tail` — `known` / `assumed from today`
  - `load_tail` — `own forecast` / `borrowed from last plan`
  - `fallback` — present with the cause when the heuristic could not run
- Unavailable while the battery is disabled; in the non-optimized modes
  the sensor still reports (state + `mode` + `reason`), so the dropdown
  and the sensor never disagree.

`docs/entities.md` gains a row for it when implemented.

## Tests

- `tests/test_terminal.py` — pure-function scenarios:
  - evening before a cheap night → target near reserve (discharge
    allowed through the peak);
  - night before a sunny surplus day → low target, solar bias engaged;
  - no PV tail, expensive tail → target covers the bridge energy;
  - missing price tail before 13:00 → today's curve proxy used and
    flagged;
  - target beyond power-limit reach → `unreachable` annotated, value
    *not* clamped;
  - ±2 pp jitter → hysteresis holds the previous value;
  - no tails at all → same-as-start fallback with explanatory reason;
  - `same_as_start` and `fixed_50` passthroughs;
  - `load_source` defaults to `own forecast`, rides through verbatim to
    `details["load_tail"]` when the caller passes `borrowed from last
    plan`.
- `tests/test_payload.py` — `soc_final` passthrough when set; existing
  pin-to-`soc_init` behaviour preserved when unset.
- Config-flow test: `end_soc_mode` stored, default `optimized`,
  round-trips through the options flow.
- Engine test: one missing entity out of three → warn + skip, records
  from the remaining two; all entities missing → `ProfileError` as
  before.
- PV-tail tests: series ending at horizon → `pv_tail: "assumed zero"`,
  no solar bias; Solcast profile selected without day 3 while the
  entity exists → repair issue raised.

## Appendix: EMHASS end-SOC mechanics (≥ 0.18, verified 2026-08-04)

Checked against the v0.18.0 tag (and v0.17.9 / master for comparison).
Three similarly-named knobs that are *not* the same thing:

- **`soc_final`** (runtime param — our delivery mechanism): a
  throughput constraint per battery,
  `total_energy_change == (soc_init − soc_final) · cap`, **soft since
  0.18**: under/over slack variables enter the objective priced at
  `SOC_FINAL_DEVIATION_PENALTY_FACTOR = 100` × the dearest positive
  import price (floored so it stays meaningful at zero/negative
  tariffs). Effectively still a pinned target — 100× the worst price is
  never worth trading away — but an unreachable value now degrades
  gracefully with a solver warning instead of an infeasible run, which
  is why this plan has no feasibility clamp. On ≤ 0.17.x the same
  constraint is a hard equality (that's the world `payload.py`'s
  current comment describes), hence the ≥ 0.18 backend floor.
- **`battery_target_state_of_charge`** (config key, what we send in
  `_battery_settings`): read *only* as the fallback default when
  `soc_init`/`soc_final` are not passed at runtime — an unpassed
  `soc_final` first defaults to `soc_init`, and the config key is only
  reached when *neither* is passed. We always pass both, so it is
  inert. Kept in the payload per the assert-every-setting convention;
  companion-side it becomes the Optimized mode's reserve floor.
- **`soc_target` + `soc_target_timestep`** (runtime params, issue
  #553): an optional stored-energy **floor (≥)** at *one specific
  timestep* of the horizon — defaulting to the **last** timestep when
  no timestep is given. It coexists with the `soc_final` constraint and
  cannot replace it (a lower `soc_final` pin plus a higher end floor
  just fights the penalty). Useful later for mid-horizon guarantees
  ("≥80% before the EV leaves at 17:00"), not for this feature.

Also relevant from 0.18: batteries are per-index lists internally
(#610) and `soc_init`/`soc_final` accept either a bare float (broadcast
to every battery) or a per-battery list. The companion is
single-battery and keeps sending scalars, which is the unchanged
`number_of_batteries == 1` calling convention.

## Appendix: live A/B validation (2026-08-04)

Ran against the real EMHASS (v0.17.9) with the forecasts from that
evening's actual MPC run (22 kWh battery, 24 h horizon 23:00→22:45, a
low-solar day peaking ~2.6 kW, prices 1.00–1.48 SEK/kWh): same
`soc_init` = 0.85, deferrable loads dropped, only `soc_final` varied.

| `soc_final` | 24 h cost | grid import | export |
|---|---|---|---|
| 0.85 (today's pin) | 10.93 SEK | 10.3 kWh | 0 |
| 0.60 | 5.00 SEK | 4.7 kWh | 0 |
| 0.30 | **−0.46 SEK** | **0 kWh** | 1.8 kWh |

The pinned plan showed both predicted failure modes: the battery idled
at ~0.78 all night while the grid carried the whole house load, and at
14:15 it imported **8.6 kW** at the day's price floor purely to force
the battery to 100% in time to ride back down to the pin. The freed
plans ran the night on battery with zero import and exported the
morning PV instead of hoarding it.

Caveat: gross numbers overstate the net win — energy released below the
start SOC must be re-bought after the horizon, so the true benefit is
the price spread plus the avoided forced buys, which is exactly the
trade-off `decide_end_soc` exists to judge. Note also that the
configured reserve (`soc_target`, here 0.5) floors what Optimized mode
may free: on this day the heuristic would have landed around
0.6–0.7 (reserve 0.5 + ~5 kWh bridge to the next cheap night hours),
capturing roughly half the 0.30 run's gross gain.

## Out of scope / future work

- **Horizon anchoring** — letting the effective horizon end at the last
  known price instead of a fixed `horizon_steps` would make the tail
  analysis stronger (separate change, touches scheduling).
- **Candidate sweep (v2)** — solve the same MPC for 3–5 candidate end
  SOCs, score each by EMHASS's reported cost minus the terminal energy
  value, pick the winner. Strictly more exact; costs extra EMHASS calls;
  reuses this feature's plumbing (it is just a better `decide_end_soc`).
- **Upstream terminal value (endgame)** — an EMHASS option that drops
  the `soc_final` equality and adds `−value · soc_end` to the objective
  would let the solver choose the end SOC itself; the companion would
  then only estimate the €/kWh value of stored energy. Worth a PR to
  `davidusb-geek/emhass` once the heuristic has proven the concept here.

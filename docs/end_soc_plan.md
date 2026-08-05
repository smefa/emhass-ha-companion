# End SOC (planned)

*Status: planned — nothing here is implemented yet.*

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
| **Fixed (50%)** | `fixed_50` | `soc_final = 0.5`, feasibility-clamped |

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
   as far as asked; PV extends as far as the source forecasts (Solcast
   and friends give 2+ days); the **price tail is the binding one**.
   Before ~13:00 tomorrow's Nordpool prices don't exist, so when the
   price tail is shorter than ~2 h, substitute today's known curve
   shifted 24 h as a statistical proxy, and mark the decision
   `price_tail: "assumed from today"` so the sensor shows it.
2. **Find the next replenishment event** after `horizon_end` — the
   earlier of:
   - *solar*: the first PV-surplus block (`pv − load > 0`) whose
     cumulative surplus reaches a material fraction of capacity
     (~10%), timestamped at the block's start;
   - *cheap grid*: the first price at or below the 25th percentile of
     the full known buy-price series.
3. **Bridge energy**: `E = Σ max(0, load − pv) · step` over
   `[horizon_end, replenishment)`, divided by discharge efficiency —
   the energy the house needs from the battery before it can be
   refilled cheaply or for free.
4. **Raw target**: `reserve + E / capacity`, where `reserve` is the
   existing `soc_target` setting, repurposed to its natural meaning of
   "comfort/backup floor the plan should always end with". `soc_target`
   is still sent to EMHASS as `battery_target_state_of_charge`,
   unchanged: EMHASS only reads that key as the *default* end SOC when
   no `soc_final` is supplied, and we always supply one, so it is inert
   there — but the companion's convention is to assert every setting on
   every request so EMHASS's persisted config never silently decides,
   and "default end SOC" stays semantically compatible with the floor
   reading if that fallback ever fired.
5. **Solar bias**: if the tail shows tomorrow's surplus refilling the
   battery from the raw target to `soc_max` before the first expensive
   block, do not round the target up further — leftover SOC at sunrise
   is only worth the export price, so holding extra costs the
   buy/sell spread.
6. **Clamps, in order** (each recorded in `details["clamped_by"]`):
   - range: `[soc_min, soc_max]`;
   - **hysteresis**: if `previous` exists and the new target is within
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
  - `next_replenishment` / `replenishment_kind` — timestamp and
    `solar` / `cheap_grid`
  - `bridge_energy_wh` — the E from step 3
  - `reserve` — the floor used (from `soc_target`)
  - `clamped_by` — `none` / `range` / `hysteresis`
  - `unreachable` — present and `true` when the target likely exceeds
    what charge/discharge power can deliver within the horizon (EMHASS
    will aim for it and fall short, softly)
  - `price_tail` — `known` / `assumed from today`
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
  - `same_as_start` and `fixed_50` passthroughs.
- `tests/test_payload.py` — `soc_final` passthrough when set; existing
  pin-to-`soc_init` behaviour preserved when unset.
- Config-flow test: `end_soc_mode` stored, default `optimized`,
  round-trips through the options flow.

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

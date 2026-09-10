# Battery lockout

Keep the battery from feeding one specific load — the car charger, most often
— without touching how the battery serves everything else.

EMHASS solves one house-wide power balance per timestep. It has no notion of
*where* a kWh came from, so there is no native "this load may not use the
battery" constraint. The only blunt levers without this feature are global:
raise the minimum charge level, raise the discharge cycle cost for the whole
horizon, switch `select.emhass_mode` to *Idle* (which also suspends every
deferrable load and blocks charging), or turn the battery off entirely.

## Setup

Turn on `switch.<load>_battery_lockout` on the load you want to protect. That
is the whole user-facing feature — one switch, off by default, so every
existing install is byte-identical until it is used.

| Entity | |
|---|---|
| `switch.<load>_battery_lockout` | On = price the battery out of this load's window (see below). Off (default) = no change to today's behaviour |
| `lockout_held_start` / `lockout_held_end` attributes | The latched window derived from the previous plan, if one is currently held |
| `lockout_running` attribute | Whether the load is drawing right now and contributing its own window on top of the held one |

## How the window is derived

Two sources, unioned:

- **While running.** From the moment the load is observed drawing power, for
  however much of its target run time is left. Needs no plan and nothing
  latched — it can never oscillate, because there is nothing fed back into it.
- **The previous plan's own schedule for this load**, latched once derived and
  held until the run it covers ends (or its own end passes, for a window that
  never actually saw the load run). *Not* re-derived every cycle: an early
  version of this feature tried that and found — against a real EMHASS
  install, with nothing external changing between two-minute forced
  recalculations — that the unlatched window moves on its own by a step or
  more. That is solver-level jitter, not a property of one tariff, so
  re-deriving it every run would feed that jitter straight into the next
  request and chase itself indefinitely.

**The two are never merged into one span.** A load can be started well outside
its own held window — a manual on-demand start hours before its scheduled
slot, say — while that held window hasn't reached its end yet and so hasn't
released. Merging would then lock the battery out for the entire gap between
the two windows, not just the two real ones. Internally each load carries up
to two separate windows, and every timestep covered by *any* window on *any*
lockout-enabled load is priced — a per-timestep union, same as when two
different loads are both flagged.

## How it's enforced

A large price on `weight_battery_discharge`, for exactly the priced steps,
derived per run as `max(100 × the horizon's own highest buy price, 100.0)` —
never a fixed number, since that would be wrong at a different currency scale.
When the hub cost function is **Maximize self-consumption**, that price is
multiplied by EMHASS's own self-consumption bigM (`1000`): that costfun marks
grid import up by the same factor while leaving the discharge weight unmarked,
so without the scale-up the lockout still loses to battery-over-grid inside the
window. Profit and minimize-cost are unchanged.

This is **weight-only**:

- **Soft.** A cost, not a constraint, so it can never make the solve
  infeasible however large it is. There is real headroom too: against a
  4.0-currency/kWh price spread under profit/cost the true break-even for *any*
  discharge sits around 4.4, and this feature's default prices roughly 20×
  above that.
- **Discharge-only.** The battery may still *charge* through a locked-out
  window — on surplus PV, say — while the flagged load draws from the grid.
  That is the strongest reason to prefer this over an inverter-level lockout
  or `select.emhass_mode` → *Idle*, neither of which can express "charge yes,
  discharge no" for one window.

The held window's start is latched and does not chase replan jitter, but its
**end may grow** when the previous plan's first contiguous block still overlaps
the latch and runs later — so a schedule that lengthens across MPC cycles stays
covered without collapsing two disjoint blocks into one span.

## What it does not do

**It relocates discharge, it does not preserve charge.** If the battery still
has to reach its end-of-horizon SOC target, locking it out of one window just
moves the same discharge earlier or later — the battery can still end the day
empty, having simply avoided feeding it at *that* moment. Pair this with the
minimum charge level if the goal is "make sure there is still charge left for
this load's window": the two are complements, not alternatives.

**It does not close the self-consumption gap.** The executor hands the
battery to the inverter's own logic whenever a plan row's `|P_grid|` falls
inside `self_consume_threshold_w` (300 W by default) — regardless of what the
plan says or any lockout price. For an 8 kW car charge that band is
irrelevant; for a small flagged load on a sunny day it is real, and no setting
here changes it. A `battery_discharge_power_max` override or an executor-side
block would close it, but neither ships with this feature — see the plan
document for why.

## See also

- [Deferrable loads](deferrable_loads.md)
- [Surplus loads](surplus_loads.md) — the other place a load's window is
  derived from a plan the load itself doesn't control
- `planning/battery_lockaout_plan.md` in the repository, for the full design
  rationale and the live-system tests behind the numbers above

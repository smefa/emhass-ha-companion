# Surplus loads

A load that only ever wants energy which would otherwise leave the house.

The motivating case is a pool heater: 800 W, on or off, and worth running only
when the sun is producing more than the house and its other deferrable loads
need. There is no deadline, no target run time and no "hours per day" — it
takes what is spare for as long as it is armed. *When* it may run is decided
without any regard for the battery or the grid — see [the add-back](#the-add-back)
for why — but *how much* it is asked for still leaves room for the battery to
reach its own target first; see [the battery reservation](#the-battery-reservation).

Choose *On spare solar* under **Runs** when adding the load, then arm it with its
**Requested** switch. Turn the switch off to stop. That is the whole
user-facing feature.

![The Add a deferrable load dialog, with On spare solar chosen under Runs](assets/deferrable-add-load.png)

![A surplus load's Controls and Sensors: Requested switch, Run now button, next start, running, runtime today, scheduled power, should run](assets/surplus-load-controls-sensors.png)

Adding a deferrable load asks two questions first — its name and what makes it
want to run — because that answer decides which settings it even has. A config
flow cannot show or hide a field in reaction to another field in the same form,
and a surplus load has no operating hours and no time window to offer. An
existing load can be switched over at any time with its `recurrence` select.

## Why this cannot be an ordinary deferrable load

EMHASS's cost function is imports minus export revenue. It has **no value term
for consumption at all**, so any load it is not forced to run is pure cost and
the solver sets it to zero at every timestep. There is no way to express "run
this if it happens to be free" in the MILP.

The only lever is `def_total_hours`, which is a hard constraint. So a surplus
load's run time has to be computed outside the optimiser, from the surplus the
plan itself predicts, and handed back as an ordinary hours target.

## How the budget is derived

Once per run, in `surplus.py`, from the **previous** run's plan:

1. **The series.** `max(0, P_PV - P_load - ordinary deferrables)` — PV the
   house and its *ordinary* scheduled deferrables don't need, plus the surplus
   loads' own `P_deferrable` added back (see below). Deliberately not
   `-p_grid`: what the battery charges on, or what gets exported, is the
   optimiser's call about where to put spare PV, not a statement about
   whether there was any. Reading `-p_grid` instead would let the battery
   silently outrank every surplus load for the same watt, any time it still
   has room to charge.
2. **The current block.** From here to the first stretch that can't even feed
   the load's run floor — the run floor being its nominal power when it can
   only be on or off, and its minimum power when it can modulate. A horizon
   that starts mid-morning also reaches past sunset into tomorrow's sunrise;
   without this cut, the block would splice today's tail onto tomorrow's,
   and everything below would be sized against a "day" that is really two
   days with a night spliced out of the middle.
3. **Qualifying timesteps**, within that block. Those where the surplus is at
   least the run floor plus *headroom* — on the gross series, before the
   battery's own reservation is taken off (see below).
4. **The window.** First to last *gross*-qualifying timestep *in the block*,
   sent as an absolute `start_at`/`end_at` pair rather than a wall-clock one.
5. **The budget.** `hours = usable energy / nominal power`, where each
   timestep's contribution is clipped at what the load can actually take —
   this time from the *net* series, after the battery's reservation.

The load is then sent to EMHASS as a completely ordinary deferrable with those
hours and that window. The optimiser still decides *when* inside it — but only
inside it. Without the block cut, a window spanning a whole night is still a
technically valid window, and EMHASS will place the load at 3 a.m. if that
happens to be the cheapest way to fill it — solar never entered its decision,
only the (wrongly wide) window did.

### The add-back

Only *ordinary* deferrables are subtracted from `P_PV - P_load`. Every surplus
load's own `P_deferrable` is left out of the subtraction entirely, which makes
the series **invariant to whether those loads are scheduled**: a surplus load
that is currently running must not see its own draw counted against next
cycle's budget, or it would switch itself off every time it switched on. An
ordinary deferrable's consumption stays subtracted because it is a real claim
on the house's energy — leaving it out too would promise the same
kilowatt-hour to the pool as well, selling it twice.

### The battery reservation

The window is gross, but the *hours* asked for are net of one more thing: what
the battery's own charging already claims from the same slot, read straight
from the previous plan's `P_batt` (`battery_reserved_series` in `surplus.py`).
That plan was solved honouring `battery_target_state_of_charge` already — SOC,
timing, price, efficiency, all of it — so reusing its own charging schedule is
both simpler and more faithful than re-deriving "energy still needed to reach
70 %" from SOC and capacity here and risking it disagreeing with what EMHASS
actually intends to do.

This narrows `hours`, never the window: a slot the battery is charging hard on
is still gross-qualifying, so it still counts towards *when* the load may run,
it just may contribute little or nothing towards *how much*. Keeping the two
separate is what stops this turning back into the `-p_grid` bug — a battery
that is still short of its target must not be able to push the load's start
out, only to leave it fewer hours once it's running.

Sharing among multiple surplus loads (priority order, see above) still comes
out of what is left *after* the reservation: the battery's claim is taken off
the top before the highest-priority surplus load even sees the series.

### A currently-running unbroken load whose window moved on

*Must run in one unbroken block* (`single_constant`) and the surplus window
interact badly if left alone. EMHASS pins a currently-on single-constant load
to run from t=0 for its full requested duration and widens the window mask to
fit — an unbroken block can't be split, so it just lets an in-progress one run
to completion rather than interrupt it. That is correct when the window still
covers now: the pin is simply continuing the block that opened it. It is wrong
when the block that justified starting has ended and the window has moved to
a later, unrelated one (this page's motivating case: dishwasher claims the
last of today's sun, and the next real block is tomorrow) — the pin would
still fire, off the *new* block's fresh multi-hour total, starting immediately
on grid or battery power the load was never meant to draw.

Because a surplus load always reports zero completed timesteps (see below),
there is no "remaining" figure for EMHASS's own progress tracking to release
it with, either — every cycle re-pins a full fresh block, so left alone this
does not merely run once, it can keep re-arming indefinitely.

The fix is in `payload._describe`: when a load is both `current_state` (really
running) and `single_constant`, and its freshly-resolved window's start index
is past zero, its hours are sent as 0 for that request instead of the later
block's total. With no operating requirement, EMHASS has nothing to pin and is
free to plan the load off starting now. The later block is asked for normally
on its own cycle, once "now" actually reaches it and `current_state` has had
the chance to catch up. A load that has not started yet is unaffected — its
future window is the ordinary, correct way to ask for it — and so is a
non-single-constant load, which only ever gets a one-timestep force-on from
EMHASS's current-power pin, not a multi-hour one.

### Why the previous plan

This is a lagged single pass. Solving twice per cycle — once to find the
surplus, once with the load in it — would feed the second solve's output back
into its own input, and the budget would oscillate between runs. Using the last
plan costs one MPC interval of lag on a load that heats a swimming pool, and
the whole budget is recomputed from scratch every cycle, so an error corrects
itself immediately.

Before the first optimisation after a restart there is no plan, so the budget is
empty and the load is parked. It picks up on the next run.

### Why no completed timesteps

`def_current_operating_timesteps` is sent as **zero** for a surplus load.

The budget is derived from the *forward* surplus, which shrinks on its own as
the day's sunny timesteps fall behind the horizon — so the hours being sent are
already "what is left". Letting EMHASS subtract completed timesteps as well
double-counts every hour served: a pool two hours into a four-hour surplus
would be told it needs two more hours and has already done two, and would stop
at noon with half the sun still ahead of it.

`def_current_state` is still sent, so a running load is not charged a startup
penalty twice.

## Settings

![A surplus load's Configuration section, with hours/window fields greyed out and Spare solar headroom, Energy needed and Surplus priority active](assets/surplus-load-configuration.png)

Greyed-out fields above are the ones a surplus load has taken over — see the
table below for exactly which.

| Setting | Surplus load |
| --- | --- |
| Power when running | **configured** — the load's rating, and its run floor when on/off |
| Runs at full power only | **configured** |
| Lowest power while running | **configured**, when the above is off |
| Cost of starting | **configured** |
| Energy needed | **configured** — optional cap, 0 for none |
| Spare solar headroom | **configured** |
| Surplus priority | **configured** — only matters with more than one surplus load |
| Hours needed per day | *derived* |
| Restrict to a time window | *derived* |
| Run within | not applicable |

The two power settings are the same pair every deferrable load has, and they
interact the same way: with *Runs at full power only* on, the load is either off
or at its nominal power, and *Lowest power while running* is not read.

They also decide how the budget is counted, which is easy to get wrong:

- **Full power only** — the load draws exactly its nominal power whenever it
  runs, so a timestep offering 3 kW still only feeds an 800 W pool 800 W. The
  budget reduces to a slot count. Summing each slot's own energy instead would
  over-commit the pool by nearly 2.5x, and EMHASS would import the difference.
- **Variable** — the budget is the clipped energy sum. The clip matters as soon
  as the load's own maximum is below the day's peak surplus.

  The *ceiling* EMHASS is given for the run is not always the configured
  nominal power, either. It is clamped down to the best surplus actually seen
  in a qualifying slot this run — so an 11 kW charger on a 3 kW day is sent as
  a 3 kW load, not an 11 kW one. Without that clamp, `hours` is computed
  against a ceiling the day cannot reach, which understates the hours needed
  and leaves EMHASS free to run near the unreachable ceiling for a short
  burst instead of at the achievable power for the whole window — importing
  the difference either way. The configured value is still a hard cap in the
  other direction: the clamp only ever pulls the ceiling down, never raises
  it past what the appliance can actually take. A full-power-only load has no
  ceiling to clamp — it is always either off or at its configured power.

### Headroom

The margin a timestep must clear *above* the load's own draw before it counts.

Its main job is absorbing PV forecast error: a slot counted at exactly the
load's power imports the moment the forecast comes in light. It also opens a
band — between the load's power and the load's power plus the headroom — of
timesteps that do not add to the budget but are still safe to run *through*,
because the load's draw still fits inside the surplus there.

That band is why **Cost of starting** is the right way to keep an on/off load
from short-cycling, rather than *Must run in one unbroken block*. A startup
penalty prices each start, so EMHASS runs one contiguous block on a clear day
(splitting costs the penalty and buys nothing) and splits across two peaks on a
broken-cloud day (straddling the gap costs more than the penalty). One unbroken
block is a hard constraint, and on a day with a deep midday gap it will drive
the load through that gap and import to satisfy itself.

### Energy needed

A total, not a rate: "put 20 kWh in the car and stop". Zero means no cap, which
is the pool's case and the default.

Progress is tracked locally against the current request and subtracted from the
cap; it is never sent to EMHASS (see above). When the cap is met the request
disarms itself. Without a cap the load stays armed until the switch is turned
off — a temperature limit is then an automation that turns the switch off, not a
feature of the load.

The cap is exact for a full-power load, where delivered energy is runtime times
nominal power. For a variable-power load it over-states delivery whenever the
load modulates below nominal, so the cap is reached early and the load stops
short rather than over-running.

## Several surplus loads

They share one series, allocated by **Surplus priority** (lower claims the
series first, ties broken by name), with each load's draw subtracted from what
the next one sees.

Without that every surplus load reads the same number, all of them are told the
whole surplus is theirs, and running two together imports the difference. The
optimiser cannot arbitrate it, because none of these loads is in the plan the
budget was derived from.

Priority only matters once there are two or more surplus loads competing for
the same series — with a single one there is nothing to be first *ahead of*.
Every load defaults to the same priority, so nothing changes until you set one
lower than another's — a car charger given a lower number than a pool, say,
takes what it needs first and leaves the pool whatever surplus is left.

## What is published

On the hub device:

- `sensor.…_solar_surplus` — spare solar now, with the whole series in
  `forecast` and the reporting window in the attributes
- `sensor.…_solar_surplus_energy` — kWh still ahead in the horizon. Usually the
  more useful trigger: whether to start heating a pool is a question about
  kilowatt-hours coming, not watts at this instant
- `sensor.…_solar_surplus_start` / `_end` — the reporting window's ends
- `binary_sensor.…_solar_surplus` — above the reporting threshold now
- `number.…_surplus_threshold` — what those four call a surplus

That threshold is **reporting only**. No load budgets against it: a load's own
threshold is its run floor plus its own headroom, because "is there enough spare
sun for the pool" and "…for the car" are different questions.

Per load, `sensor.<load>_surplus_budget` reports the hours the last run was
allowed to ask for, with the window, the energy, the qualifying threshold and
any remaining cap as attributes. A load that is not running is either short of
surplus, short of headroom, or was beaten to it by a higher-priority load, and
those look identical from the outside without it.

![A surplus load's Diagnostic section: EMHASS deferrable number and Spare solar budget](assets/surplus-load-diagnostic.png)

Also diagnostic: `sensor.<load>_emhass_deferrable_number` — see
[on-demand loads](on_demand_loads.md#deferrable-numbering-stays-stable) for
why it stays stable.

## What this is not

Exported power is worth the sell price, so consuming it here forgoes that
revenue — it is not free energy, it is simply the only energy no other part of
the plan has already claimed. On a tariff where selling pays more than the load
is worth to you, a surplus load is a bad trade, and nothing here will notice.

# Dynamic grid limits

The grid step asks for **Maximum import power** and **Maximum export power**,
and those two numbers go straight to EMHASS as `maximum_power_from_grid` and
`maximum_power_to_grid`. For most plants a constant is the right answer.

It isn't when the usable limit moves:

- **An unbalanced three-phase connection.** There is no single fuse rated at
  the sum of the phases — there are three fuses, one per phase, and each only
  sees its own. The connection delivers its full rating only when all three
  phases are loaded equally. This case is built in: see
  [the phase guard](#three-phase-imbalance-the-phase-guard) below, which
  needs no sensor of your own.
- **Load balancing or a dynamic main fuse.** Anything that hands out a
  capacity allowance that changes minute to minute.
- **A curtailment order from the network operator**, typically on the export
  side.

For these, the grid step also takes an optional **Import limit sensor** and
**Export limit sensor**. When set, the sensor's value — plain watts — is used
in place of the fixed number on every run.

## How the sensor is used

- **It can only lower the fixed limit, never raise it.** The number you typed
  stays the connection's physical rating. A template built on the wrong fuse
  size can make the plan needlessly cautious; it cannot invite EMHASS to plan
  through a fuse.
- **Unreadable means unchanged.** Unavailable, non-numeric, or negative, and
  the fixed number is used for that run, with a warning in the log. A broken
  template never stops the plan.
- **The import limit is floored at the load that flows regardless.** Below the
  house's own forecast draw, EMHASS answers *infeasible* rather than answering
  with a smaller plan — so a limit under that value is raised back to it and
  the run reports that it did. The floor comes from the load forecast's peak
  over the horizon, or from the live load reading when the load profile leaves
  EMHASS to build its own forecast.
- **The export limit has no floor.** Nothing has to leave the property the way
  the house load has to be served. One exception: with **Let EMHASS optimise
  PV curtailment** off, surplus solar has nowhere else to go, so an export
  limit below your peak surplus makes the problem infeasible. Turn curtailment
  on if you're going to constrain export.

Both limits sent from the grid step and the sensors above are single values
per run, applied to every timestep alike — a day-ahead run therefore applies
one instant's reading across the whole horizon. See [Smoothing](#smoothing)
below.

That is not, however, "all EMHASS accepts": `maximum_power_from_grid` also
takes a per-timestep list, and this integration sends one when a network
tariff's `capacity_limit` (or its windowed-demand-charge fallback) is
configured — see
[Network tariffs, "The windowed hard cap"](network_tariffs.md#the-windowed-hard-cap).
When it does, the scalar described above still sets the ceiling for every
timestep *outside* that window; the array only ever lowers timesteps inside
it, never raises anything past what the grid step and the sensors already
allow.

## Three-phase imbalance: the phase guard

There is no single fuse rated at the sum of the phases. There are three fuses,
one per phase, and each sees only its own current. A battery charging evenly
across the phases while a 2.2 kW dishwasher heater runs on L1 can put L1 far
past its fuse while the total is still well inside the connection:

| | L1 | L2 | L3 |
|---|---|---|---|
| House | 400 | 300 | 300 |
| Battery charge | 2870 | 2870 | 2870 |
| Dishwasher heater | 2200 | – | – |
| **Total** | **5470 W ≈ 24 A** | 3170 | 3170 |

On a 16 A gG fuse (Diazed, Neozed), that is a fuse that blows within minutes.
A gG fuse runs at its rating indefinitely and holds 1.25× its rating (20 A on
16 A) for up to an hour. At 1.5–2× (24–32 A) it blows within a few minutes,
and at the top of that range within about a minute.

The plan can't prevent this on its own. It sees the phases only when it
solves, it assumes added load spreads evenly over the phases, and it sees an
appliance's average power, not its bursts. So the integration guards the fuse
itself, from your meter's per-phase readings.

### Setting it up

Under **Advanced settings** on the grid step:

- **Phase L1 / L2 / L3 reading** — your meter's live reading for each phase,
  in W, kW or A. The unit is taken from the sensor. Current is best, since
  that is what the fuse responds to; watts are converted at the phase voltage
  below. Fill in all three phases, or only L1 for a single-phase connection.
  A sensor whose unit can't be read as power or current counts as unreadable.
  It is never guessed at, because amps read as watts would make the phase look
  230 times emptier than it is.
- **Main fuse per phase** — the fuse rating in A: 16, 20, 25… Leaving it
  empty turns the guard off, even with the phase readings still filled in.
- **Fuse margin** — how far below the rating every phase is kept. Default
  1 A. It covers meter lag, power factor, and a load that switches on between
  two readings. Because a gG fuse holds a modest overload for a long time,
  the margin doesn't have to absorb the seconds the guard takes to react.
- **Phase voltage** — for converting watt readings to current. Default 230 V.
- **Voltage sensor** — optional. A live phase voltage from your meter, used
  in place of the fixed voltage while it reads within 20% of it. A heavily
  loaded phase sags, and the same watts are then more amps, which is exactly
  when it matters. Use L1's voltage, or the lowest of the three if your meter
  publishes one. A reading further off than that (a 400 V line-to-line sensor
  picked by mistake, a unit mix-up) is ignored with a warning in the log, and
  the fixed voltage is used instead. Not needed if your phase readings are
  already in A. The voltage in use shows in `sensor.*_phase_headroom`'s
  `voltage_v` / `voltage_measured` attributes.
- **Phase limit look-back** — see [The plan's limit](#the-plans-limit).
  Default 30 minutes.

If you were using a hand-written import limit template for this, clear it
from **Import limit sensor** and delete it. If you keep both, the plan uses
the lower of the two.

### The plan's limit

The plan gets an aggregate import limit worked out from the worst phase. Load
added symmetrically puts a third of its power on each phase, so the worst
phase reaches its limit after `n · (limit − worst)` more:

```
limit = (I1 + I2 + I3) + 3 · I_limit − 3 · max(I1, I2, I3)        × voltage
```

`I_limit` is the fuse rating minus the margin. The phase readings are not
clamped at zero: a phase that is exporting really does lower what the others
may import. The result is clamped at zero instead, because it only goes
negative once a phase is already over its limit.

The plan doesn't use the reading from the moment it solves. It uses the
**lowest** value over the look-back window. Otherwise a dishwasher that
happens to be between heating cycles at 22:00 hands the plan the whole fuse
for the night. Worst case of the last half hour is the right model for a
thermal fuse. Set the look-back to 0 to use the live reading.

The value then goes through the same rules as any
[import limit sensor](#how-the-sensor-is-used). It never raises the fixed
maximum, and it is lifted back to the house's own forecast draw if it falls
below it.

### The real-time clamp

The plan's limit is a single value per run, and the plan only runs every
15–30 minutes. Real protection has to react between runs, so the executor
checks every meter update.

It needs two things, and without either one the clamp stays off. A repair
in **Settings → Repairs** says which one is missing. The plan's limit doesn't
depend on either and still applies.

- **A battery power sensor**, set on the battery step. The guard has to
  *measure* what the battery is doing. A charge the inverter hasn't started
  yet (ramping, a BMS taper, solar covering it on the DC side) or a battery
  that is discharging would otherwise make the house look lighter than it
  is, and the guard would allow too much.
- **An inverter profile that can be written within 30 seconds.** A profile
  that allows a write only every few minutes (the iSolarCloud cloud profile
  waits 300 s) can't carry a cut before an overloaded fuse runs out of time.
  Local profiles over Modbus have no such delay.

How it works:

- **Only a forced charge is touched.** That is the one battery action that
  adds load to every phase on purpose. Discharge and self-consumption take
  load off the grid.
- **The battery's own share is taken out first.** From the measured battery
  power, the guard works out what each phase would carry with the battery
  idle: it subtracts a charge, and adds a discharge back. Then it allows as
  much symmetric charging as fits under the worst phase's limit.
- **The command never goes over the cut.** The inverter profile's charge
  boost and rounding are applied before the cap, and rounding goes down: a
  2,550 W cap on a profile that writes in steps of 100 W sends 2,500 W.
- **The cut is immediate. The release waits.** A climbing phase cuts the
  charge on the next reading. The charge comes back only after every phase
  has stayed clear for 60 seconds, so a heater cycling on and off doesn't drag
  the inverter up and down with it.
- **Too little room hands the battery to self-consumption.** If less than
  300 W of charging fits, there's no point forcing a token charge. On a hybrid
  inverter, self-consumption also lets the battery cover the house, which
  takes load off the phase that is in trouble.
- **The guard fails open.** If a phase reading or the battery power reading
  goes unavailable, charging follows the plan unlimited until it is back,
  with a warning in the log.
  That matches how the import limit sensor behaves: a meter integration
  restarting must not stop the battery charging.

The clamp also runs while **Control** is off, so you can watch what it would
have done before you hand over.

### What you see

| Entity | |
|---|---|
| `sensor.*_phase_headroom` | Symmetric charging the worst phase has room for right now, in W, with the battery's own charge taken out. It reads the same whether the battery is charging or not. Negative means a phase is over its limit before any charging. The attributes hold each phase's current, the worst phase, the fuse, the planner limit and the overload state |
| `binary_sensor.*_phase_guard_active` | On while the executor is holding the charge below the plan. The attributes hold the planned charge, the allowed charge and the watts cut |

`sensor.*_battery_action` also shows the cut in its `rules` trace and in
`planned_charge_w` / `phase_cut_w`. A phase above the fuse rating is logged
as a warning, marked *light* up to 1.25× the rating and *heavy* beyond.

`phase_headroom` is also meant for automations. A three-phase EV charger the
integration doesn't drive can hold back by it. The battery gives way first,
because the guard cuts the battery's charge before the charger has to slow
down.

### What it doesn't cover yet

- **Only the battery is throttled.** Deferrable loads are switched on and
  off, not modulated, and a car charger isn't driven by power setpoint yet.
  If the house alone is over the fuse, the guard can't do more than stop
  charging. You will see a *heavy* warning in the log.
- **It assumes the inverter charges symmetrically.** That holds for
  three-phase hybrids such as Sungrow SH-RT. An inverter that can load phases
  unevenly would need its own model.
- **The plan still doesn't know which phase a load is on.** A single-phase
  appliance on the worst phase costs three times its power in aggregate
  headroom. The look-back window catches it after it has been seen. Planning
  around a known single-phase load before it starts is future work. See
  [docs/plan/phase_imbalance_safety.md](plan/phase_imbalance_safety.md).

## Smoothing

A day-ahead run applies one value across the entire horizon, and a single
kettle can move a live reading by a kilowatt. The phase guard does its own
smoothing (see [The plan's limit](#the-plans-limit)). For any other import or
export limit sensor, smooth at the source rather than feeding in a raw
instantaneous value. The integration deliberately doesn't average those
itself, so that a sensor that already smooths isn't smoothed twice. Prefer the
lowest value of the window to its mean:

```yaml
sensor:
  - platform: statistics
    name: Grid import limit smoothed
    entity_id: sensor.grid_import_limit
    state_characteristic: value_min
    max_age:
      minutes: 30
```

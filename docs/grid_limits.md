# Dynamic grid limits

The grid step asks for **Maximum import power** and **Maximum export power**,
and those two numbers go straight to EMHASS as `maximum_power_from_grid` and
`maximum_power_to_grid`. For most plants a constant is the right answer.

It isn't when the usable limit moves:

- **An unbalanced three-phase connection.** There is no single fuse rated at
  the sum of the phases — there are three fuses, one per phase, and each only
  sees its own. The connection delivers its full rating only when all three
  phases are loaded equally.
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

## Three-phase imbalance

EMHASS plans one aggregate grid power. Your fuses constrain each phase
separately. To convert one into the other, assume a deferrable load is added
symmetrically across the three phases — true for a three-phase EV charger,
heat pump or water heater — and ask how much can be added before the worst
phase reaches its fuse:

```
max(p) + X/3 ≤ fuse_W        →     X ≤ 3 · (fuse_W − max(p))
```

The resulting aggregate grid power is the current net total plus that
addition:

```
limit = (p1 + p2 + p3) + 3 · fuse_W − 3 · max(p1, p2, p3)
```

As a template sensor, for a 16 A per-phase service. `3600` is `16 A × 225 V` —
deliberately 225 rather than 230, since fuses limit *current* and the meter
reports *watts*, so a poor power factor means the real current is a little
higher than the watts suggest:

```yaml
template:
  - sensor:
      - name: Grid import limit
        unique_id: grid_import_limit
        unit_of_measurement: W
        device_class: power
        state: >
          {% set fuse = 3 * 3600 %}
          {% set p1 = states('sensor.p1_meter_active_power_phase_1')|float(0) %}
          {% set p2 = states('sensor.p1_meter_active_power_phase_2')|float(0) %}
          {% set p3 = states('sensor.p1_meter_active_power_phase_3')|float(0) %}
          {{ [ (p1 + p2 + p3) + fuse - 3 * [p1, p2, p3]|max, 0 ]|max | round(0) }}
```

Three things about that expression:

- **Do not clamp the individual phase readings at zero.** A phase that is
  exporting genuinely lowers the aggregate you are allowed to import, and
  zeroing it hides that: with `[+2000, −1000, 0]` the unclamped formula gives
  5800 W, which is correct, and clamping gives 6800 W, which would put L1 at
  3933 W on a 3600 W fuse.
- **Clamp the result instead.** It goes negative only when a phase is already
  over its fuse, and zero is the honest answer there. The integration's own
  floor lifts it back to something feasible.
- **It can never exceed the nameplate.** `Σp − 3·max(p) ≤ 0` always, with
  equality exactly when the phases are balanced.

If your meter exposes per-phase **current** as well, prefer it: it is what the
fuse actually responds to, so the power-factor question disappears. Run the
same formula on amps (`fuse = 3 * 16`, phases in A), then multiply the result
by your nominal phase voltage at the end — the setting itself is still watts.

## Smoothing

A day-ahead run applies whatever the sensor reads at that moment to the entire
horizon, and a single kettle can move the reading by a kilowatt. Smooth at the
source rather than feeding in a raw instantaneous value — the integration
deliberately does not average it, so that a sensor which already smooths
itself isn't smoothed twice:

```yaml
sensor:
  - platform: statistics
    name: Grid import limit smoothed
    entity_id: sensor.grid_import_limit
    state_characteristic: mean
    max_age:
      minutes: 30
```

A main fuse is thermal, not a trip threshold: a 16 A gG fuse tolerates a
modest overload for minutes. A brief excursion caused by a forecast error will
not blow anything, which is what makes a smoothed value per run acceptable in
place of a hard real-time limiter. Anything that must be enforced in real time
belongs in a load-balancing device, not in a planner.

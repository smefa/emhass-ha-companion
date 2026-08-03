# On-demand loads

By default a deferrable load wants its configured hours **every single day**.
Some loads don't work like that:

- a hot water tank only needs heating once its temperature has dropped,
- a dishwasher only needs a run after someone has loaded it,
- a pool pump may only need to run after heavy use.

These share one shape: no standing demand, armed by an event. Switch a
load's recurrence to *On demand* and it wants nothing until something arms
it — then it enters the plan exactly like any other deferrable load, and
disarms itself once its hours are met.

*Status: implemented — recurrence, the requested switch, deadlines and
auto-disarm all work today. There is no ready-made blueprint yet, so wire
the requested switch up with a plain automation (examples below). A
`request_run` service with per-request overrides is planned separately —
see [Request-run service (planned)](request_run_service.md).*

## Setup

On the load's device page:

1. Set **Recurrence** (`select.<load>_recurrence`) to **On demand**.
2. Automate something to turn **Requested** (`switch.<load>_requested`) on.

Guiding principle: your automation declares *whether* and *how much*; EMHASS
still decides *when*. The trigger only flips a switch — it never drives the
appliance directly.

| Entity | |
|---|---|
| `select.<load>_recurrence` | *Every day* (default) / *On demand* / *On spare solar* — see [Surplus loads](surplus_loads.md) for the third option |
| `switch.<load>_requested` | On = a run is wanted; the load enters the plan until it's done. Off = no demand, and turning it off **cancels** a pending request |
| `number.<load>_run_within` | Optional deadline, in hours — see below. Only available while recurrence is *On demand* — a surplus load has no run-length target for a deadline to count down against either |

There is no separate "pending" sensor — the requested switch's own state is
what to watch or display.

## Example automations

**Arm from a sensor threshold** — a hot water tank whose temperature has
dropped:

```yaml
triggers:
  - trigger: numeric_state
    entity_id: sensor.tank_temperature
    below: 45
    for: "00:10:00"
actions:
  - action: switch.turn_on
    target:
      entity_id: switch.hot_water_requested
```

The switch's own lifecycle provides the hysteresis: once requested, the
sensor dropping further does nothing until the run completes and the switch
turns itself off again.

**Arm from an event** — a dishwasher door sensor, an `input_boolean`, or a
button:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.dishwasher_door
    to: "off"
actions:
  - action: switch.turn_on
    target:
      entity_id: switch.dishwasher_requested
```

**Cancel** is the same call with `switch.turn_off` — turning the switch off
mid-run clears the request.

## Deadlines: "run within N hours"

Two different kinds of constraint apply to a load, and it's worth keeping
them apart:

- **A time window** (`switch.<load>_restrict_to_a_time_window` and its
  `time.<load>_earliest_start` / `_latest_finish`) is standing config — a
  property of the appliance or the household ("never before 22:00"). It
  applies to every load, whatever its recurrence, and recurs every day.
- **A deadline** (`number.<load>_run_within`) belongs to *this* request. It
  counts from the moment the request was armed, not from "now" on each
  recalculation — a deadline that re-derived itself from the clock would
  slide forward and never arrive. It is only available on-demand loads,
  because a daily load has no request event to anchor it to.

Both may apply at once. If they conflict:

| Situation | Result |
|---|---|
| Deadline falls before the time window opens | Deadline wins, load runs ASAP |
| Deadline has already passed | Scheduled at the earliest opportunity |
| Deadline is further out than the plan horizon | Unconstrained for now; starts to bind as the horizon catches up |

The deadline winning over a quiet-hours window is deliberate: an explicit,
timed request is judged more important than a standing preference — a
request that silently does nothing for hours is worse than one that breaks
a preference audibly.

Progress toward a deadline is tracked per request, not reset at midnight —
so a dishwasher loaded at 23:00 keeps its progress and its deadline across
the day boundary instead of being told it needs to start over.

## Greyed-out settings

Some settings are only meaningful in certain states, and report unavailable
rather than accepting a value that would silently never be read:

| Entity | Unavailable when |
|---|---|
| `switch.<load>_requested` | Recurrence is *Every day* |
| `number.<load>_run_within` | Recurrence is anything but *On demand* (also unavailable on a surplus load) |
| `time.<load>_earliest_start` / `_latest_finish` | The time-window switch is off |
| `number.<load>_lowest_power_while_running` | The load runs at full power only |

## Deferrable numbering stays stable

EMHASS refers to its loads as `P_deferrable0`, `P_deferrable1`, … in its own
charts and logs, with no name attached. The Companion sends **every**
configured load on every run — an idle on-demand load is sent "parked" at
zero hours rather than left out — specifically so that number never shifts
just because a request lapsed or a load got disabled. It still moves if you
add or remove a load, since the assignment is by name order.

The number is shown as the diagnostic `sensor.<load>_emhass_deferrable_number`,
and as an `emhass_deferrable` attribute on `should_run` and
`scheduled_power`, useful if you're cross-referencing EMHASS's own output.

# EMHASS HA Car Companion — plan

A separate HACS integration (`emhass_car_companion`, repo
`smefa/emhass-ha-car-companion`) that turns the *EMHASS COMPANION CAR TEST*
automation into configuration. It owns the car-charging problem — what the car
needs, when it should get it, and how to command the charger — and delegates
the *when* to EMHASS Companion by driving one deferrable load.

It is not a second optimiser. Everything it knows about timing, it learns from
a Companion load's plan.

## What the automation proved, and what it costs

The CAR TEST automation works, and its structure is the right one:

- a **mode** selector chooses a *shape* for the load (surplus vs on-demand,
  splittable vs unbroken, full power vs modulating, deadline vs free),
- the load is **armed** from the cable and the car's state of charge, not from
  runtime,
- the plan's `scheduled_power` is **mirrored onto the charger** as amps and a
  phase count, with hysteresis and a floor,
- and re-planning happens **only when the ask actually changed**.

What it costs is roughly 250 lines of Jinja with `sensor.easee_home_12877_…`,
`70.0` kWh, `690`/`230` V, `4000`/`4400` W, `1400` W and a device id hard-coded
into it. None of that is Easee-specific *thinking*; all of it is
Easee-and-this-car-specific *data*. That split is exactly what the Companion's
profile system already exists to express, so this plugin uses the same system,
with the same rules: a profile is data, never code; the escape hatch is a
`template` or a `script`, not a new keyword.

## Shape

Two profile kinds, chosen independently, because the same car moves between
chargers and the same charger serves several cars:

| Kind | Answers |
|---|---|
| `charger` | How do I command watts? How do I start and stop? What is it doing? |
| `car` | How full is it, how full does it want to be, and is it plugged in? |

Then four code layers, each of which is a paragraph of the automation made
general:

1. **Session model** — plugged / needed / target / armed / delivering / done.
   Owns "how many kWh does this car want, and by when".
2. **Deferrable bridge** — writes the Companion load's settings and reads its
   plan. This is the whole "shape the load to suit the mode" block.
3. **Executor** — turns `scheduled_power` into a charger command, with
   hysteresis, deadband, minimum write interval, the delivery floor and the
   house current limit. This is the "Drive the charger" block.
4. **Frontend** — one card, `custom:emhass-car-card`.

## Profile schema

Same document conventions as the Companion's: `name`, `kind`, `version`,
`brand`, `models`, `requires`, `docs`, `untested`, `detect`, `options` (with
`selector` and `suggest`), `sensors`, `limits`, `actions`. Only the blocks
below are new.

### `kind: charger`

```yaml
name: Easee (Home / Lite / Charge)
kind: charger
version: 1
brand: Easee
requires: Easee (HACS, nordicopen)

control:
  setpoint_unit: a            # a | w
  setpoint_scope: circuit     # charger | circuit — what the limit applies to
  per_phase: true             # can address P1/P2/P3 separately
  phase_control: explicit     # none | explicit | select | threshold
  min_current_a: 6
  max_current_a: 16           # a default; the user's own fuse overrides it
  nominal_voltage_v: 230
  stop_mode: action           # action | zero_setpoint
  resume_needs_setpoint_first: true
  deadband_a: 1
  min_write_interval_s: 0
  poll_lag_s: 0               # how stale the charger's own reporting is
  restore_required: false     # hand control back on shutdown

states:                       # charger vocabulary -> canonical
  disconnected: [disconnected]
  waiting: [awaiting_start, ready_to_charge]
  charging: [charging]
  paused: [paused]
  finished: [completed]
  error: [error]

sensors:
  status: sensor.easee_home_12877_status
  power: sensor.easee_home_12877_power
  session_energy: sensor.easee_home_12877_session_energy

actions:
  set_current: [...]          # receives amps, phases, power_w
  start: [...]
  stop: [...]
  restore: [...]              # optional; hand the charger back to itself
  prepare: [...]              # optional; take manual control once per session
```

Four things in there are not obvious, and each of them exists because a real
charger below would otherwise be driven wrongly:

**`setpoint_scope`.** Easee's dynamic limit is a property of the *circuit*, not
the charge point; Zaptec's available current is a property of the
*installation*. On a site with one charger this is invisible, on a site with
two it is the difference between limiting the car and limiting the house.
Recorded so the card can say which, and so a future two-car setup has somewhere
to hang the distinction rather than needing a schema change.

**`phase_control`.** Four genuinely different hardware models: none (fixed
wiring), `explicit` (write P2/P3 to zero — Easee), `select` (a 1/3/auto option
list — go-e), `threshold` (a number where the *value* is the amperage at which
the charger switches itself over — Zaptec). The executor picks amps and phases
together; how it *says* phases is the profile's business.

**`stop_mode` and `resume_needs_setpoint_first`.** "Stop" is not universally
"set zero amps". Zaptec's own documentation states that resume will not start
charging if the available current is 0 A — encoding stop as zero there wedges
the charger until something else writes a current. So a profile must name its
stop explicitly, and cross-field validation refuses `stop_mode: zero_setpoint`
on a profile that also declares `resume_needs_setpoint_first`.

**`prepare` and `restore`.** OpenEVSE's correct control path is
`set_override` / `clear_override`, not writing the charge-rate number; Ohme has
its own scheduler that must be put in max-charge before anything external can
drive it. Both are "take the wheel" and "give it back", which the Companion's
inverter profiles already model as `self_consume`/`restore`, and which for a
car matters more: a charger left in a manual override forever is a car that
never charges when Home Assistant is down.

Cross-field validation, in the spirit of `_validate_inverter`:

- `start` requires `stop`, and vice versa — anything switchable on must be
  switchable off.
- `phase_control: explicit|select` requires a `set_phases` action or a
  `set_current` that accepts `phases`.
- `restore_required: true` requires a `restore` action.
- `setpoint_unit: w` requires no voltage; `a` requires `nominal_voltage_v`.
- a profile with no `set_current` at all is **monitor-only** and is rejected at
  setup with a clear reason (see Tesla Wall Connector below) rather than
  accepted and silently unable to charge.

### `kind: car`

```yaml
name: Tesla (Teslemetry / Tessie / tesla_custom)
kind: car
version: 1

battery:
  capacity_kwh: 70            # or a template reading the integration's own value
  charge_efficiency: 0.9
  usable_fraction: 1.0

sensors:
  soc: sensor.tesla_model_3_battery_level
  charge_limit: sensor.tesla_model_3_charge_limit
  plugged_in: binary_sensor.tesla_model_3_charger        # optional
  at_home: device_tracker.tesla_model_3                  # optional
  max_charge_current_a: sensor.tesla_model_3_charge_current_max   # optional

staleness:
  max_age_minutes: 120        # older than this, treat SoC as unknown
  fallback_energy_kwh: 20     # what to ask for when the car will not say

actions:
  wake: [...]                 # optional
  set_charge_limit: [...]     # optional
```

`staleness` is the part the automation gets right by luck and this should get
right by design. A sleeping car reports `unavailable`, and `| float(0)` turns
that into "0 % — charge everything". The plugin treats unknown as unknown and
falls back to a flat kWh ask, which is what CAR TEST's `car_data_ok` guard
does, generalised. Never infer a SoC from `unavailable`.

A **`no_car` profile ships and is the default**: no integration, no SoC, just a
kWh-per-session number the user sets. Plenty of people have a charger and no
car API, and the plugin must be fully useful to them.

## Modes

The five CAR TEST modes ship as data, in `modes.yaml` inside the integration,
and a user file at `/config/emhass_car_companion/modes.yaml` may add to or
override them. That is the "flexible if needed" half; the "simple" half is that
nobody has to look at it.

A mode is a mapping from a name to the Companion load settings it implies:

```yaml
solar_direct:
  name: Solar Direct
  icon: mdi:weather-sunny
  recurrence: surplus
  start_asap: true
  startup_penalty: 0.2
  target: energy          # energy | hours
  surplus_headroom_w: 200

solar_cheap:
  name: Solar Cheap
  recurrence: surplus
  start_asap: false
  startup_penalty: 0.2
  target: energy

grid_cheapest:
  name: Grid Cheapest
  recurrence: on_demand
  run_within_h: 24
  full_power_only: false
  unbroken: false
  startup_penalty: 0.1
  target: hours

grid_urgent:
  name: Grid Urgent
  recurrence: on_demand
  run_within: just_fits   # deadline = time needed + one timestep
  full_power_only: true
  unbroken: true
  startup_penalty: 0.1
  target: hours
  bypass_plan: true       # drive the charger directly, do not wait for a solve

off:
  name: Off
```

`bypass_plan` deserves its own line in the docs, because it is the one mode
that does not follow the plan: Grid Urgent charges immediately at whatever the
main fuse has left, and the deadline it writes is there to make the *rest* of
the plan (battery, other loads) solve around a car that is already drawing.

Two settings from CAR TEST that are deliberately **not** in the mode table:
`surplus_priority` and `surplus_headroom_w` at a value tuned against the user's
pool heater. Those are cross-load decisions about somebody else's load, and a
car plugin that silently reorders another load's claim on the sun is
surprising. They become settings, defaulted to the Companion's own defaults.

## The deferrable bridge

The plugin does not reach into the Companion's Python. It uses only the surface
the Companion already treats as public — its entities — resolved through the
entity registry rather than by guessing entity ids, so a rename never empties
the link. This is the same rule the Companion's own cards follow.

**Discovery.** Companion entries are `hass.config_entries.async_entries(
"emhass_companion")`; each load is a subentry, and each load's entities carry
`unique_id == f"{entry_id}_{subentry_id}_{key}"` and a device identified by
`(emhass_companion, f"{entry_id}_{subentry_id}")`. The config flow lists the
loads by name and the user picks the one that is the car; the plugin stores
`(entry_id, subentry_id)` and resolves entity ids from the registry at every
use.

**Keys written**, all of which CAR TEST writes today:

| Purpose | Key | Domain |
|---|---|---|
| Recurrence | `recurrence` | select |
| Armed | `load_requested` | switch |
| Take the sun early | `start_asap` | switch |
| Energy target (surplus) | `energy_needed` | number |
| Hours target (on demand) | `operating_hours` | number |
| Deadline | `run_within` | number |
| Full power only | `semi_continuous` | switch |
| One unbroken block | `single_constant` | switch |
| Cost of starting | `startup_penalty` | number |
| Spare-solar headroom | `surplus_headroom` | number |
| Surplus priority | `surplus_priority` | number |

**Keys read:** `scheduled_power` (and its `schedule` attribute, for the card),
`should_run`, `next_start`, `running`.

**Two settings the automation never writes, and should.** `nominal_power` and
`minimum_power` are derived facts, not preferences: they are
`max_current_a × voltage × phases` and `min_current_a × voltage × 1`. Writing
them keeps EMHASS from planning slots the charger physically cannot deliver —
CAR TEST's `>= 1400` floor discards those slots *after* the solve, which means
the optimiser spent them on a car that got nothing. Writing the floor as
`minimum_power` instead makes the plan itself honest. This is the single
biggest quality win available over the automation.

**Re-planning.** `emhass_companion.run_mpc`, called only when the computed ask
differs from what the load already holds — the `replan` variable, kept — and
additionally rate-limited (a minimum interval, default 5 minutes) so that a
flapping cable or a twitchy SoC sensor cannot turn into a solve every few
seconds. Writes land in the Companion's runtime registry immediately, so a
solve issued straight after a write sees the new values.

**Version coupling.** A separate repo can drift from the Companion's version in
a way the Companion's own bundled cards cannot. The plugin declares a minimum
Companion version and raises a repair issue if the installed one is older,
mirroring the Companion's own `_check_version`.

## The executor

Watts in, a charger command out. Everything here is the "Drive the charger"
block, generalised:

- **Amps and phases together.** `amps = clamp(target_w / (V × phases),
  min_current_a, max_current_a)`, with the phase decision taken first and with
  hysteresis around the changeover (CAR TEST: three-phase above 4000 W, back to
  one below 4400 W) so a plan hovering at the boundary does not flip the
  contactor every timestep.
- **The floor.** Below `min_current_a × V` there is nothing to deliver; the
  command is a stop, not a small number.
- **Deadband and minimum write interval.** Only write when the command actually
  moved. This is not politeness: Wallbox refreshes on a 90-second cycle agreed
  with the vendor to protect their cloud, and a chatty integration on a cloud
  charger is how people get rate-limited.
- **`poll_lag_s`.** With a cloud charger, what the charger reports is up to a
  poll behind what was commanded. The executor compares against *its own last
  command*, not against the charger's reported state, until the lag has passed —
  otherwise it chases its own tail and writes continuously.
- **House current limit (optional).** Per-phase house current sensors and a main
  fuse rating; every command is capped at the headroom. The subtlety worth
  documenting is the one CAR TEST comments: the house sensor already includes
  what the charger is drawing, so the headroom calculation must add back the
  charger's own granted current before subtracting, or the limit walks itself
  down to the floor.
- **Handing back.** On shutdown, on the master switch going off, and when the
  session ends: `restore` if the profile defines it, otherwise `stop`. A
  charger left in an override because Home Assistant was restarted is the
  failure this plugin must not have.

## Entities

The plugin's own device, `EMHASS Car Companion`, one per configured car:

| Entity | |
|---|---|
| `select.<car>_mode` | The modes above |
| `switch.<car>_smart_charging` | Master. Off hands the charger back to itself |
| `number.<car>_target_soc` | Overrides the car's own limit when it has none |
| `number.<car>_energy_needed` | The manual ask, for the `no_car` profile |
| `time.<car>_ready_by` | Deadline; feeds `run_within` |
| `button.<car>_charge_now` | Grid Urgent for this session only |
| `sensor.<car>_state` | Canonical: disconnected / waiting / charging / paused / finished / error |
| `sensor.<car>_energy_needed` | kWh to reach the target, the number everything derives from |
| `sensor.<car>_time_needed` | Hours at nominal power |
| `sensor.<car>_command` | Amps × phases last written, with the reason in an attribute |
| `sensor.<car>_finish_estimate` | From the plan, not from now |
| `sensor.<car>_session_energy` | Delivered this session |
| `binary_sensor.<car>_plugged_in` | |
| `binary_sensor.<car>_charging` | Observed, not commanded |

Config flow steps, which is the full settings inventory:

1. **Charger** — profile, then its options (entity and device pickers,
   pre-filled from `suggest`), then electrical: phases, voltage, min and max
   amps.
2. **Car** — profile, then its options, then battery capacity, charge
   efficiency, and the fallback kWh when the car will not answer.
3. **EMHASS link** — which Companion entry, which deferrable load. If there is
   no suitable load, the step links to the Companion's *Add deferrable load*
   flow rather than creating one behind the user's back; a load created by
   something that later gets uninstalled is a load nobody understands.
4. **House limits** (optional) — main fuse amps, per-phase current sensors,
   whether the charger's own draw is included in them.
5. **Behaviour** — which modes to offer, default mode, minimum SoC floor,
   re-plan interval, surplus priority and headroom.

## The card

`custom:emhass-car-card`, in the same style as the Companion's: inline SVG, no
charting library, no build step, Home Assistant's energy-dashboard CSS
variables so it follows the theme in both modes, entities found through the
registry.

Contents, top to bottom: the mode as a row of chips; state of charge as a bar
with the target marked; energy needed and estimated finish; the plan timeline
for this load, reusing the plan card's rendering; the live command (amps ×
phases, or "paused"); and — the part that makes a car card trustworthy — one
line of **why**: *waiting for cheaper hours until 02:00*, *waiting for sun*,
*capped by main fuse at 10 A*, *below the charger's minimum*, *car is asleep,
assuming 20 kWh*. Everything on that line is already known to the executor; not
showing it is what makes people distrust automatic charging and go back to
plugging in and pressing start.

The bundle is served and registered exactly as `frontend.py` does it, from the
plugin's own path. The SVG helpers get copied rather than shared — no build
step means no package to import — which is a real cost and worth revisiting if
a third companion ever appears.

## Checked against real chargers

Each of these forced something in the schema above.

| Charger | Setpoint | Start / stop | What it forces |
|---|---|---|---|
| **Easee** | `easee.set_circuit_dynamic_limit(device_id, current_p1..p3, time_to_live)` | `easee.action_command` with `override_schedule` then `resume` / `pause` | Options must allow a **device** picker, not just entities. Multi-step actions with ordering. `setpoint_scope: circuit`. Per-phase amps in one call. |
| **Zaptec** | `number` per phase, or `zaptec.limit_current(available_current_phase1..3)` | Charging **switch**; stop/resume **buttons** | Resume does not work at 0 A → `stop_mode: action` is mandatory, and `resume_needs_setpoint_first`. Its *3-to-1-phase switch current* number is `phase_control: threshold` — a phase model that no explicit-phases design would have. |
| **Wallbox** | `number` max charging current (A) | Pause/resume **switch** | 90-second cloud refresh → `poll_lag_s` and `min_write_interval_s` are not optional extras. Entities go unavailable outside the app's schedule → a documented precondition, and the executor must tolerate an unavailable target without spinning. |
| **go-e** | `number` charger max current (A), or `change_charging_power` | `start_charging` / `stop_charging` services | `phase_control: select` (auto/1/3, via `set_phase`). Confirms that both an entity and a service path must be expressible for the same knob. |
| **OpenEVSE** | `number` charge rate 1–48 A, but properly `openevse.set_override(charge_current, auto_release)` | override state / `clear_override` | `prepare` and `restore` actions. `auto_release` is a lease, which is exactly what `restore_required` is for. |
| **Ohme** | Charger has its **own** scheduler (target time and %) | Max-charge / paused | Two planners fighting. `prepare` puts it in max charge; the docs must say plainly that Ohme's own schedule should be cleared. |
| **Tesla Wall Connector** | — | — | Monitor-only. The schema must be able to *refuse*: a profile with no `set_current` is rejected at setup naming the reason, instead of installing cleanly and never charging. |
| **Generic (script)** | your script | your scripts | Ships, like `generic_script.yaml` does for inverters. Receives `power_w`, `amps`, `phases`, `state`. |
| **Generic (entity)** | a number you pick, in A or W | a switch you pick | Covers the long tail with no YAML at all. `setpoint_unit` is the user's answer, not the profile's. |

Two findings from that table are worth restating because they are load-bearing:
a charger's stop is **not** portable as "zero amps", and a cloud charger's
reported state is **not** a valid feedback signal on the timescale the executor
writes at.

## Milestones

- **M0 — skeleton.** Repo, HACS manifest, config entry, profile loader and
  schema lifted from the Companion (it is already generic; the kinds differ).
- **M1 — one charger, one car, no modes.** Easee + Tesla, hard link to a
  deferrable load, executor with floor and hysteresis, follow the plan only.
  Parity with CAR TEST's *Grid Cheapest*.
- **M2 — modes.** The mode table, the bridge writing every setting, `replan`
  throttling, `nominal_power`/`minimum_power` derivation. Full CAR TEST parity,
  and CAR TEST is retired.
- **M3 — breadth.** Zaptec, Wallbox, go-e, OpenEVSE, generic-entity and
  generic-script profiles; `no_car`; the monitor-only rejection path. Every
  profile that has not been run against real hardware ships `untested: true`,
  as the inverter profiles do.
- **M4 — the card.** Including the *why* line.
- **M5 — house limits.** Per-phase capping with the add-back rule, and its own
  tests, because it is the one part that can trip a real fuse.

## Open questions

1. **Should completion be energy-based in the Companion instead?** The plugin
   re-arms the load every pass because the Companion disarms on *runtime*,
   which over-states delivery whenever the charger modulates. That is exactly
   what the deferred `request_run` service plan addresses. If that lands, this
   plugin gets simpler and should use it; until then, re-arming is correct and
   cheap.
2. **Two cars, one charger.** The schema has room (`setpoint_scope`), the
   executor does not. Deliberately out of scope for M0–M5; the thing to avoid
   now is a design that forecloses it — hence one config entry per car rather
   than per charger.
3. **Should the plugin create the deferrable load itself?** Driving another
   integration's subentry flow programmatically is possible but is a two-step
   form flow, and a load created invisibly is a load nobody can reason about.
   Deep-linking to the Companion's own flow is the M0 answer; a small public
   `emhass_companion.create_load` service is the better long-term one, and
   belongs in the Companion, not here.

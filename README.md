# EMHASS Companion

A Home Assistant integration that makes [EMHASS](https://github.com/davidusb-geek/emhass) usable without writing YAML.

EMHASS is an excellent energy optimiser with a difficult front door. Using it today means hand-building the same pile everyone else builds: `input_number` helpers in package files, `rest_command` entries with sprawling Jinja2 payloads, and a fleet of automations to schedule runs and act on results. The most common complaint about it is simply that there are too many ways to configure it and no indication of which apply to your house.

This integration answers that. Install the EMHASS add-on, install this, answer a short config flow, and get a working optimised home.

> **Status: early.** Plan retrieval, scheduling, deferrable loads and battery control are implemented. The dashboard cards are not. Control ships **off** — see [Handing over control](#handing-over-control). See [Roadmap](#roadmap).

---

## How it works

Home Assistant owns all state. EMHASS is treated as a **stateless calculator**.

```
                 Home Assistant                        EMHASS
  ┌────────────────────────────────────────┐        ┌──────────────────┐
  │ prices  ← your price integration       │        │                  │
  │ solar   ← your forecast integration    │──────▶ │   optimiser      │
  │ load    ← your power sensor            │ inputs │                  │
  │ battery ← your inverter integration    │ +      │                  │
  │                                        │settings│                  │
  │ plan sensors, deferrable schedules     │◀────── │  /api/v1/plan    │
  └────────────────────────────────────────┘  plan  └──────────────────┘
```

Three consequences worth knowing:

**Your existing integrations are the data sources.** This integration ships no API clients. Prices come from your Nord Pool / ENTSO-E / Tibber integration, solar from Solcast or Forecast.Solar, hardware from whatever inverter integration you already run. Reading Solcast's cached forecast attribute costs **zero** API calls, so re-optimising every few minutes never touches your daily quota.

**Configuration cannot drift.** Every optimisation request carries the full set of settings as runtime parameters. EMHASS's own `config.json` is written once at setup and never consulted for those values afterwards. There is no "sync" step to forget, because there are not two sources of truth. (A consequence: EMHASS's own web UI will show stale values. The *Last request to EMHASS* diagnostic sensor is the authoritative record.)

**It ships unable to touch your hardware.** `switch.emhass_control_enabled` starts **off**. You almost certainly arrive with working automations; the integration computes and records what it *would* do so you can compare its judgement against your existing setup before handing anything over.

## Requirements

- Home Assistant 2026.7 or newer
- **EMHASS 0.17.9 or newer** — this integration reads the optimisation plan through the JSON API introduced in that release, and does not use EMHASS's `publish-data` mechanism at all

Nothing else is required. No battery, no solar, no dynamic tariff and no supported inverter are each valid configurations.

## Installation

1. Install and start EMHASS (the add-on, or a container).
2. Add this repository to HACS as a custom repository (category: Integration), then install **EMHASS Companion**.
3. Restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → EMHASS Companion**.

The add-on's address is filled in automatically if it is running.

## Setup

The flow asks a few questions at a time, and **only ever offers data sources you actually have installed**.

| Step | What it asks |
|---|---|
| Connect | Where EMHASS is |
| Electricity prices | Which price source, and its settings |
| Buy and sell prices | What your supplier and grid operator add on top of spot |
| Solar forecast | Which forecast source — or *No solar* |
| House consumption | Which sensor, and how to forecast from it |
| Battery | Capacity and limits — or leave it off |
| Grid and schedule | Connection limits, time resolution, how often to recalculate |

### Buy and sell prices

Price sources return **raw spot prices**. Composing your real prices happens separately, because tariff structures differ enormously between markets and change from year to year. Three modes, chosen independently for import and export:

- **Linear** — `spot × multiplier + adder`. Use the multiplier for VAT and the adder for grid fees, energy tax and supplier markup.
- **Already includes costs** — some price integrations can bake fees in themselves. If yours does, use this and do not add them twice.
- **Template** — a Jinja template with `spot` and `time` available, for tiered or time-of-day structures.

> Tax and subsidy schemes change. Nothing here is kept current for you — check your own numbers each year.

## Scheduling

Two cadences:

- **Model-predictive** — re-optimises from the current state every 15 minutes by default.
- **Day-ahead** — a full plan across the whole horizon.

The day-ahead run is **event-driven, not a fixed clock time**. It fires when the price series actually gains a new day. Markets publish at different hours and publication slips; a fixed time means quietly optimising against yesterday's prices whenever it does. A configurable fallback time exists as a backstop.

## Deferrable loads

A deferrable load is anything whose *timing* the optimiser may choose: a dishwasher, a car charger, a pool pump. Tell it the load needs three hours before morning and it decides which three.

Add them under **Settings → Devices & Services → EMHASS Companion → Add deferrable load**. Each becomes its own device, with its own entities, added and removed independently.

Setup asks only for things that rarely change — power, hours, whether it runs at full power or can be modulated, whether it may be interrupted. Everything you might change day to day becomes a control on the load's own page:

| Entity | |
|---|---|
| `binary_sensor.<load>_should_run` | **The one to automate against.** Unknown rather than off when there is no usable plan |
| `sensor.<load>_scheduled_power` | Planned power now, with the full schedule in its `schedule` attribute |
| `sensor.<load>_next_start` | When it is next due to start |
| `sensor.<load>_runtime_today` | How long it has run today |
| `switch.<load>_enabled` | Include this load in the optimisation at all |
| `switch.<load>_restrict_to_a_time_window` | Whether the window below applies |
| `time.<load>_earliest_start` / `_latest_finish` | The window. May cross midnight |
| `number.<load>_power_when_running` / `_hours_needed_per_day` | |
| `select.<load>_mode` | Follow the plan / Force on / Force off |

Two distinctions worth knowing, because conflating either causes confusion:

**Mode overrides the signal, not the plan.** Force off makes `should_run` off, but EMHASS is still asked to schedule the load. To leave a load out of planning entirely, turn off its **Enabled** switch. Keeping these separate means pausing a load never silently changes the problem being solved.

**A power sensor is optional but worth adding.** With one, the integration can tell EMHASS the load is already running — so it is not charged a start-up cost twice, and is not asked to repeat work it has already done today. Runtime is counted from that sensor and reset at local midnight, because EMHASS has no notion of a day boundary for it.

Automating against a load looks like:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.dishwasher_should_run
    to: "on"
actions:
  - action: switch.turn_on
    target:
      entity_id: switch.dishwasher
```

## Entities

| Entity | |
|---|---|
| `sensor.*_planned_solar_production` | Planned PV, with the full series in its `forecast` attribute |
| `sensor.*_planned_house_consumption` | Planned load |
| `sensor.*_planned_grid_power` | Positive = import, negative = export |
| `sensor.*_planned_battery_power` | Positive = discharge, negative = charge |
| `sensor.*_planned_battery_level` | Percent |
| `sensor.*_import_price` / `*_export_price` | Composed prices |
| `sensor.*_optimisation_status` | Result of the last solve |
| `sensor.*_planned_cost` | Cost of the current plan |
| `binary_sensor.*_plan_out_of_date` | The plan is too old to act on |
| `switch.*_control_enabled` | Master gate on acting. Ships **off** |
| `select.*_mode` | Anything but *Automatic* suspends control |
| `button.*_recalculate_now` / `*_rebuild_day_ahead_plan` | Run on demand |

Battery entities only exist if you configured a battery.

## Handing over control

The integration can command your battery and switch your deferrable loads. It ships unable to do either, and that is deliberate: you almost certainly arrive with working automations, and handing control to a newly configured optimiser before watching it make sensible decisions is how a battery ends up charging at the day's peak price.

While `switch.emhass_control_enabled` is off, the executor still runs on every cycle and records exactly what it *would* have done — the action, the power, the reasoning, and the precise service calls — on `sensor.emhass_battery_action`. That is the migration path:

1. Configure everything, leave control **off**.
2. Watch `sensor.emhass_battery_action` alongside your existing automations for a few days. Its `steps` attribute is the literal list of calls it would have made.
3. When you agree with its judgement, turn the switch on and retire the automations.

**Battery control** is configured under **Configure → Battery control**, by choosing an inverter profile. Two ship: one driving a *mode select plus a power number* entity (common for Modbus integrations), and one calling *a script of yours per action*, for anything needing a sequence of writes. This integration never talks to hardware directly — it only ever calls entities and services your own inverter integration already provides. Leave the profile unset and the battery is never touched.

**Deferrable loads** are switched only if you set a control entity on the load. Leave it empty and the load stays advisory: `binary_sensor.<load>_should_run` still tracks the plan, and acting on it remains your automation's job.

Three safety behaviours, each covered by a test:

- **Staleness watchdog.** If a plan stops being refreshed for more than twice the recalculation interval, the executor stops following it and falls back to self-consumption. A stale plan describes a world that no longer exists.
- **Deadband.** An unchanged command is not reissued, so a slow bus is not hammered every cycle. The deadband never suppresses a *change of action* — a switch from charging to discharging is always sent.
- **Manual override.** `select.emhass_mode` set to anything but *Automatic* suspends the optimiser entirely rather than competing with it.

A failed service call is recorded and retried on the next cycle rather than being marked as applied — otherwise the deadband would suppress the retry.

## Data sources

Every source is a **YAML profile** — data, not code. Adding support for another integration means writing one file, and you can drop it in yourself without waiting for a release.

Built in today:

| Kind | Profiles |
|---|---|
| Price | Nord Pool (core), Nord Pool (HACS), Fixed tariff |
| Solar | Solcast, EMHASS built-in (Open-Meteo), No solar |
| Load | House load sensor, Forecast from an entity attribute |
| Inverter | Mode select plus power number, Scripts |

See **[docs/profiles.md](docs/profiles.md)** to write your own or contribute one.

## Troubleshooting

**A source returns nothing, or the wrong values.** Call the `emhass_companion.test_profile` action. It resolves the profile and returns exactly what came back:

```yaml
action: emhass_companion.test_profile
data:
  profile: price/nordpool_custom
response_variable: result
```

**A plan looks wrong.** Enable the *Last request to EMHASS* sensor (disabled by default) — its `payload` attribute is precisely what EMHASS was asked to solve, and its `warnings` attribute flags problems such as a forecast that does not reach the end of the horizon. Download diagnostics from the integration page for the same thing plus profile state.

**Forecast coverage warnings.** EMHASS forward-fills a forecast that stops short, so the plan looks healthy while being built on a price that flat-lined. These warnings are how you find out.

## Roadmap

| Phase | Status |
|---|---|
| 1 — Plan retrieval, profiles, scheduling, sensors | **done** |
| 2 — Deferrable loads as config subentries | **done** |
| 3 — Inverter profiles, executor, dry-run gate, watchdog | **done** |
| 4 — Custom dashboard cards | not started |
| 5 — More source profiles, translations, docs | partial |

Control is implemented but ships off; see [Handing over control](#handing-over-control).

## Development

```bash
pip install -r requirements-dev.txt
pytest
ruff check . && ruff format --check .
```

Tests are split in two. `tests/` needs no running Home Assistant and runs on any platform. `tests/integration/` needs one, via `pytest-homeassistant-custom-component`, which does not load on Windows — run those on Linux or in a container.

Run the bare `pytest` before pushing, not the two directories separately: collection problems only appear when both are collected together.

Every built-in profile must ship a fixture in `tests/fixtures/`: a recorded blob from a real installation plus the series it should resolve to. That is what lets a profile for an integration none of us has installed still be verified, and what makes reviewing a contributed profile mechanical.

## Licence

MIT. Not affiliated with the EMHASS project.

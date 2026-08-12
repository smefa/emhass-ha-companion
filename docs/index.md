# EMHASS Companion

A Home Assistant integration that makes [EMHASS](https://github.com/davidusb-geek/emhass) usable without writing YAML.

EMHASS is an excellent energy optimiser with a difficult front door. Using it today means hand-building the same pile everyone else builds: `input_number` helpers in package files, `rest_command` entries with sprawling Jinja2 payloads, and a fleet of automations to schedule runs and act on results.

This integration answers that. Install the EMHASS add-on, install this, answer a short config flow, and get a working optimised home.

!!! tip "New here?"
    The **[User Guide](guide/index.md)** is a plain, screen-by-screen
    walkthrough — what each button and field does, nothing more. The rest of
    this site is the **technical reference**: the reasoning behind each
    setting, and how the integration works internally.

!!! note "Status: early"
    Plan retrieval, scheduling, deferrable loads, dashboard cards and battery control are implemented. Control ships **off** — see [Handing over control](handing_over_control.md).

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

**Your existing integrations are the data sources.** This integration ships no API clients. Prices come from your Nord Pool / ENTSO-E / Tibber integration, solar from Solcast, hardware from whatever inverter integration you already run. Reading Solcast's cached forecast attribute costs **zero** API calls, so re-optimising every few minutes never touches your daily quota. See [Data sources](data_sources.md).

**Configuration cannot drift.** Every optimisation request carries the full set of settings as runtime parameters. EMHASS's own `config.json` is written once at setup and never consulted for those values afterwards. (A consequence: EMHASS's own web UI will show stale values. The *Last request to EMHASS* diagnostic sensor is the authoritative record.)

**It ships unable to touch your hardware.** `switch.emhass_control_enabled` starts **off**. You almost certainly arrive with working automations; the integration computes and records what it *would* do so you can compare its judgement against your existing setup before handing anything over. See [Handing over control](handing_over_control.md).

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

## Where to go next

- **[Setup](setup.md)** — the config flow, composing tariffs from raw spot prices, and how the schedule is chosen.
- **[Deferrable loads](deferrable_loads.md)** and **[Thermal loads](thermal_loads.md)** — the entities each load type creates, on-demand loads, and calibration advice. **[Surplus loads](surplus_loads.md)** covers loads that only ever run on spare solar.
- **[Dashboard cards](dashboard_cards.md)** — the plan overview and per-load cards that register automatically.
- **[Entities](entities.md)** — the full list of sensors, switches and controls.
- **[Cost and savings](savings.md)** — what today actually cost, what it saved against a house with no solar and no battery, and what the next 24 hours look like.
- **[Handing over control](handing_over_control.md)** — the migration path and safety behaviours.
- **[Inverter control](inverter_control.md)** — configuring battery control: profiles available today, and the scripts fallback for any inverter. Fronius GEN24 owners want the [scripts recipe](fronius_gen24_scripts.md).
- **[Data sources](data_sources.md)** and **[Writing a profile](profiles.md)** — what ships built in, and how to add your own.
- **[Troubleshooting](troubleshooting.md)** — isolating a data source, diagnosing a wrong-looking plan, resolving an infeasible optimisation.

## Contributing

Development setup, tests and translations are in [CONTRIBUTING.md](https://github.com/smefa/emhass-ha-companion/blob/main/CONTRIBUTING.md).

## Licence

Copyright (C) 2026 Tomas Smedberg.

Licensed under the GNU Affero General Public License, version 3 or later ([LICENSE](https://github.com/smefa/emhass-ha-companion/blob/main/LICENSE)). It comes with no warranty. If you run a modified version as a network service, the AGPL requires you to offer that service's users the modified source.

Not affiliated with the EMHASS project, which is licensed separately. This integration talks to EMHASS over HTTP and includes none of its code.

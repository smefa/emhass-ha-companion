# EMHASS Companion

A Home Assistant integration that makes [EMHASS](https://github.com/davidusb-geek/emhass) usable without writing YAML.

EMHASS automatically analyzes your solar forecasts, household usage patterns, and dynamic grid pricing to schedule your batteries and heavy appliances for maximum financial savings.

Join my [Discord](https://discord.gg/wvavNscm8) if you have questions or want to give input.


📖 **[Full documentation](https://smefa.github.io/emhass-ha-companion/)**

EMHASS is an excellent energy optimiser with a difficult front door. Using it today means hand-building the same pile everyone else builds: `input_number` helpers in package files, `rest_command` entries with sprawling Jinja2 payloads, and a fleet of automations to schedule runs and act on results.

This integration answers that. Install the EMHASS add-on, install this, answer a short config flow, and get a working optimised home.


> The docs site has a plain, screen-by-screen [User Guide](https://smefa.github.io/emhass-ha-companion/guide/) alongside the technical reference below.

---

## How it works

Home Assistant owns all state. EMHASS is treated as a **stateless calculator**.

```
                 Home Assistant                        EMHASS
  ┌──────────────────────────────────────────┐         ┌──────────────────┐
  │ prices  ← your price integration         │         │                  │
  │ solar   ← your forecast integration      │──────▶  │   optimiser      │
  │ load    ← your power sensor              │ inputs  │                  │
  │ battery ← your inverter integration      │ +       │                  │
  │                                          │settings │                  │
  │ plan sensors, deferrable schedules       │◀──────  │  /api/v1/plan    │
  └──────────────────────────────────────────┘  plan   └──────────────────┘
```

Three consequences worth knowing:

**Your existing integrations are the data sources.** This integration ships no API clients. Prices come from existing price integrations, solar from Solcast, hardware from whatever inverter integration you already run. See [Data sources](docs/data_sources.md).

**Configuration cannot drift.** Every optimisation request carries the full set of settings as runtime parameters. EMHASS's own `config.json` is written once at setup and never consulted for those values afterwards. (A consequence: EMHASS's own web UI will show stale values.)

**It ships unable to touch your hardware.** `switch.emhass_control_enabled` starts **off**. The integration computes and records what it *would* do so you can compare its judgement against your existing setup before handing anything over. See [Handing over control](docs/handing_over_control.md).

## Requirements

- Home Assistant 2026.7 or newer
- **EMHASS 0.18.1 or newer** — this integration reads the optimisation plan through the JSON API.

Nothing else is required. No battery, no solar, no dynamic tariff and no supported inverter are each valid configurations.

## Installation

1. Install and start EMHASS (the add-on, or a container).
2. Add this repository to HACS as a custom repository (category: Integration), then install **EMHASS Companion**.

   <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=smefa&repository=emhass-ha-companion&category=integration" target="_blank">
     <img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.">
   </a>

3. Restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → EMHASS Companion**.

The add-on's address is filled in automatically if it is running.

## Setup

The config flow asks a few questions at a time, and only ever offers data sources you actually have installed: connect to EMHASS, electricity prices, buy/sell composition, solar forecast, house consumption, battery, and how often to recalculate.

See **[docs/setup.md](docs/setup.md)** for the full flow, composing real tariffs from raw spot prices, and how the optimisation time step and schedule are chosen.

## Deferrable and thermal loads

A **deferrable load** is anything whose *timing* the optimiser may choose — a dishwasher, a car charger, a pool pump. Tell it the load needs three hours before morning and it decides which three. A **thermal load** is a variant whose *temperature* is controlled instead — a heat pump, direct electric heating — kept inside a comfort band you set rather than a fixed run time. Each is added under **Settings → Devices & Services → EMHASS Companion**, becomes its own device, and exposes a `binary_sensor.<load>_should_run` to automate against.

See **[docs/deferrable_loads.md](docs/deferrable_loads.md)** and **[docs/thermal_loads.md](docs/thermal_loads.md)** for the entities, on-demand (event-triggered) loads, and calibration advice. There's also **[docs/surplus_loads.md](docs/surplus_loads.md)** for loads that only ever run on spare solar.

## Dashboard cards

Dashboard cards ship with the integration and register themselves automatically — a plan overview and a per-load view, both following your Home Assistant theme. See **[docs/dashboard_cards.md](docs/dashboard_cards.md)**.

## Entities

See **[docs/entities.md](docs/entities.md)** for the full list of sensors, switches and controls the integration creates.


## Data sources

Every source is a **YAML profile** — so adding support for another integration means writing one file, no release required.

| Kind | Profiles |
|---|---|
| Price | Nord Pool (core), Nord Pool (HACS), ENTSO-E, Amber, Tibber, Fixed tariff, Any entity attribute |
| Solar | Solcast, EMHASS built-in (Open-Meteo), Any entity attribute, No solar |
| Load | House load sensor, Any entity attribute, Typical household (no sensor needed) |
| Inverter | Mode select plus power number, Scripts |
| Tariffs | Amber Electric, Göteborg Energi |


See **[docs/data_sources.md](docs/data_sources.md)** for more detail, and **[docs/profiles.md](docs/profiles.md)** to write or contribute your own profile.

## Troubleshooting

See **[docs/troubleshooting.md](docs/troubleshooting.md)** — testing a data source in isolation, diagnosing a wrong-looking plan, and resolving an infeasible optimisation.

## Contributing

Development setup, tests and translations are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

Copyright (C) 2026 Tomas Smedberg.

Licensed under the GNU Affero General Public License, version 3 or later
([LICENSE](LICENSE)). It comes with no warranty. If you run a modified version
as a network service, the AGPL requires you to offer that service's users the
modified source.

Commits published before this change were made available under the MIT licence
and stay available under it. The AGPL applies from this commit onwards.

Not affiliated with the EMHASS project, which is licensed separately. This
integration talks to EMHASS over HTTP and includes none of its code.

# Entities

## Hub

| Entity | |
|---|---|
| `sensor.*_planned_solar_production` | Planned PV, with the full series in its `forecast` attribute |
| `sensor.*_planned_house_consumption` | Planned load |
| `sensor.*_planned_grid_power` | Positive = import, negative = export |
| `sensor.*_planned_battery_power` | Positive = discharge, negative = charge |
| `sensor.*_planned_battery_level` | Percent |
| `sensor.*_end_soc_target` | The battery level the plan aims for at the horizon's end, with the why in its attributes (`reason`, `next_replenishment`, `bridge_energy_wh`, …) — see [End SOC](end_soc_plan.md) |
| `sensor.*_import_price` / `*_export_price` | Composed prices |
| `sensor.*_optimisation_status` | Result of the last solve |
| `sensor.*_planned_cost` | Cost of the current plan |
| `sensor.*_battery_action` | What the executor did or would do, and why — see [Handing over control](handing_over_control.md) |
| `sensor.*_last_request_to_emhass` | The exact payload of the last optimisation request. Diagnostic, disabled by default — see [Troubleshooting](troubleshooting.md) |
| `binary_sensor.*_plan_out_of_date` | The plan is too old to act on |
| `switch.*_control_enabled` | Master gate on acting. Ships **off** |
| `select.*_mode` | Anything but *Automatic* suspends control |
| `button.*_recalculate_now` / `*_rebuild_day_ahead_plan` | Run on demand |
| `button.*_train_load_forecaster` | Only present if your load forecast profile trains EMHASS's own model (see [Data sources](data_sources.md)) |

Battery entities only exist if you configured a battery. Solar-surplus hub
entities (`sensor.*_solar_surplus*`) only exist once a surplus load does — see
[Surplus loads](surplus_loads.md).

Two services mirror the recalculate buttons for use in scripts and
automations: `emhass_companion.run_dayahead` and `emhass_companion.run_mpc`.

## Deferrable and thermal loads

See [deferrable_loads.md](deferrable_loads.md) and
[thermal_loads.md](thermal_loads.md) for the per-load entities.

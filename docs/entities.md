# Entities

## Hub

| Entity | |
|---|---|
| `sensor.*_planned_solar_production` | Planned PV, with the full series in its `forecast` attribute |
| `sensor.*_planned_house_consumption` | Planned load |
| `sensor.*_planned_grid_power` | Positive = import, negative = export |
| `sensor.*_planned_battery_power` | Positive = discharge, negative = charge |
| `sensor.*_planned_battery_level` | Percent |
| `sensor.*_end_soc_target` | The battery level the plan aims for at the horizon's end, with the why in its attributes (`reason`, `cover_energy_wh`, `cover_until`, `sale_blocked`, …) — see [End SOC](end_soc_plan.md) |
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

## EMHASS entity names

If you are replacing a bare EMHASS install, your dashboards, template sensors
and automations are written against the entity ids EMHASS publishes under, and
nothing in Home Assistant will tell you those ids stopped being written.

**Settings → Devices & services → EMHASS Companion → Configure → EMHASS entity
names** switches nine hub sensors over to those ids:

| Companion | EMHASS |
|---|---|
| `sensor.*_planned_solar_production` | `sensor.p_pv_forecast` |
| `sensor.*_planned_house_consumption` | `sensor.p_load_forecast` |
| `sensor.*_planned_grid_power` | `sensor.p_grid_forecast` |
| `sensor.*_planned_battery_power` | `sensor.p_batt_forecast` |
| `sensor.*_planned_battery_level` | `sensor.soc_batt_forecast` |
| `sensor.*_optimisation_status` | `sensor.optim_status` |
| `sensor.*_planned_cost` | `sensor.total_cost_fun_value` |
| `sensor.*_import_price` | `sensor.unit_load_cost` |
| `sensor.*_export_price` | `sensor.unit_prod_price` |

The setup wizard offers this too, but only if EMHASS entities already exist —
otherwise it never asks, and the Companion's own names are used.

Matching the id is not enough on its own for anything that reads the forecast
series, so with the option on each of these sensors also carries the series in
EMHASS's shape — `forecasts` (or `battery_scheduled_power`,
`battery_scheduled_soc`, `unit_load_cost_forecasts`,
`unit_prod_price_forecasts`), a list of `{"date": …, "<object_id>": "<value>"}`
with values as strings, starting at the current timestep. The Companion's own
`forecast` attribute stays alongside it, because the dashboard cards read that.

Turning the option back off restores the ids the sensors had before, so it is
safe to try.

Two limits worth knowing:

- **Per-load sensors are not renamed.** EMHASS numbers those `P_deferrable0`,
  `P_deferrable1` and so on by load order, but a Home Assistant entity id never
  moves once assigned — adding or renaming a load would leave the number
  pointing at a different appliance, silently and permanently.
- **If EMHASS is still publishing, the ids are already taken.** Home Assistant
  will not move a sensor onto an occupied id, so those keep their Companion
  names and a repair notice names them. Turn off publishing in EMHASS (clear
  `publish_prefix`, or stop calling its publish-data action), delete the
  leftover entities, then reload EMHASS Companion.

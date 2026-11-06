# Actions

For scripts, automations and troubleshooting. You never need any of these for
the integration to work: it already runs itself, and everything day-to-day has
a control on the [hub device](hub_controls.md) or a load's device.

Call an action from **Developer tools → Actions**, a script or an automation.
Actions marked *administrators only* are refused for a non-admin user; a call
with no user behind it (an automation, a script) is always allowed.

| Action | What it does | Fields | Who |
| --- | --- | --- | --- |
| [`emhass_companion.run_mpc`](#run_mpc) | Re-plans from the current state | none | anyone |
| [`emhass_companion.run_dayahead`](#run_dayahead) | Rebuilds the plan across the whole horizon | none | anyone |
| [`emhass_companion.train_load_forecaster`](#train_load_forecaster) | Retrains the machine-learning load forecaster | none | anyone |
| [`emhass_companion.set_hot_water_plan`](#set_hot_water_plan) | Replaces a hot-water tank's whole plan | `device_id`, `periods` | anyone |
| [`emhass_companion.export_settings`](#export_settings) | Saves Companion's static settings into EMHASS | none | administrators |
| [`emhass_companion.test_profile`](#test_profile) | Shows what a data source profile returns | `profile`, `options`, `limit` | administrators |
| [`emhass_companion.typical_load_forecast`](#typical_load_forecast) | Builds the *Typical household* forecast | `day`, `average_w` | anyone |

## run_mpc

The **Recalculate now** button, as an action. Runs a model-predictive
optimisation from the current state instead of waiting for the next scheduled
one.

```yaml
action: emhass_companion.run_mpc
```

## run_dayahead

A full optimisation across the whole planning horizon. There is no button for
it, because it already runs by itself:

- at startup;
- when the price source gains roughly a new day of prices (Nord Pool
  publishes the next day around 13:00) — never more than once an hour;
- at the **Day-ahead fallback time** (13:30 by default), as a backstop, unless
  one just ran.

Call it yourself after a change you want reflected across the whole horizon
at once, or to retry after a failed run without waiting.

```yaml
action: emhass_companion.run_dayahead
```

## train_load_forecaster

Trains EMHASS's machine-learning load forecaster on your load sensor's recent
history. There is no button for it either. Only needed if your load forecast
method is *Machine-learning forecaster*; otherwise the action fails with an
explanation.

You rarely need it: the model trains itself once the sensor has enough history
(nine days), whenever the time step, load sensor or lag count changes, and
**every week** — a trained model does not keep learning from new data, so the
weekly retraining is what keeps it following the seasons. Call it to retrain
immediately, for example after a big change in how your household uses power.

```yaml
action: emhass_companion.train_load_forecaster
```

Training can take a while on a small host. If it fails, runs carry on with the
sensor's recent readings as the forecast until a later attempt succeeds.

## set_hot_water_plan

Replaces a hot-water tank's whole plan in one step. Fields, example and rules
are on [Thermal loads](thermal_loads.md#water-tank-with-an-immersion-heater).

## export_settings

*Administrators only.* Saves Companion's static settings into EMHASS's own
saved configuration, so EMHASS's web UI and manual runs there look like
Companion's. Companion's own runs never depend on it. It overwrites some EMHASS
settings, so read [Write settings to
EMHASS](hub_controls.md#write-settings-to-emhass) before using it.

```yaml
action: emhass_companion.export_settings
```

## test_profile

*Administrators only.* Resolves a data source profile and returns what it
produced, for when a source returns nothing or the wrong values. The response
includes the profile's resolved EMHASS settings, which can contain a forecast
API key, which is why it is restricted.

```yaml
action: emhass_companion.test_profile
data:
  profile: price/nordpool_custom
  options:
    entity: sensor.nordpool_kwh_se3_sek_3_10_025
  limit: 10   # how many points to return, 1–500
response_variable: result
```

See [Troubleshooting](../troubleshooting.md)
and [Writing a profile](../profiles.md).

## typical_load_forecast

Used internally by the *Typical household* load profile, which asks it for a
day's typical shape scaled to your average power. You don't need to call it;
it is listed because it shows up in **Developer tools → Actions**.

# Effective EMHASS settings

Optimisation requests use `effective_settings.resolve_effective_settings` for
static settings, followed by action-specific inputs in `payload.build_payload`.
The persistent projection is an **allowlisted static baseline**, not a copy of
an optimisation request. It is exported only by the manual action described
below; setup and reload never write it. ML fitting and prediction invoked by
day-ahead/MPC share the resolver and do not depend on EMHASS's saved settings.
Explicit supported profile values retain their existing precedence.

In short:

- **API only.** Companion talks to EMHASS through its HTTP API (`/action/*`,
  `/get-config`, `/set-config`). It never reads or writes backend files, and
  profile YAML is Companion input, not a backend configuration source.
- **Requests carry the static settings.** A run needs no earlier export or
  sync. EMHASS still needs the saved baseline it requires before any action,
  its credentials and storage paths, and compatible trained model files.
- **Export is manual and limited.** **Write settings to EMHASS** writes the
  allowlisted static baseline and nothing live.
- **Limits of reproduction.** The exported baseline and Companion's settings
  cannot recreate a past run. That needs its original runtime payload and
  compatible backend and model state.

## Ownership and destinations

The executable field inventory is `effective_settings.SETTINGS`: each entry
records its public runtime name, persistent-config destination (or no
destination), minimum version, default/clearing value and profile policy.
Public runtime and persistent names currently coincide; internal legacy names
such as `freq`, `var_load`, `num_batteries` and `Pc_max` are not API payload keys.
The resolver never derives the persistent baseline from live action data.

| Owner / family | Fields | Destination and consuming path |
| --- | --- | --- |
| Companion static: timing / objective | `optimization_time_step`, `method_ts_round`, `costfun` | Both projections; runtime merge precedes forecast-date construction, retrieval and optimisation. |
| Companion static: battery | `set_use_battery`, `number_of_batteries`, capacity, charge/discharge powers and efficiencies, SOC min/max/target, charge/discharge restrictions, first priority, dynamic limits, throughput weights, SOC deficit/surplus costs and thresholds, stress cost/segments, charge derating, final reward | Both; `plant_conf` and `optim_conf` consumed by `Optimization`. Disabled battery still sends the static baseline. |
| Companion static: inverter / PV | Hybrid flag, AC input/output powers and efficiencies, both inverter power curves, inverter stress cost/segments, `compute_curtailment`, `set_use_pv`, PV module/inverter models, tilt/azimuth, modules/string, strings/inverter | Both; `Forecast` and `Optimization`. A supplied PV series enables PV unless the profile explicitly disables it. Native PV profile supplies its own model/geometry. Unselected geometry is not fabricated. |
| Companion static: physical grid | `maximum_power_from_grid`, `maximum_power_to_grid` | Persistent projection uses configured scalars only; runtime request replaces them with sensor/phase/window-constrained limits. |
| Companion static: solver | `lp_solver_timeout=45`, `lp_solver_mip_rel_gap=0.01` (both user-adjustable under Advanced settings; the choice outranks a profile), `num_threads=0` | Both; optimisation solver invocation. Solver **selection** (`lp_solver`, not in associations) and installed solver packages remain backend-owned. |
| Companion static: native history | `historic_days_to_retrieve=2` (at least 9 for ML), `load_negative=false`, `set_zero_min=true`, `sensor_replace_zero=[]`, `sensor_linear_interp=[]`, profile-selected load/PV sensor | Both; `RetrieveHass` preprocessing. A profile may supply sensor-specific preprocessing; credentials and transport remain backend-owned. |
| Companion static: native forecasts / ML | Load/weather methods, PV quantile bias, Open-Meteo cache age, adjusted-PV flag and model settings, battery-identification flag/trust/model age, `var_model`, `num_lags`; explicit profile model/training options | Both; `Forecast`/ML configuration. The load sensor is the single source of `var_model` (a differing profile value is ignored with a warning, because prediction indexes the retrieved history by it). Default lags cover the whole-day-rounded day-ahead horizon. ML defaults are `model_type=long_train_data`, `sklearn_model=KNeighborsRegressor`, `split_date_delta=48h`, `perform_backtest=false`, `mlforecaster_weather_features=[]`. |
| Companion static: native tariffs | Load/production forecast methods, HP periods/costs, constant sale price, tariff timezone, `set_total_pv_sell` | Both; native tariff forecasting and optimisation. Timestamped price inputs take precedence in the backend. |
| Companion static: thermal defaults | `heat_topology={}`, `cop_solver=static`, `cop_solver_tolerance=0.5`, `cop_hx_approach=5` | Both where supported. Empty topology prevents post-merge compilation from replacing Companion's registry. COP settings affect native thermal forecasting. |
| Runtime only: topology | `shared_thermal_tanks=[]`, `is_electric_load=[true, ...]`, `def_load_config=[{}, ...]`, `deferrable_load_groups`, `cost_forecast_per_deferrable_load=[null, ...]` | Action only. Independent inherited thermal/tank/group/cost models are cleared. Thermal configurations are rebuilt at each load's registry index; null **entries** disable optional costs, not null field-level overrides. |
| Runtime only: load schedule and progress | Number of loads, nominal/minimum powers, operating hours/timesteps, start/end timesteps, semi-continuous/single-constant flags, startup penalties/limits, minimum on/off times, current state/power/completed/on/off timesteps | Action only. Even nominal arrays can contain parked or pinned live values, so none are exported as static load definitions. |
| Runtime only: current inputs | `soc_init`, `soc_final`, five timestamped forecast mappings, MPC `alpha`/`beta`, `prediction_horizon`, day-ahead `delta_forecast_daily` | Action only. Forecast mappings retain explicit timestamps; backend resampling uses the same requested rounding. |
| Runtime only: grid / demand / lockout | Dynamic import/export limits, `capacity_cost_per_kw`, `current_period_peak`, charge windows, charge interval/history, time-varying discharge weights | Action only. Built by the existing safety, tariff and lockout code; baseline export must never capture these temporary values. |
| Backend only | HA URL/token, location/timezone, provider/database credentials, transport/database connection options, data/model/storage paths, logger/process settings, installed solvers, publication lifecycle/identifiers, valid bootstrap config and saved trained models | Neither resolver projection. These remain prerequisites and may still affect retrieval, publication or native forecasting. The resolver is not a claim of complete backend statelessness. |

## ML settings and saved-model compatibility

The existing training action sends only the shared ML/history/preprocessing
projection, including timestep and rounding, plus the selected load sensor,
`var_model`, lag count, model identity, estimator and training options. Native
ML optimisation prediction receives the same values. No standalone prediction
or tuning workflow exists in Companion. Training is automatic and also
available as the `emhass_companion.train_load_forecaster` action; there is no
button.
History retrieval is explicitly at least nine days for ML, matching the
backend's training minimum, and longer profile-selected history is preserved.

Companion persists the complete effective fit-settings snapshot only after a
successful fit. Compatibility excludes request rounding, weather-provider
selection and cache age: rounding affects the request grid, and ML covariates
come from Open-Meteo independently of the PV weather-provider choice. These
values are still sent on requests. A changed sensor, timestep, lags, model/estimator, weather
features, history or preprocessing invalidates that snapshot. Legacy
sensor-only records also require a new fit. Until retraining succeeds, runs
use the existing real-sensor bootstrap fallback and ML-not-ready repair;
automatic training retains its history check and retry cooldown. A model that
matches the settings is also refitted weekly, because a trained model does not
learn from new data; if the history check fails at that point the working model
is kept and the check is retried later. The action and automatic fits share a
lock. An attempted fit invalidates the previous
snapshot before contacting EMHASS, so a failed retrain is not certified.

This checks Companion's last successful fit settings, not the contents of
backend pickle files. Deletion, replacement or external retraining of those
files is not detectable through this local snapshot; existing failed-run
fallback still applies. Compatible trained model files and valid backend
initialisation remain required. Runtime overrides cannot replace trained models.

## Manual export

**Write settings to EMHASS** (the admin-only
`emhass_companion.export_settings` action; there is no button) is the only way the persistent
projection reaches EMHASS. It uses
`coordinator._persistent_baseline`, which feeds the resolver the same
configuration, cost function, rounding mode, profile precedence, configured PV
enablement and version gates as an optimisation request, then keeps only
`EffectiveSettings.persistent()`.

1. Resolve the baseline; a profile that no longer resolves stops the export
   before EMHASS is contacted.
2. `GET /get-config` for the active configuration. An empty or non-object
   answer aborts: `/set-config` rebuilds from EMHASS defaults, so posting only
   Companion's keys would reset everything else.
3. Overlay the baseline onto the fetched configuration and `POST /set-config`
   with the whole result. Unrelated keys, including credentials, storage paths,
   solver selection and settings from newer EMHASS releases, round-trip
   unchanged.

Only the allowlist is written, with the same explicit clearing values as
requests (empty topology, `[]` curves/derating, `false` flags). Fields the
detected backend version does not support are left as the backend has them.

Because clearing values are part of the baseline, the export deliberately
overwrites some backend values a user may have set by hand: a saved
`heat_topology`, thermal tanks, battery charge taper, inverter power curves,
derating and the one-battery setting are reset to their empty values, and the
native forecast and tariff settings take Companion's defaults (placeholders,
not real prices) unless a profile supplies them. An empty `load_peak_hour_periods`, which clears peak pricing in a request, is
saved as EMHASS's own placeholder period instead, because EMHASS's configuration
page cannot render an empty value there and stops building the sections after
Tariff. The user-facing list is under
[Hub controls](guide/hub_controls.md#write-settings-to-emhass).
Live and temporary inputs are not in the allowlist, and the grid limits written
are the configured scalars, not the sensor/phase/window-constrained values.

A failed read, refused save or unreachable backend raises an error naming the
step and the HTTP status; success is logged only after `/set-config` accepts
the request. The fetched configuration, and any response body, is never logged
or quoted in an error. Exports are serialised. No backend file is read or
written and no YAML is produced.

This makes EMHASS's own UI and manual runs more representative of Companion's
settings. It is not a reproduction of a Companion run: that needs the original
runtime payload (live inputs, loads, forecasts) and compatible backend and
trained-model state. Normal runs never require it.

## Setup and reload

Setup no longer persists the timestep, load sensor, `var_model`, lag count or
any other Companion setting. It makes one API-only initialisation call:
fetch the active configuration and save it back unchanged. EMHASS rejects every
`/action/*` request until it has a saved configuration, and `/set-config`
creates it; for a backend that already has one the call is a no-op, so a value
chosen in EMHASS (timestep, load sensor, ML model) is never overwritten by a
reload. Nothing is read from or written to backend files.

- **Backend unavailable at setup:** the failure is logged and setup continues.
  Reloading retries it. Runs, fits and the manual export report their own
  errors.
- **Missing baseline:** an empty answer from `/get-config` is saved back as the
  EMHASS defaults, which creates the baseline.
- **Not required for normal runs:** every request carries the effective static
  settings, so a run needs neither a successful initialisation call nor a
  manual export, only the baseline EMHASS itself requires.
- **Still backend-owned:** that baseline, credentials and connection options,
  and trained model files. A changed timestep, sensor or lag count invalidates
  Companion's record of the last fit (see above) rather than being pushed to
  EMHASS; retrain, or export, as needed.

## Compatibility and explicit clearing

The minimum supported baseline is EMHASS **0.17.9**. Unknown, missing or invalid
versions receive only this known capability set. Below-baseline versions are
already rejected at integration setup. New optional fields are omitted on older
or unknown versions, including custom-profile contributions to those fields.

| First supported release | Runtime field | Default / clearing policy |
| --- | --- | --- |
| 0.17.9 | Base static inventory, `heat_topology`, electric flags and direct `def_load_config` runtime handler | Empty object disables topology; empty/per-load-empty arrays clear flat load models. False/zero disable owned flags and penalties. |
| 0.18.0 | `number_of_batteries` | `1`; Companion represents one aggregate battery. |
| 0.18.2 | `capacity_charge_interval_timesteps` | `1` unless a live demand-charge interval overrides it; action only. Prevents an inherited rolling interval changing instantaneous tariffs. |
| 0.18.3 | `battery_charge_power_derating` | `[]` means flat charge-power ceiling, not inherited derating. |
| 0.18.5 | `battery_soc_final_reward_factor`, both inverter curves, tariff timezone | `0`, `[]`, `[]`, `""` respectively. |
| 0.18.6 | Direct `shared_thermal_tanks` runtime handler, COP solver fields | `[]` clears shared tanks; pinned COP defaults prevent inheritance. |

These gates were checked against upstream tagged `associations.csv` and the
special handlers in `utils.treat_runtimeparams`, not just association presence.
In particular, `def_load_config` had a direct handler before it was listed in
associations. Shared tanks cannot be reliably cleared per-request on an older
backend without its direct handler; upgrading is required for that isolation.
No field uses JSON null to clear a saved setting. Unset Companion curtailment
now explicitly resolves to false rather than inheriting the backend value.

Pinned native tariff defaults are fallback values, not evidence of a working
price source. Runs without price series or explicit profile tariff methods
produce warnings, and support diagnostics flag default-only forecast sources.
Do not rely on those plans until a price source has been configured. History
defaults deliberately replace saved preprocessing/history choices; custom
profiles can supply supported alternatives.

## Profiles and rounding

Existing selection order remains price, PV, load, then temperature; later
profiles win. Supported static fields in the inventory may override defaults
and Companion values, including solver, native forecast/model, PV, tariff and
optional inverter settings. Unknown keys, backend-owned fields, live-input
keys and null values are ignored with a warning naming the key only, never its
value. Version gates apply **after** profiles, so a profile cannot bypass them.

Timestep, rounding, one-battery representation, empty topology/tanks, dynamic
grid safety limits and `var_model` (always the load sensor) are Companion
invariants, not profile overrides. A profile value that cannot be used, such as
a non-numeric `historic_days_to_retrieve` for ML, fails the run, fit or export
with a message naming the setting rather than an unhandled error. Registry
load topology/progress cannot be replaced by a profile. This intentionally
narrows the previous unrestricted dictionary merge to an explicit contract.
Lockout penalties are applied after profile baseline weights and cannot be
replaced by them. Nonempty inverter curves are ignored with a warning when
the effective hybrid flag is off, preventing invalid runtime requests.

Production PV enablement comes from the selected PV profile, not the current
availability of its forecast series. The No solar profile still explicitly
disables PV. Static exports must pass configured enablement to the resolver;
the standalone payload builder's series inference is not an export input.

Rounding policy retains the coordinator's selected mode (`nearest` initially,
with its existing plan-based learning) and sends it as `method_ts_round` on
every request. All positional arrays and coverage endpoints use
`PayloadInputs.row_zero`; custom profiles cannot change rounding behind them.
Timestamped forecasts are aligned by EMHASS. A request crossing a time-grid
boundary between preparation and backend execution still has clock-race risk;
this change does not introduce a server-side request timestamp.

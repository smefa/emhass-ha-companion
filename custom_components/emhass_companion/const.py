"""Constants for the EMHASS Companion integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "emhass_companion"

# --- EMHASS backend contract -------------------------------------------------

# Minimum EMHASS version. 0.17.9 introduced GET /api/v1/plan, /api/v1/last-run and
# /healthz, which this integration depends on: we read the plan as JSON instead of
# using EMHASS's publish-data machinery. Below this, there is no supported path.
MIN_EMHASS_VERSION: Final = "0.17.9"

# We pin to major 1 of the plan output schema (docs/plan_output_schema.md). A major
# bump means a column was renamed/removed or a unit/sign convention changed, so
# continuing to parse would silently produce wrong numbers.
SUPPORTED_PLAN_SCHEMA_MAJOR: Final = 1

DEFAULT_PORT: Final = 5000
DEFAULT_URL: Final = f"http://localhost:{DEFAULT_PORT}"
# The add-on has no official listing, so it is only ever installed from a
# third-party repository -- Supervisor always prefixes its slug with a hash of
# that repository's URL (e.g. "5b918bf2_emhass"), never the bare "emhass".
# The hash isn't knowable in advance, so the add-on is found by its name
# instead of a hardcoded slug; see `_async_addon_url` in config_flow.py.
ADDON_NAME: Final = "EMHASS"

ENDPOINT_HEALTHZ: Final = "/healthz"
ENDPOINT_PLAN: Final = "/api/v1/plan"
ENDPOINT_LAST_RUN: Final = "/api/v1/last-run"
ENDPOINT_GET_CONFIG: Final = "/get-config"
ENDPOINT_SET_CONFIG: Final = "/set-config"
ENDPOINT_ACTION: Final = "/action/{action}"

ACTION_DAYAHEAD: Final = "dayahead-optim"
ACTION_MPC: Final = "naive-mpc-optim"
ACTION_PERFECT: Final = "perfect-optim"
ACTION_FORECAST_FIT: Final = "forecast-model-fit"

# EMHASS config.json keys that must stay consistent with each other and with
# what a run actually asks for. `var_model` (optim_conf) is a separate,
# independently-persisted copy of the load sensor from `retrieve_hass_conf`'s
# `sensor_power_load_no_var_loads` -- EMHASS's own `_get_ml_param()` falls back
# to whichever `var_model` is currently on disk regardless of a per-request
# `sensor_power_load_no_var_loads` override, so the two must be pushed together.
EMHASS_CONF_SENSOR_LOAD: Final = "sensor_power_load_no_var_loads"
EMHASS_CONF_VAR_MODEL: Final = "var_model"
EMHASS_CONF_NUM_LAGS: Final = "num_lags"
EMHASS_CONF_LOAD_FORECAST_METHOD: Final = "load_forecast_method"
# Same wire name EMHASS's own runtime parameter uses (see payload.py); a
# mismatch between this persisted value and what a run actually sends is a
# hard crash inside EMHASS's skforecast layer, not a soft error.
EMHASS_CONF_TIME_STEP: Final = "optimization_time_step"
LOAD_FORECAST_METHOD_MLFORECASTER: Final = "mlforecaster"
LOAD_FORECAST_METHOD_NAIVE: Final = "naive"
LOAD_FORECAST_METHOD_TYPICAL: Final = "typical"
LOAD_FORECAST_METHOD_LIST: Final = "list"

# Minimum days of recorder history EMHASS's own forecast-model-fit requires --
# utils.py forces `historic_days_to_retrieve` up to this floor itself and
# warns below it ("this could cause an error with the fit"). Checked
# proactively before ever auto-triggering a fit, so a freshly created "Create
# a house load sensor" entity (no history of its own yet) is never handed a
# guaranteed-failing request.
ML_MIN_HISTORY_DAYS: Final = 9

# Below this much real recorder history, EMHASS's own "naive" load forecast
# method hard-errors instead of degrading -- it slices exactly one horizon's
# worth of rows out of its own retrieved history (`df.iloc[-forecast_horizon:]`)
# and crashes on a shape mismatch if there are fewer. Below the floor, the
# coordinator builds its own bootstrap series instead of asking for "naive".
NAIVE_FALLBACK_MIN_HISTORY: Final = timedelta(hours=24)
# How far back to pull real history when building that bootstrap series --
# long enough that repeating "the same time a day ago" (Series.
# extended_with_previous_day) has a real point to copy across a horizon of
# up to two days.
LOAD_BOOTSTRAP_LOOKBACK: Final = timedelta(hours=48)

# --- Config entry keys -------------------------------------------------------

CONF_URL: Final = "url"
CONF_USE_ADDON: Final = "use_addon"

CONF_TIME_STEP: Final = "optimization_time_step"
CONF_MPC_INTERVAL: Final = "mpc_interval_minutes"
CONF_HORIZON_HOURS: Final = "horizon_hours"
CONF_DAYAHEAD_FALLBACK_TIME: Final = "dayahead_fallback_time"

CONF_PROFILE: Final = "profile"
CONF_PROFILE_OPTIONS: Final = "profile_options"
CONF_PRICE: Final = "price"
CONF_PV: Final = "pv"
CONF_LOAD: Final = "load"
CONF_INVERTER: Final = "inverter"
CONF_TEMPERATURE: Final = "temperature"

CONF_TARIFF: Final = "tariff"
CONF_BUY: Final = "buy"
CONF_SELL: Final = "sell"
CONF_MODE: Final = "mode"
CONF_MULTIPLIER: Final = "multiplier"
CONF_ADDER: Final = "adder"
CONF_TEMPLATE: Final = "template"

CONF_BATTERY: Final = "battery"
CONF_USE_BATTERY: Final = "use_battery"
CONF_CAPACITY_WH: Final = "capacity_wh"
CONF_CHARGE_POWER_MAX: Final = "charge_power_max_w"
CONF_DISCHARGE_POWER_MAX: Final = "discharge_power_max_w"
CONF_SOC_MIN: Final = "soc_min"
CONF_SOC_MAX: Final = "soc_max"
CONF_SOC_TARGET: Final = "soc_target"
CONF_CHARGE_EFFICIENCY: Final = "charge_efficiency"
CONF_DISCHARGE_EFFICIENCY: Final = "discharge_efficiency"
# Cycle-cost penalties, sent as EMHASS's `weight_battery_discharge` /
# `weight_battery_charge`. Currency per kWh of battery throughput, in the same
# currency as the tariff prices: EMHASS subtracts weight * energy from the
# objective it maximises, so they buy back the wear a purely price-driven plan
# would ignore. Zero (EMHASS's own default) means cycle freely.
CONF_WEIGHT_BATTERY_DISCHARGE: Final = "weight_battery_discharge"
CONF_WEIGHT_BATTERY_CHARGE: Final = "weight_battery_charge"
# Dwell penalties on the *level* rather than the throughput. EMHASS charges
# cost * (kWh past the threshold) * hours held there, so unlike soc_min/soc_max
# -- which are hard planning bounds -- these bend the plan without ever making
# it infeasible. Deficit discourages sitting low (keeps a reserve), surplus
# discourages sitting full (calendar ageing). A cost of zero disables its half
# outright: EMHASS skips building the constraint unless cost and threshold are
# both above zero.
CONF_BATTERY_SOC_DEFICIT_THRESHOLD: Final = "battery_soc_deficit_threshold"
CONF_BATTERY_SOC_DEFICIT_COST: Final = "battery_soc_deficit_cost"
CONF_BATTERY_SOC_SURPLUS_THRESHOLD: Final = "battery_soc_surplus_threshold"
CONF_BATTERY_SOC_SURPLUS_COST: Final = "battery_soc_surplus_cost"
# C-rate penalty: a quadratic cost on battery power magnitude, so half power
# costs a quarter as much and the plan spreads a charge out rather than
# slamming it into the cheapest hour. `segments` is the piecewise-linear
# discretisation of that curve -- a solver knob, not a battery property, and
# EMHASS gates the whole thing on stress_cost > 0 and a non-zero power limit.
CONF_BATTERY_STRESS_COST: Final = "battery_stress_cost"
CONF_BATTERY_STRESS_SEGMENTS: Final = "battery_stress_segments"
# How the end-of-horizon SOC target (EMHASS's `soc_final`) is chosen. Optimized
# computes it each run from the forecast tails past the horizon (terminal.py);
# same_as_start pins it to the live SOC (the pre-0.9 behaviour); fixed_50
# always asks for the soc_target slider's value (the value key is unchanged
# for backwards compatibility with stored config entries, even though the
# behaviour and label moved off a hardcoded 50%). See docs/end_soc_plan.md.
CONF_END_SOC_MODE: Final = "end_soc_mode"
END_SOC_OPTIMIZED: Final = "optimized"
END_SOC_SAME_AS_START: Final = "same_as_start"
END_SOC_FIXED_50: Final = "fixed_50"
END_SOC_MODES: Final = (END_SOC_OPTIMIZED, END_SOC_SAME_AS_START, END_SOC_FIXED_50)

# A hybrid inverter shares one AC-side throughput limit between PV and
# battery; a "two separate inverters" plant has no such shared cap. Lives
# alongside the battery fields (same form) since it is meaningless without one.
CONF_HYBRID_INVERTER: Final = "hybrid_inverter"
CONF_INVERTER_AC_OUTPUT_MAX: Final = "inverter_ac_output_max_w"
CONF_INVERTER_AC_INPUT_MAX: Final = "inverter_ac_input_max_w"
CONF_INVERTER_EFFICIENCY_DC_AC: Final = "inverter_efficiency_dc_ac"
CONF_INVERTER_EFFICIENCY_AC_DC: Final = "inverter_efficiency_ac_dc"

CONF_GRID_IMPORT_MAX: Final = "grid_import_max_w"
CONF_GRID_EXPORT_MAX: Final = "grid_export_max_w"
# Demand (capacity) charge on the single highest import power over the horizon,
# in currency per kW. A grid setting, not a battery one: EMHASS prices it off
# `peak_import`, which exists whether or not a battery does -- deferrable loads
# can shave a peak on their own. Kept out of _battery_settings for exactly that
# reason, since that helper returns early when the battery is switched off.
CONF_CAPACITY_COST_PER_KW: Final = "capacity_cost_per_kw"
# EMHASS's own PV curtailment. A `plant_conf` parameter on its side, listed in
# its associations.csv, so it can be set per run through runtimeparams rather
# than only in the add-on's stored configuration. Turning it on is what makes
# the optimiser produce the `P_PV_curtailment` column that
# strategy.decide_curtailment's primary rule reads -- without it that rule can
# never fire, whatever the inverter profile supports.
CONF_COMPUTE_CURTAILMENT: Final = "compute_curtailment"

CONF_SOC_ENTITY: Final = "soc_entity"
CONF_LOAD_ENTITY: Final = "load_entity"
# The live PV power reading blended into the first naive-mpc-optim forecast
# step (payload.build_payload), weighted by number.MixBetaNumber. No PV
# profile exposes a raw current-power sensor on its own -- they are all
# forecast sources -- so this is asked for separately.
CONF_PV_ENTITY: Final = "pv_entity"
# The whole-house sensor picked in the "Create a house load sensor" step. Set
# only when that flow was used; its presence is what tells EmhassConfig to
# resolve the auto-created net-load sensor's entity id into the load/sensor
# profile's `entity` option rather than trusting whatever is stored there.
CONF_HOUSE_LOAD_TOTAL_ENTITY: Final = "house_load_total_entity"

# Deferrable load subentry keys
CONF_NAME: Final = "name"
CONF_NOMINAL_POWER: Final = "nominal_power_w"
# Floor on the power a load may draw while it is on, sent as EMHASS's
# `minimum_power_of_deferrable_loads`. Only bites when the load is *not*
# semi-continuous: semi-cont pins the power to `nominal * binary`, leaving
# nothing for a floor to constrain.
CONF_MINIMUM_POWER: Final = "minimum_power_w"
CONF_OPERATING_HOURS: Final = "operating_hours"
CONF_EARLIEST_START: Final = "earliest_start"
CONF_LATEST_END: Final = "latest_end"
CONF_SEMI_CONTINUOUS: Final = "semi_continuous"
CONF_SINGLE_CONSTANT: Final = "single_constant"
CONF_STARTUP_PENALTY: Final = "startup_penalty"
# Hard cap on how many times a load may be switched on across the horizon,
# sent as EMHASS's `set_deferrable_max_startups`. 0 means no cap; unlike the
# startup penalty this is a constraint, not a cost, so a too-low value makes
# the requested run time unreachable rather than merely expensive.
CONF_MAX_STARTUPS: Final = "max_startups"
# Minimum dwell time once a load switches on/off, protecting compressor-driven
# loads (heat pumps, freezers) from short-cycling. Minutes in config/UI, sent
# to EMHASS as timesteps (`def_minimum_on_time`/`def_minimum_off_time`). 0
# means no minimum.
CONF_MINIMUM_ON_TIME: Final = "minimum_on_time_minutes"
CONF_MINIMUM_OFF_TIME: Final = "minimum_off_time_minutes"
# Optional: the load's own running sensor -- a numeric power sensor or a plain
# on/off binary_sensor, both read by deferrable.state_to_power. Without one
# (and no control entity either) the optimiser has no way to observe the load
# at all; see DeferrableRuntime.assume_from_plan for what happens instead. The
# key stays "power_sensor" for backwards compatibility with existing subentries
# even though it now accepts a binary_sensor too.
CONF_POWER_SENSOR: Final = "power_sensor"
CONF_USE_TIME_WINDOW: Final = "use_time_window"
# Optional: what the executor switches to actually run the load. Without it the
# load is advisory only and the user automates on should_run themselves.
CONF_CONTROL_ENTITY: Final = "control_entity"

# Surplus loads. The margin a timestep's surplus must clear *above* the load's
# own draw before it counts towards the budget. Its whole job is to absorb PV
# forecast error: a slot counted at exactly the load's power imports the moment
# the forecast comes in light. It also opens a band -- between the load's power
# and the load's power plus this -- of slots that do not add to the budget but
# are still safe to run through, which is what lets a startup penalty produce
# one unbroken run without a hard contiguity constraint.
CONF_SURPLUS_HEADROOM: Final = "surplus_headroom_w"
# A total, not a rate: "put this much in and stop". 0 means no cap, and the
# load runs on whatever surplus exists until its request is turned off.
CONF_ENERGY_NEEDED: Final = "energy_needed_kwh"
# Which surplus load gets first claim on a shared series -- lower runs first,
# ties broken by name. Only meaningful with two or more surplus loads; a
# single one has nothing to compete with.
CONF_SURPLUS_PRIORITY: Final = "surplus_priority"
# What makes the load want to run; see RECURRENCES below. Asked first when
# adding a load, because it decides which of the other fields even apply.
CONF_RECURRENCE: Final = "recurrence"

# Thermal deferrable loads
CONF_LOAD_TYPE: Final = "load_type"
LOAD_TYPE_STANDARD: Final = "standard"
LOAD_TYPE_THERMAL: Final = "thermal"
LOAD_TYPES: Final = (LOAD_TYPE_STANDARD, LOAD_TYPE_THERMAL)
CONF_TEMPERATURE_SENSOR: Final = "temperature_sensor"
CONF_SENSE: Final = "sense"
CONF_HEATING_RATE: Final = "heating_rate"
CONF_COOLING_CONSTANT: Final = "cooling_constant"
CONF_THERMAL_INERTIA: Final = "thermal_inertia"
CONF_COMFORT_TEMPERATURE: Final = "comfort_temperature"
CONF_SETBACK_TEMPERATURE: Final = "setback_temperature"
CONF_MAX_TEMPERATURE: Final = "max_temperature"
CONF_COMFORT_START: Final = "comfort_start"
CONF_COMFORT_END: Final = "comfort_end"

SUBENTRY_TYPE_DEFERRABLE: Final = "deferrable_load"
# A separate subentry type, not a field on the deferrable one: each type gets
# its own "Add ..." button in the integrations UI, and what a load *is* --
# run-time-driven or temperature-driven -- is fixed at creation, exactly the
# property a subentry type is meant to carry.
SUBENTRY_TYPE_THERMAL: Final = "thermal_load"
LOAD_SUBENTRY_TYPES: Final = (SUBENTRY_TYPE_DEFERRABLE, SUBENTRY_TYPE_THERMAL)
# A load group is not itself a load: it expresses a relationship between
# existing deferrable-load subentries (a shared power budget or mutual
# exclusion), so it is deliberately excluded from LOAD_SUBENTRY_TYPES above --
# the registry and entity-platform filters that iterate that tuple should
# never try to treat a group as a load.
SUBENTRY_TYPE_LOAD_GROUP: Final = "load_group"
CONF_GROUP_LOAD_IDS: Final = "load_subentry_ids"
CONF_GROUP_MAX_POWER: Final = "max_power_w"
CONF_GROUP_MUTUAL_EXCLUSION: Final = "mutual_exclusion"

# --- Defaults ----------------------------------------------------------------

# 30 minutes is EMHASS's own default and keeps the LP small. 15 captures more detail
# in markets that settle at 15-minute resolution, at roughly double the problem size.
DEFAULT_TIME_STEP: Final = 30
DEFAULT_MPC_INTERVAL: Final = 15
DEFAULT_HORIZON_HOURS: Final = 24
DEFAULT_DAYAHEAD_FALLBACK_TIME: Final = "13:30:00"

DEFAULT_SOC_MIN: Final = 0.10
DEFAULT_SOC_MAX: Final = 0.95
# Under the default Optimized end-SOC mode this is only the reserve floor the
# plan may never end below, not a level to aim for, so it wants to be low: a
# high floor quietly blocks profitable evening discharge. 20% sits just above
# the 10% soc_min and leaves a usable backup buffer.
DEFAULT_SOC_TARGET: Final = 0.20
DEFAULT_END_SOC_MODE: Final = END_SOC_OPTIMIZED
DEFAULT_CHARGE_EFFICIENCY: Final = 0.95
DEFAULT_DISCHARGE_EFFICIENCY: Final = 0.95
# EMHASS's own defaults: no cycle cost, so the plan plays the price spread for
# any profit at all. Left at zero the payload is byte-identical to before.
DEFAULT_WEIGHT_BATTERY_DISCHARGE: Final = 0.0
DEFAULT_WEIGHT_BATTERY_CHARGE: Final = 0.0
# All EMHASS's own defaults. The thresholds are non-zero but inert on their
# own -- each pair only builds a constraint once its cost is set above zero --
# so the shipped values simply pre-fill sensible bands rather than change any
# plan. Stored as 0-1 fractions, like every other SOC field.
DEFAULT_BATTERY_SOC_DEFICIT_THRESHOLD: Final = 0.40
DEFAULT_BATTERY_SOC_DEFICIT_COST: Final = 0.0
DEFAULT_BATTERY_SOC_SURPLUS_THRESHOLD: Final = 0.90
DEFAULT_BATTERY_SOC_SURPLUS_COST: Final = 0.0
DEFAULT_BATTERY_STRESS_COST: Final = 0.0
# 10 piecewise-linear pieces per quadratic curve. More tracks the curve closer
# at the cost of two extra solver constraints each; EMHASS's own default.
DEFAULT_BATTERY_STRESS_SEGMENTS: Final = 10
# EMHASS's own default for both AC/DC conversion directions -- no assumed loss
# unless the user knows one and states it.
DEFAULT_INVERTER_EFFICIENCY: Final = 1.0
DEFAULT_GRID_IMPORT_MAX: Final = 9000
DEFAULT_GRID_EXPORT_MAX: Final = 9000
# Zero is a true no-op in EMHASS: the peak_import variable is only created when
# this is above zero, so an untouched config solves exactly the same problem.
DEFAULT_CAPACITY_COST_PER_KW: Final = 0.0

# Executor
DEFAULT_POWER_DEADBAND_W: Final = 100
STALE_PLAN_FACTOR: Final = 2

# Self-consumption classification (strategy.decide_battery): "does the plan
# want any grid exchange at all", read straight from the plan's own P_grid
# column. Not the same question as soc_min/soc_max -- those are planning
# limits, this is a real-time mode choice.
DEFAULT_SELF_CONSUME_THRESHOLD_W: Final = 300
# Once in self-consumption, P_grid must clear threshold * this factor before
# forcing resumes -- damps boundary chatter a single static threshold would
# let through on every recalculation.
SELF_CONSUME_EXIT_FACTOR: Final = 2.0

# What the form offers for EMHASS's own curtailment, matching EMHASS's own
# default. Distinct from "not configured" (None), which is what an entry saved
# before this setting existed carries: that sends nothing and leaves whatever
# the add-on has stored alone, rather than silently switching it off.
DEFAULT_COMPUTE_CURTAILMENT: Final = False

# Surplus loads
DEFAULT_SURPLUS_HEADROOM_W: Final = 300
# 0 for every load until someone sets otherwise, so allocation falls back to
# the existing name order -- adding this feature changes nothing by default.
DEFAULT_SURPLUS_PRIORITY: Final = 0
# Reporting only: the level the hub's surplus binary sensor and its start/end
# timestamps describe. Deliberately *not* what any load budgets against -- that
# threshold is the load's own power plus its own headroom, because "is there
# enough sun for the pool" and "is there enough sun for the car" are different
# questions with different answers.
DEFAULT_SURPLUS_THRESHOLD_W: Final = 500
# Weight given to the live PV/load reading when blended into the first
# naive-mpc-optim forecast step (payload.build_payload). 0.5 matches EMHASS's
# own default for the same blend (Forecast.get_mix_forecast).
DEFAULT_MIX_BETA: Final = 0.5

# --- Modes -------------------------------------------------------------------

MODE_AUTO: Final = "auto"
MODE_SELF_CONSUME: Final = "self_consume"
MODE_FORCE_CHARGE: Final = "force_charge"
MODE_FORCE_DISCHARGE: Final = "force_discharge"
MODE_IDLE: Final = "idle"

BATTERY_ACTIONS: Final = (
    MODE_SELF_CONSUME,
    MODE_FORCE_CHARGE,
    MODE_FORCE_DISCHARGE,
    MODE_IDLE,
)
SYSTEM_MODES: Final = (MODE_AUTO, *BATTERY_ACTIONS)

# --- Cost function -------------------------------------------------------------
# EMHASS's own optimisation.py objective-function branches; sent verbatim as
# the "costfun" runtime parameter (payload.build_payload), so these must match
# its literal strings exactly, hyphen included.

COST_FUN_PROFIT: Final = "profit"
COST_FUN_COST: Final = "cost"
COST_FUN_SELF_CONSUMPTION: Final = "self-consumption"
COST_FUNS: Final = (COST_FUN_PROFIT, COST_FUN_COST, COST_FUN_SELF_CONSUMPTION)
DEFAULT_COST_FUN: Final = COST_FUN_PROFIT

# --- Inverter actions --------------------------------------------------------
#
# The four battery actions above are what a plan resolves to. These are the
# lifecycle and side-channel actions around them. A profile defines only the
# ones its hardware has: the set of actions it defines *is* its capability
# list, which is why there is no separate `capabilities:` block to keep in sync.

# Run once before the first forced command of a session. This is where an
# inverter that gates remote control behind a mode -- SolarEdge's "Remote
# Control", Victron's "External control", Sigenergy's remote_ems_enable --
# opens that gate.
ACTION_PREPARE: Final = "prepare"

# How control is handed back. Distinct from `self_consume` because for some
# inverters giving control back means undoing `prepare` as well, and because
# `restore` must run on paths where no plan exists at all (shutdown, unload).
# Falls back to `self_consume` when a profile does not define it.
ACTION_RESTORE: Final = "restore"

# PV curtailment, driven by the plan's own P_PV_curtailment column rather than
# by the battery decision -- the two are independent.
ACTION_CURTAIL: Final = "curtail"
ACTION_UNCURTAIL: Final = "uncurtail"

# Grid charging is separately permissioned on most hybrids (Growatt's
# allow_grid_charge, Deye's grid_charge_enabled, SolarEdge's ac_charge_policy).
# EMHASS knows nothing about it, so a plan can schedule a grid charge the
# inverter will silently refuse unless the gate is asserted first.
ACTION_ALLOW_GRID_CHARGE: Final = "allow_grid_charge"
ACTION_BLOCK_GRID_CHARGE: Final = "block_grid_charge"

INVERTER_ACTIONS: Final = (
    *BATTERY_ACTIONS,
    ACTION_PREPARE,
    ACTION_RESTORE,
    ACTION_CURTAIL,
    ACTION_UNCURTAIL,
    ACTION_ALLOW_GRID_CHARGE,
    ACTION_BLOCK_GRID_CHARGE,
)

LOAD_MODE_AUTO: Final = "auto"
LOAD_MODE_FORCE_ON: Final = "force_on"

# A daily load wants its operating_hours every day, same as today's only
# behaviour. An on-demand load wants nothing until armed -- see
# DeferrableRuntime.requested and docs/on_demand_loads.md.
#
# A surplus load is armed the same way, but asks for no fixed run time at all:
# its hours and its window are derived from the exported power the last plan
# predicted (see surplus.py), so it takes whatever the optimiser can spare and
# nothing more. Its operating hours, time window and deadline are all derived
# rather than configured, and the entities backing them report unavailable.
RECURRENCE_DAILY: Final = "daily"
RECURRENCE_ON_DEMAND: Final = "on_demand"
RECURRENCE_SURPLUS: Final = "surplus"
RECURRENCES: Final = (RECURRENCE_DAILY, RECURRENCE_ON_DEMAND, RECURRENCE_SURPLUS)

# Attributes of the Requested switch. requested_at is the instant a deadline
# counts from, and is carried here rather than in an entity of its own so the
# whole request -- flag plus anchor -- is restored as one unit. request_runtime
# is the progress made towards operating_hours since that anchor -- without it
# surviving a restart too, a request that already ran its full target (or any
# part of it) forgets that progress and reverts to looking freshly armed, with
# requested_at still the original, now stale, anchor (issue: "deferrable not
# updating run time").
ATTR_REQUESTED_AT: Final = "requested_at"
ATTR_DEADLINE_AT: Final = "deadline_at"
ATTR_REQUEST_RUNTIME_SECONDS: Final = "request_runtime_seconds"

# --- Profiles ----------------------------------------------------------------

PROFILE_KIND_PRICE: Final = "price"
PROFILE_KIND_PV: Final = "pv"
PROFILE_KIND_LOAD: Final = "load"
PROFILE_KIND_INVERTER: Final = "inverter"
# Outdoor temperature, needed only once a thermal deferrable load exists.
PROFILE_KIND_TEMPERATURE: Final = "temperature"
PROFILE_KINDS: Final = (
    PROFILE_KIND_PRICE,
    PROFILE_KIND_PV,
    PROFILE_KIND_LOAD,
    PROFILE_KIND_TEMPERATURE,
    PROFILE_KIND_INVERTER,
)
# Built-in profile keys are f"{kind}/{filename stem}" (profiles/__init__.py).
# The "House load sensor" profile is the one load profile with a live
# load-power sensor of its own (profiles/builtin/load/sensor.yaml) --
# coordinator._read_load_live reads it directly rather than asking for a
# second, separate live-load entity.
PROFILE_KEY_LOAD_SENSOR: Final = "load/sensor"

# Not a real profile: a config-flow-only choice offered alongside the load
# profiles that instead walks through creating one (picking a whole-house
# sensor, then auto-configuring PROFILE_KEY_LOAD_SENSOR against a sensor this
# integration creates and keeps net of every deferrable/thermal load). Kept
# out of profiles/builtin entirely since nothing about it is declarative.
LOAD_PROFILE_CREATE_SENTINEL: Final = "__create__"

# Explicit display order for the load-profile picker. available_profiles()
# otherwise returns profiles in the alphabetical file-load order from
# profiles/__init__.py (emhass_native, forecast_entity, sensor), which buries
# "point me at a sensor" -- the option most users with a whole-house meter
# want -- under two others. Renaming the underlying files would change their
# profile keys and break every existing config entry, so the reorder happens
# here instead. Any profile not listed (a user-authored one) sorts after
# these, in whatever order it was loaded.
LOAD_PROFILE_ORDER: Final = (
    LOAD_PROFILE_CREATE_SENTINEL,
    PROFILE_KEY_LOAD_SENSOR,
    "load/emhass_native",
    "load/forecast_entity",
)

# Same idea for the price and PV pickers: Nord Pool and Solcast are the
# integrations most users in EMHASS's core markets (Nordics/Europe) already
# have, so they lead the list instead of sorting alphabetically behind
# ENTSO-E/fixed-tariff/Tibber or forecast.solar/generic-attribute. Anything
# not listed here sorts after these, in load order, same as LOAD_PROFILE_ORDER.
PRICE_PROFILE_ORDER: Final = (
    "price/nordpool_core",
    "price/nordpool_custom",
)
PV_PROFILE_ORDER: Final = ("pv/solcast",)

# The temperature picker: a Home Assistant weather entity is the option most
# users with a weather integration already have, followed by the generic
# attribute escape hatch, with EMHASS's own Open-Meteo fetch last since it
# only works while the solar forecast is also Open-Meteo. Alphabetical
# file-load order would otherwise put Open-Meteo first.
TEMPERATURE_PROFILE_ORDER: Final = (
    "temperature/weather_entity",
    "temperature/generic_attribute",
    "temperature/emhass_native",
)

# Unique-id suffix for the sensor created by the "Create a house load sensor"
# flow (full unique_id is f"{entry.entry_id}_{NET_HOUSE_LOAD_KEY}"). Shared
# between sensor.py (creates the entity) and configuration.py (resolves its
# entity id back out of the registry) so the two can never drift apart.
NET_HOUSE_LOAD_KEY: Final = "net_house_load"

# The profile schema is a public API for contributors and for users writing local
# profiles. Bump only with a documented migration.
#
# 2 adds the inverter `control:` block -- command semantics (unit, sign,
# lifetime) that the executor needs in order to decide *when* to write.
# Version 1 inverter profiles keep loading and are given the defaults in
# DEFAULT_CONTROL below, which describe exactly the behaviour they had before.
PROFILE_SCHEMA_VERSION: Final = 2

# --- Inverter control semantics ----------------------------------------------
#
# The archetype does not change what the engine does -- every profile still
# resolves to a list of service calls. It exists so that a profile can be
# reviewed against the model it claims to implement, and so the config flow can
# explain the shape to a user. Behaviour comes from `lifetime` and the unit
# fields below, which are orthogonal to it.

# One signed number carries magnitude and direction (SolaX remotecontrol_active_power,
# Sigenergy active_power_fixed_adjustment).
ARCHETYPE_SIGNED_POWER: Final = "signed_power"
# Direction from a select, magnitude from a number (Sungrow, SolarEdge, GoodWe, Fox ESS).
ARCHETYPE_MODE_AND_MAGNITUDE: Final = "mode_and_magnitude"
# A service call carrying power *and* a lifetime (Huawei forcible_charge).
ARCHETYPE_COMMAND_WITH_DURATION: Final = "command_with_duration"
# The write is a grid target, not a battery one (Victron ESS AcPowerSetPoint).
ARCHETYPE_GRID_SETPOINT: Final = "grid_setpoint"
# Rewrite a time-of-use slot; no session concept at all (Deye Prog1-6, Growatt SPH).
ARCHETYPE_TOU_REWRITE: Final = "tou_rewrite"
CONTROL_ARCHETYPES: Final = (
    ARCHETYPE_SIGNED_POWER,
    ARCHETYPE_MODE_AND_MAGNITUDE,
    ARCHETYPE_COMMAND_WITH_DURATION,
    ARCHETYPE_GRID_SETPOINT,
    ARCHETYPE_TOU_REWRITE,
)

# What the number an action writes actually means. Getting this wrong is a 100x
# error delivered to somebody's inverter, which is why it is declared once per
# profile rather than open-coded in every action template.
POWER_UNIT_W: Final = "w"
POWER_UNIT_KW: Final = "kw"
# Percent of rated power (GoodWe eco_mode_power, most Growatt families).
POWER_UNIT_PERCENT: Final = "percent_of_rated"
# Percent in hundredths, 0-10000 (Fronius SunSpec InWRte/OutWRte).
POWER_UNIT_PERCENT_HUNDREDTHS: Final = "percent_hundredths"
POWER_UNITS: Final = (
    POWER_UNIT_W,
    POWER_UNIT_KW,
    POWER_UNIT_PERCENT,
    POWER_UNIT_PERCENT_HUNDREDTHS,
)
PERCENT_UNITS: Final = (POWER_UNIT_PERCENT, POWER_UNIT_PERCENT_HUNDREDTHS)

# What a `curtail`/`uncurtail` write actually targets. The mechanisms are
# physically different -- an export limit still lets PV serve load and
# battery, a zero-export switch does not distinguish -- and only the profile
# knows which one its hardware has.
CURTAIL_MODE_EXPORT_LIMIT: Final = "export_limit"
CURTAIL_MODE_ZERO_EXPORT_SWITCH: Final = "zero_export_switch"
CURTAIL_MODES: Final = (CURTAIL_MODE_EXPORT_LIMIT, CURTAIL_MODE_ZERO_EXPORT_SWITCH)

# How long a command survives without being re-sent.
#
# Registers that hold their value until changed. The deadband is safe here, and
# `restore` is mandatory -- nothing else will ever put the inverter back.
LIFETIME_PERSISTENT: Final = "persistent"
# The command carries its own duration and the inverter reverts when it elapses
# (Huawei forcible_charge). Must be re-issued before it expires.
LIFETIME_EXPIRES: Final = "expires"
# A countdown the controller is expected to keep reloading (Fox ESS timeout_set,
# SolaX remotecontrol_duration, Victron's implicit ESS revert).
LIFETIME_WATCHDOG: Final = "watchdog"
CONTROL_LIFETIMES: Final = (LIFETIME_PERSISTENT, LIFETIME_EXPIRES, LIFETIME_WATCHDOG)

# Re-issue an expiring command once this fraction of its life has passed. Half
# gives a full command's worth of margin against a slow bus or a missed cycle.
COMMAND_REFRESH_FRACTION: Final = 0.5
DEFAULT_COMMAND_DURATION_MIN: Final = 30

# Applied to every version 1 inverter profile, and as the floor under a version 2
# `control:` block. These values describe what the executor did before this
# block existed, so an unmigrated profile behaves exactly as it always did.
DEFAULT_CONTROL: Final = {
    "archetype": ARCHETYPE_MODE_AND_MAGNITUDE,
    "power_unit": POWER_UNIT_W,
    "signed": False,
    "invert_sign": False,
    "charge_boost": 1.0,
    "round_to": 1,
    "lifetime": LIFETIME_PERSISTENT,
    "duration_min": DEFAULT_COMMAND_DURATION_MIN,
    "deadband_w": DEFAULT_POWER_DEADBAND_W,
    "min_write_interval_s": 0,
    "restore_required": True,
    "curtail_mode": CURTAIL_MODE_EXPORT_LIMIT,
    "curtail_unit": POWER_UNIT_W,
}

# Deliberately only three. Anything a declarative source cannot express belongs in
# a `template` source rather than in a fourth keyword -- that is the line that
# keeps this engine from turning into a poor programming language.
SOURCE_TYPE_ATTRIBUTES: Final = "attributes"
SOURCE_TYPE_SERVICE: Final = "service"
SOURCE_TYPE_TEMPLATE: Final = "template"
SOURCE_TYPES: Final = (
    SOURCE_TYPE_ATTRIBUTES,
    SOURCE_TYPE_SERVICE,
    SOURCE_TYPE_TEMPLATE,
)

UNIT_WATTS: Final = "watts"
UNIT_CURRENCY_PER_KWH: Final = "currency_per_kwh"
UNIT_CELSIUS: Final = "celsius"

# User-supplied profiles live outside the integration directory so that HACS
# updates cannot delete them.
USER_PROFILE_DIR: Final = "emhass_companion/profiles"
BUILTIN_PROFILE_DIR: Final = "profiles/builtin"

# --- Issues ------------------------------------------------------------------

ISSUE_EMHASS_VERSION: Final = "emhass_version_unsupported"
ISSUE_PLAN_SCHEMA: Final = "plan_schema_unsupported"
ISSUE_BAD_PROFILE: Final = "invalid_profile"
# Suffixed with the load's subentry_id: one issue per thermal load, not one
# for the whole integration, since the fix is load-specific (its own rates or
# schedule) and other loads are unaffected.
ISSUE_THERMAL_UNREACHABLE: Final = "thermal_comfort_unreachable"
# Whole-integration issue, not per-load: EMHASS reports infeasibility for the
# problem as a whole, with no hint at which load caused it.
ISSUE_OPTIMIZATION_INFEASIBLE: Final = "optimization_infeasible"
# A run that errored outright (EMHASS unreachable, rejected the request, or
# raised internally) rather than merely failing to find a feasible plan.
ISSUE_RUN_FAILED: Final = "run_failed"
# End SOC's Optimized mode has little or no PV forecast past the horizon, but
# the selected Solcast profile could provide one more day by adding its day-3
# sensor. A nudge with a one-click fix, not an error -- the heuristic already
# degrades safely by assuming zero PV where the forecast ends.
ISSUE_PV_TAIL_SHORT: Final = "pv_tail_short"
# The load profile wants mlforecaster but it has not been confirmed trained
# for the currently configured sensor yet -- runs use "naive" (or a live
# -reading bootstrap, below NAIVE_FALLBACK_MIN_HISTORY) meanwhile. Clears
# once an auto- or button-triggered fit against that sensor succeeds.
ISSUE_ML_FORECASTER_NOT_READY: Final = "ml_forecaster_not_ready"

# Solcast's day sensors are named today / tomorrow / day_3..day_7 -- "tomorrow"
# *is* day 2, there is no forecast_day_2. Day 3 is the first sensor past the
# profile's old today+tomorrow default.
PROFILE_KEY_PV_SOLCAST: Final = "pv/solcast"
SOLCAST_DAY3_ENTITY: Final = "sensor.solcast_pv_forecast_forecast_day_3"

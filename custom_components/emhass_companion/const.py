"""Constants for the EMHASS Companion integration."""

from __future__ import annotations

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
ADDON_SLUG: Final = "emhass"

ENDPOINT_HEALTHZ: Final = "/healthz"
ENDPOINT_PLAN: Final = "/api/v1/plan"
ENDPOINT_LAST_RUN: Final = "/api/v1/last-run"
ENDPOINT_GET_CONFIG: Final = "/get-config"
ENDPOINT_SET_CONFIG: Final = "/set-config"
ENDPOINT_ACTION: Final = "/action/{action}"

ACTION_DAYAHEAD: Final = "dayahead-optim"
ACTION_MPC: Final = "naive-mpc-optim"
ACTION_PERFECT: Final = "perfect-optim"

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

CONF_GRID_IMPORT_MAX: Final = "grid_import_max_w"
CONF_GRID_EXPORT_MAX: Final = "grid_export_max_w"

CONF_SOC_ENTITY: Final = "soc_entity"
CONF_LOAD_ENTITY: Final = "load_entity"
CONF_PV_ENTITY: Final = "pv_entity"

# Deferrable load subentry keys
CONF_NAME: Final = "name"
CONF_NOMINAL_POWER: Final = "nominal_power_w"
CONF_OPERATING_HOURS: Final = "operating_hours"
CONF_EARLIEST_START: Final = "earliest_start"
CONF_LATEST_END: Final = "latest_end"
CONF_SEMI_CONTINUOUS: Final = "semi_continuous"
CONF_SINGLE_CONSTANT: Final = "single_constant"
CONF_STARTUP_PENALTY: Final = "startup_penalty"
# Optional: the load's own power sensor. Without it EMHASS is never told that a
# load is already running, so it can re-charge a startup penalty or re-schedule
# work already done today.
CONF_POWER_SENSOR: Final = "power_sensor"
CONF_USE_TIME_WINDOW: Final = "use_time_window"
# Optional: what the executor switches to actually run the load. Without it the
# load is advisory only and the user automates on should_run themselves.
CONF_CONTROL_ENTITY: Final = "control_entity"

SUBENTRY_TYPE_DEFERRABLE: Final = "deferrable_load"

# --- Defaults ----------------------------------------------------------------

# 30 minutes is EMHASS's own default and keeps the LP small. 15 captures more detail
# in markets that settle at 15-minute resolution, at roughly double the problem size.
DEFAULT_TIME_STEP: Final = 30
DEFAULT_MPC_INTERVAL: Final = 15
DEFAULT_HORIZON_HOURS: Final = 24
DEFAULT_DAYAHEAD_FALLBACK_TIME: Final = "13:30:00"

DEFAULT_SOC_MIN: Final = 0.10
DEFAULT_SOC_MAX: Final = 0.95
DEFAULT_SOC_TARGET: Final = 0.50
DEFAULT_CHARGE_EFFICIENCY: Final = 0.95
DEFAULT_DISCHARGE_EFFICIENCY: Final = 0.95
DEFAULT_GRID_IMPORT_MAX: Final = 9000
DEFAULT_GRID_EXPORT_MAX: Final = 9000

# Executor
DEFAULT_POWER_DEADBAND_W: Final = 100
STALE_PLAN_FACTOR: Final = 2

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

LOAD_MODE_AUTO: Final = "auto"
LOAD_MODE_FORCE_ON: Final = "force_on"
LOAD_MODE_FORCE_OFF: Final = "force_off"
LOAD_MODES: Final = (LOAD_MODE_AUTO, LOAD_MODE_FORCE_ON, LOAD_MODE_FORCE_OFF)

# --- Profiles ----------------------------------------------------------------

PROFILE_KIND_PRICE: Final = "price"
PROFILE_KIND_PV: Final = "pv"
PROFILE_KIND_LOAD: Final = "load"
PROFILE_KIND_INVERTER: Final = "inverter"
PROFILE_KINDS: Final = (
    PROFILE_KIND_PRICE,
    PROFILE_KIND_PV,
    PROFILE_KIND_LOAD,
    PROFILE_KIND_INVERTER,
)

# The profile schema is a public API for contributors and for users writing local
# profiles. Bump only with a documented migration.
PROFILE_SCHEMA_VERSION: Final = 1

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

# User-supplied profiles live outside the integration directory so that HACS
# updates cannot delete them.
USER_PROFILE_DIR: Final = "emhass_companion/profiles"
BUILTIN_PROFILE_DIR: Final = "profiles/builtin"

# --- Issues ------------------------------------------------------------------

ISSUE_EMHASS_VERSION: Final = "emhass_version_unsupported"
ISSUE_PLAN_SCHEMA: Final = "plan_schema_unsupported"
ISSUE_BAD_PROFILE: Final = "invalid_profile"

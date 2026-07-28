"""Build the EMHASS runtime-parameter payload.

Every optimisation request carries the *complete* set of inputs and settings.
EMHASS's ``associations.csv`` drives a generic override loop, so nearly every
configuration key can be supplied per-request; sending all of them means the
configuration stored inside EMHASS is never consulted for these values and can
never drift away from what Home Assistant believes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
import logging
import math
from typing import Any

from homeassistant.util import dt as dt_util

from .const import ACTION_MPC
from .models import BatteryConfig, DeferrableLoad, GridConfig, Series
from .thermal import build_def_load_config

_LOGGER = logging.getLogger(__name__)


class PayloadError(ValueError):
    """The payload could not be built."""


# --- deferrable load time windows --------------------------------------------


def in_window(moment: time, earliest: time | None, latest: time | None) -> bool:
    """Whether a wall-clock time falls inside a (possibly overnight) window."""
    if earliest is None and latest is None:
        return True
    if earliest is None:
        return moment < latest  # type: ignore[operator]
    if latest is None:
        return moment >= earliest
    if earliest <= latest:
        return earliest <= moment < latest
    # Crosses midnight, e.g. 22:00 -> 06:00.
    return moment >= earliest or moment < latest


def _next_occurrence(after: datetime, target: time, *, strict: bool = False) -> datetime:
    """The next local datetime with wall-clock time ``target``."""
    candidate = after.replace(
        hour=target.hour,
        minute=target.minute,
        second=target.second,
        microsecond=0,
    )
    while candidate < after or (strict and candidate <= after):
        candidate += timedelta(days=1)
    return candidate


def window_to_timesteps(
    earliest: time | None,
    latest: time | None,
    grid_start: datetime,
    step: timedelta,
    horizon_steps: int,
) -> tuple[int, int]:
    """Convert a wall-clock window into EMHASS timestep indices.

    EMHASS interprets these indices *relative to the moment the optimisation is
    launched*, not as wall-clock times, so they have to be recomputed on every
    single request -- a window cached at launch would slide through the day.

    In EMHASS's convention ``0`` means "unconstrained": a start of 0 is "may
    begin immediately" and an end of 0 is "may run to the end of the horizon".
    """
    if earliest is None and latest is None:
        return 0, 0

    local_start = dt_util.as_local(grid_start)
    step_seconds = step.total_seconds()

    def elapsed_steps(moment: datetime) -> float:
        """Real time between the grid start and ``moment``, in timesteps.

        Both operands are converted to UTC first. Subtracting two aware
        datetimes that share a ``tzinfo`` object makes Python skip the offset
        conversion and return the *naive* wall-clock difference, which loses
        the extra hour on the night DST ends -- a 22:00-06:00 window would be
        measured as 8 hours when 9 hours of real time elapse.
        """
        return (moment.astimezone(UTC) - local_start.astimezone(UTC)).total_seconds() / step_seconds

    # A window that is already open must not be pushed to its next opening
    # tomorrow -- at 23:00 inside a 22:00-06:00 window the load may run *now*.
    active = in_window(local_start.time(), earliest, latest)

    if earliest is None or active:
        start_index = 0
        anchor = local_start
    else:
        opens_at = _next_occurrence(local_start, earliest)
        start_index = math.ceil(elapsed_steps(opens_at))
        anchor = opens_at

    if latest is None:
        end_index = 0
    else:
        closes_at = _next_occurrence(anchor, latest, strict=True)
        end_index = math.floor(elapsed_steps(closes_at))
        # Beyond the horizon is the same as unconstrained, and saying so keeps
        # EMHASS from clamping a nonsensical index.
        if end_index >= horizon_steps:
            end_index = 0

    if start_index >= horizon_steps:
        _LOGGER.warning(
            "Deferrable load window (%s-%s) opens beyond the %d-step horizon; "
            "treating it as unconstrained for this run",
            earliest,
            latest,
            horizon_steps,
        )
        return 0, 0

    return max(start_index, 0), max(end_index, 0)


# --- payload -----------------------------------------------------------------


@dataclass(slots=True)
class PayloadInputs:
    """Everything needed to describe one optimisation request."""

    action: str
    now: datetime
    time_step_minutes: int
    horizon_steps: int
    battery: BatteryConfig
    grid: GridConfig
    loads: list[DeferrableLoad] = field(default_factory=list)
    pv: Series | None = None
    load: Series | None = None
    buy_price: Series | None = None
    sell_price: Series | None = None
    outdoor_temperature: Series | None = None
    soc_init: float | None = None
    extra_settings: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PayloadResult:
    """A built payload plus anything the user should know about it."""

    payload: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    load_order: list[str] = field(default_factory=list)
    """Subentry ids in the order they map to EMHASS's ``P_deferrable{k}``."""


def build_payload(inputs: PayloadInputs) -> PayloadResult:
    """Assemble the runtime parameters for one EMHASS request."""
    step = timedelta(minutes=inputs.time_step_minutes)
    horizon_end = inputs.now + step * inputs.horizon_steps
    warnings: list[str] = []

    payload: dict[str, Any] = {
        "optimization_time_step": inputs.time_step_minutes,
    }

    if inputs.action == ACTION_MPC:
        # Counted in timesteps, not hours -- a frequent source of horizons that
        # are wrong by a factor of two.
        payload["prediction_horizon"] = inputs.horizon_steps
    else:
        hours = inputs.horizon_steps * inputs.time_step_minutes / 60
        payload["delta_forecast_daily"] = max(1, math.ceil(hours / 24))

    # -- inputs ---------------------------------------------------------------
    # Supplying a forecast key makes EMHASS switch that forecast's method to
    # "list" by itself, so the method is deliberately not set here.
    for key, series, label in (
        ("pv_power_forecast", inputs.pv, "PV forecast"),
        ("load_power_forecast", inputs.load, "Load forecast"),
        ("load_cost_forecast", inputs.buy_price, "Buy price"),
        ("prod_price_forecast", inputs.sell_price, "Sell price"),
        (
            "outdoor_temperature_forecast",
            inputs.outdoor_temperature,
            "Outdoor temperature",
        ),
    ):
        if series is None or not series:
            continue
        payload[key] = series.to_payload()
        if not series.covers(horizon_end):
            # EMHASS forward-fills a timestamped forecast onto its grid, so a
            # short series is extended with its final value instead of raising.
            # Left unreported, the plan looks fine while being built on a
            # flat-lined price or an eternal midday sun.
            warnings.append(
                f"{label} only covers until "
                f"{dt_util.as_local(series.end):%Y-%m-%d %H:%M}, short of the "
                f"{dt_util.as_local(horizon_end):%Y-%m-%d %H:%M} horizon. EMHASS "
                "will hold the last value for the remainder."
            )

    if inputs.soc_init is not None:
        # A fraction in [0, 1]; the percentage form belongs only to display.
        payload["soc_init"] = round(inputs.soc_init, 4)

    # -- settings -------------------------------------------------------------
    payload.update(_battery_settings(inputs.battery))
    payload["maximum_power_from_grid"] = inputs.grid.import_max_w
    payload["maximum_power_to_grid"] = inputs.grid.export_max_w

    deferrable, load_order = _deferrable_settings(inputs, step)
    payload.update(deferrable)

    thermal = _thermal_settings(inputs, step, len(load_order))
    payload.update(thermal)

    # Profile-contributed settings last, so a profile can override a default
    # (a "no solar" profile turning PV modelling off, for instance).
    payload.update(inputs.extra_settings)

    for message in warnings:
        _LOGGER.warning("%s", message)

    return PayloadResult(payload=payload, warnings=warnings, load_order=load_order)


def _thermal_settings(inputs: PayloadInputs, step: timedelta, load_count: int) -> dict[str, Any]:
    """Describe any thermal loads to EMHASS.

    Sending ``def_load_config`` overwrites ``number_of_deferrable_loads`` with
    its own length, so it is built from the active load count with an empty
    entry for every ordinary load. A list containing only the thermal ones
    would silently drop the rest off the end of the optimisation.
    """
    active = [load for load in inputs.loads if load.enabled]
    thermal_by_index = {
        index: load.thermal for index, load in enumerate(active) if load.thermal is not None
    }

    config = build_def_load_config(
        thermal_by_index, load_count, inputs.now, step, inputs.horizon_steps
    )
    return {} if config is None else {"def_load_config": config}


def _battery_settings(battery: BatteryConfig) -> dict[str, Any]:
    if not battery.enabled:
        return {"set_use_battery": False}
    return {
        "set_use_battery": True,
        "battery_nominal_energy_capacity": battery.capacity_wh,
        "battery_charge_power_max": battery.charge_power_max_w,
        "battery_discharge_power_max": battery.discharge_power_max_w,
        "battery_minimum_state_of_charge": battery.soc_min,
        "battery_maximum_state_of_charge": battery.soc_max,
        "battery_target_state_of_charge": battery.soc_target,
        "battery_charge_efficiency": battery.charge_efficiency,
        "battery_discharge_efficiency": battery.discharge_efficiency,
    }


def _deferrable_settings(
    inputs: PayloadInputs, step: timedelta
) -> tuple[dict[str, Any], list[str]]:
    active = [load for load in inputs.loads if load.enabled]
    if not active:
        return {"number_of_deferrable_loads": 0}, []

    starts: list[int] = []
    ends: list[int] = []
    for load in active:
        start_index, end_index = window_to_timesteps(
            load.earliest_start,
            load.latest_end,
            inputs.now,
            step,
            inputs.horizon_steps,
        )
        starts.append(start_index)
        ends.append(end_index)

    settings: dict[str, Any] = {
        "number_of_deferrable_loads": len(active),
        "nominal_power_of_deferrable_loads": [load.nominal_power_w for load in active],
        "operating_hours_of_each_deferrable_load": [load.operating_hours for load in active],
        "start_timesteps_of_each_deferrable_load": starts,
        "end_timesteps_of_each_deferrable_load": ends,
        "treat_deferrable_load_as_semi_cont": [load.semi_continuous for load in active],
        "set_deferrable_load_single_constant": [load.single_constant for load in active],
        "set_deferrable_startup_penalty": [load.startup_penalty for load in active],
        # Feeding current state back stops EMHASS charging a startup penalty for
        # a load that is already running, and stops it re-scheduling work that
        # has already been done today.
        "def_current_state": [load.current_state for load in active],
        "def_current_operating_timesteps": [load.completed_timesteps for load in active],
    }

    if any(load.current_power_w for load in active):
        settings["def_current_power"] = [load.current_power_w for load in active]

    return settings, [load.subentry_id for load in active]

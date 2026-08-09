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

from .const import ACTION_MPC, DEFAULT_COST_FUN
from .models import (
    BatteryConfig,
    DeferrableLoad,
    DeferrableLoadGroup,
    GridConfig,
    HybridInverterConfig,
    Series,
)
from .thermal import build_def_load_config

_LOGGER = logging.getLogger(__name__)

_PRICE_KEYS = ("load_cost_forecast", "prod_price_forecast")

_POWER_KEYS = ("pv_power_forecast", "load_power_forecast")


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


def _resolve_wall_clock(
    earliest: time | None,
    latest: time | None,
    local_start: datetime,
) -> tuple[datetime | None, datetime | None]:
    """Project a recurring wall-clock window onto the absolute time axis.

    Returns ``(opens_at, closes_at)``, either of which is None when that end is
    unconstrained. ``opens_at`` is None for a window that is *already open* as
    well as for one with no start: at 23:00 inside a 22:00-06:00 window the
    load may run now, and pushing the start to tomorrow's 22:00 would waste the
    cheap overnight hours entirely.
    """
    if earliest is None and latest is None:
        return None, None

    active = in_window(local_start.time(), earliest, latest)
    if earliest is None or active:
        opens_at = None
        anchor = local_start
    else:
        opens_at = _next_occurrence(local_start, earliest)
        anchor = opens_at

    closes_at = None if latest is None else _next_occurrence(anchor, latest, strict=True)
    return opens_at, closes_at


@dataclass(slots=True)
class LoadWindow:
    """One load's start/end constraint, as EMHASS timestep indices."""

    start_index: int
    end_index: int
    warnings: list[str] = field(default_factory=list)
    opens_beyond_horizon: bool = False


def resolve_load_window(
    *,
    name: str,
    earliest: time | None,
    latest: time | None,
    deadline: datetime | None,
    grid_start: datetime,
    step: timedelta,
    horizon_steps: int,
    operating_steps: int = 0,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> LoadWindow:
    """Combine every kind of "when may this run" into timestep indices.

    Three constraints of quite different natures meet here:

    * a *wall-clock window*, standing config describing the appliance and the
      household ("never before 22:00"), which recurs and so has to be
      re-projected onto the absolute axis on every request,
    * an *absolute window* (``start_at``/``end_at``), derived for this one
      request from the shape of this one day -- a surplus load's sunny hours,
      which mean nothing tomorrow, and
    * a *deadline*, one request's own "finish within N hours", already
      absolute because it was anchored when the run was asked for.

    All may apply, and the answer is their intersection. Resolving each to
    absolute datetimes first and converting once at the end means the DST-safe
    arithmetic below only has to be right in one place.

    EMHASS interprets the result *relative to the moment the optimisation is
    launched*, not as wall-clock times, which is why none of this can be cached
    between runs. In its convention ``0`` means "unconstrained": a start of 0
    is "may begin immediately", an end of 0 "may run to the end of the
    horizon".
    """
    warnings: list[str] = []
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

    opens_at, closes_at = _resolve_wall_clock(earliest, latest, local_start)

    # A derived window narrows a standing one, never widens it: both are hard,
    # so the intersection is the only safe reading. Deliberately folded in
    # before the deadline logic below, so a deadline still measures itself
    # against whatever the two windows leave.
    opens_at_is_hard = False
    if start_at is not None and (opens_at is None or start_at > opens_at):
        opens_at = start_at
        opens_at_is_hard = True
    if end_at is not None and (closes_at is None or end_at < closes_at):
        closes_at = end_at

    ends_at = closes_at
    end_is_deadline = False
    if deadline is not None:
        if closes_at is None or deadline < closes_at:
            ends_at = deadline
            end_is_deadline = True

        # An explicit, dated request beats a standing preference: a deadline
        # that expires before the window even opens would otherwise leave the
        # load unable to run at all, and a request that silently does nothing
        # for hours is worse than one that breaks quiet hours audibly. But a
        # window opened by start_at is not a preference -- it is a surplus
        # load's "only while the sun is actually spare" (see
        # DeferrableLoad.start_at), and running outside it means importing
        # grid power, the one thing such a load must never do. So only the
        # soft, wall-clock case gets relaxed; a hard start stays put and the
        # deadline is simply missed, same as any other window too narrow to
        # meet it.
        if opens_at is not None and deadline <= opens_at and not opens_at_is_hard:
            local_opens_at = dt_util.as_local(opens_at)
            warnings.append(
                f"{name}: the requested deadline falls before its time window opens "
                f"({local_opens_at:%H:%M}); running as soon as possible instead."
            )
            opens_at = None
            ends_at = deadline
            end_is_deadline = True

    # Rounded, not ceiled. ``opens_at`` is almost always a clean boundary --
    # a wall-clock time, or a surplus window derived from plan rows on their
    # own grid -- while ``grid_start`` is the literal instant this run
    # happened to launch, essentially never exactly on that grid. Ceiling
    # treats *any* fractional remainder as a full step still to wait out, so
    # a run that starts mere seconds before its own window opens would be
    # told to wait a whole extra step regardless -- silently discarding
    # nearly all of a real, already-open timestep, every single run. Rounding
    # only pushes the start out when opens_at is genuinely closer to the next
    # step than this one, which is the case an index is coarse enough to
    # actually get wrong.
    start_index = 0 if opens_at is None else max(round(elapsed_steps(opens_at)), 0)
    opens_beyond_horizon = False
    if start_index >= horizon_steps:
        # EMHASS has no index meaning "not this cycle" -- 0 is "may begin
        # immediately", the opposite of what a window that hasn't opened yet
        # needs to say. Clamping to 0 would tell EMHASS the load is free to
        # run right now, e.g. scheduling a quiet-hours-only load mid-day on a
        # short-horizon MPC run that can't see as far as its window. The flag
        # lets the caller park the load's hours instead of trusting this index.
        warnings.append(
            f"{name}: its time window opens beyond the {horizon_steps}-step horizon, "
            f"so it will not be scheduled to run this cycle."
        )
        start_index = 0
        opens_beyond_horizon = True

    if ends_at is None:
        end_index = 0
    else:
        end_index = math.floor(elapsed_steps(ends_at))
        if end_index >= horizon_steps:
            # Beyond the horizon is the same as unconstrained, and saying so
            # keeps EMHASS from clamping a nonsensical index. A deadline that
            # starts out here becomes binding on its own as later runs advance
            # the horizon towards it.
            end_index = 0
        elif end_is_deadline and end_index - start_index < operating_steps:
            # The deadline can no longer be met -- either it has passed, or the
            # run no longer fits in what is left of it, which is the ordinary
            # outcome of an anchored deadline as the clock advances towards it.
            #
            # Both must be relaxed rather than sent as-is. Asking EMHASS to fit
            # a run into a window too small for it makes the *whole problem*
            # infeasible, taking the battery and every other load down with it;
            # and falling through to 0 would turn a missed deadline into
            # "whenever", the one answer the user definitely did not ask for.
            # Scheduling it as soon as it physically fits misses the deadline
            # by as little as possible, which is the nearest thing to what was
            # actually requested.
            soonest = min(start_index + max(operating_steps, 1), horizon_steps - 1)
            missed = (
                "has already passed"
                if end_index <= start_index
                else ("no longer leaves room for the full run")
            )
            warnings.append(
                f"{name}: its deadline {missed}; scheduling it at the earliest opportunity instead."
            )
            end_index = max(soonest, 0)

    if (
        not opens_beyond_horizon
        and operating_steps > 0
        and end_index > 0
        and not end_is_deadline
        and end_index - start_index < operating_steps
    ):
        # A standing time window too narrow for the run. Unlike a deadline this
        # is a configuration error rather than the passage of time, so it is
        # reported rather than silently overridden -- widening it here would
        # breach the user's quiet hours every night without them ever finding
        # out why. EMHASS reports it as an infeasible *problem* with no hint
        # which load caused it, so naming the load is the whole value here.
        # Skipped when the window opens beyond the horizon: the caller parks
        # the load's hours entirely in that case, so there is no run for this
        # window to be too narrow for.
        warnings.append(
            f"{name}: its allowed window is {end_index - start_index} timesteps but it "
            f"needs {operating_steps}; EMHASS may report the problem as infeasible."
        )

    return LoadWindow(start_index, max(end_index, 0), warnings, opens_beyond_horizon)


def window_to_timesteps(
    earliest: time | None,
    latest: time | None,
    grid_start: datetime,
    step: timedelta,
    horizon_steps: int,
) -> tuple[int, int]:
    """A wall-clock window alone, as timestep indices.

    The deadline-free case of :func:`resolve_load_window`, kept separate
    because it is the shape most of the window behaviour is specified in.
    """
    window = resolve_load_window(
        name="",
        earliest=earliest,
        latest=latest,
        deadline=None,
        grid_start=grid_start,
        step=step,
        horizon_steps=horizon_steps,
    )
    return window.start_index, window.end_index


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
    hybrid_inverter: HybridInverterConfig
    loads: list[DeferrableLoad] = field(default_factory=list)
    load_groups: list[DeferrableLoadGroup] = field(default_factory=list)
    pv: Series | None = None
    load: Series | None = None
    buy_price: Series | None = None
    sell_price: Series | None = None
    outdoor_temperature: Series | None = None
    soc_init: float | None = None
    soc_final: float | None = None
    """End-of-horizon SOC target chosen by terminal.decide_end_soc. None keeps
    the old pin-to-start behaviour (see build_payload)."""
    pv_live_w: float | None = None
    load_live_w: float | None = None
    grid_import_limit_w: float | None = None
    grid_export_limit_w: float | None = None
    """Live readings of the optional grid-limit sensors, already fetched by the
    coordinator. ``None`` means none is configured, or the one that is could
    not be read -- either way the static limit in ``grid`` is used unchanged.
    See ``resolve_grid_limit``."""
    mix_beta: float = 0.5
    cost_fun: str = DEFAULT_COST_FUN
    extra_settings: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PayloadResult:
    """A built payload plus anything the user should know about it."""

    payload: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    load_order: list[str] = field(default_factory=list)
    """Subentry ids in the order they map to EMHASS's ``P_deferrable{k}``."""


def resolve_grid_limit(
    live_w: float | None,
    ceiling_w: float,
    floor_w: float = 0.0,
) -> int:
    """The grid limit to send, given an optional live reading of it.

    The static number configured on the grid step is the physical rating of
    the connection, so a sensor may only ever lower it: a template with a
    wrong fuse size or a stale unit can then make the plan needlessly cautious
    but can never invite EMHASS to plan through a fuse. An absent reading
    means no sensor is configured or it could not be read, and leaves the
    static number untouched.

    ``floor_w`` guards the other end. EMHASS reports an infeasible problem
    rather than a small one when the limit falls below the load that has to be
    served regardless -- the household baseline is not deferrable, and no
    amount of battery is guaranteed to cover it -- so a limit derived from a
    momentary reading is not allowed to drop below what the horizon already
    says will be drawn. Clamped rather than rejected: a plan built against a
    slightly optimistic limit still beats no plan at all, and the caller
    reports the clamp.

    Whole watts go out: EMHASS's own config carries these as integers, so a
    ``9000.0`` in the runtime parameters is only noise in its log. Rounded
    down rather than to nearest, since the number is a fuse rating and the
    plan must never be invited past it.
    """
    if live_w is None:
        return math.floor(ceiling_w)
    return math.floor(min(ceiling_w, max(live_w, min(floor_w, ceiling_w))))


def _import_floor_w(inputs: PayloadInputs, horizon_end: datetime) -> float:
    """The lowest import limit that can still serve the baseline house load.

    Deferrable loads are deliberately not counted: they are what the limit is
    supposed to be able to squeeze out. What must fit is the non-deferrable
    forecast, taken as its peak over the horizon rather than its mean, since
    EMHASS's limit binds per timestep.

    Both sources are optional and neither is always present -- the load
    profiles that let EMHASS build its own forecast hand us no series at all,
    which is exactly when the live reading is the only thing there is to go on.
    """
    candidates = [0.0]
    if inputs.load:
        window = inputs.load.window(inputs.now, horizon_end)
        if window:
            candidates.append(max(window.values))
    if inputs.load_live_w is not None:
        candidates.append(inputs.load_live_w)
    return max(candidates)


def build_payload(inputs: PayloadInputs) -> PayloadResult:
    """Assemble the runtime parameters for one EMHASS request."""
    step = timedelta(minutes=inputs.time_step_minutes)
    horizon_end = inputs.now + step * inputs.horizon_steps
    warnings: list[str] = []

    payload: dict[str, Any] = {
        "optimization_time_step": inputs.time_step_minutes,
        "costfun": inputs.cost_fun,
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
    # Live values to blend into the current step of the matching forecast, MPC
    # only -- see models.Series.blend_at. A day-ahead run's first step is
    # tomorrow, not now, so there is nothing live to blend there.
    live_values = {
        "pv_power_forecast": inputs.pv_live_w,
        "load_power_forecast": inputs.load_live_w,
    }
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
        if key in _PRICE_KEYS:
            # Tariff math (multiplier/adder) leaves the same float noise on
            # a per-kWh price; round rather than lose currency precision.
            series = series.map_values(lambda v: round(v, 4))
        if inputs.action == ACTION_MPC and (live_value := live_values.get(key)) is not None:
            series = series.blend_at(inputs.now, live_value, inputs.mix_beta)
        if key in _PRICE_KEYS and not series.covers(horizon_end):
            # A day-ahead price source (e.g. Nord Pool before its ~13:00
            # publication) often does not yet cover tomorrow. Left alone,
            # EMHASS's own fallback holds the last known price flat for the
            # rest of the horizon -- which for a series ending at local
            # midnight is usually one of the cheapest hours of the day (last
            # night), flat-lined across all of tomorrow. Repeating
            # yesterday's shape is a far better guess.
            short_until = series.end
            series = series.extended_with_previous_day(horizon_end)
            if series.covers(horizon_end):
                warnings.append(
                    f"{label} only covered until "
                    f"{dt_util.as_local(short_until):%Y-%m-%d %H:%M}; the "
                    "remainder was filled in by repeating the previous day's "
                    "prices."
                )
        values = series.to_payload()
        if key in _POWER_KEYS:
            # Profile math (efficiency factors, unit conversions) and the live
            # blend above both leave long float tails like 1575.2129175703624 W.
            # Sub-watt precision is meaningless in a forecast and only bloats
            # the request and EMHASS's log, so whole watts go out. Done here
            # rather than on the Series, whose constructor coerces every value
            # back to float, and after the blend so the blended step is rounded
            # too.
            values = {when: round(value) for when, value in values.items()}
        payload[key] = values
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

    if inputs.action == ACTION_MPC:
        # The load side of this is not the blend_at() call above -- none of
        # the built-in load profiles other than "Load forecast from an
        # entity" and "Native EMHASS forecaster" hand the companion a series
        # to blend into, since the point of "House load sensor" is to let
        # EMHASS compute the forecast itself (typical/naive/mlforecaster).
        # But EMHASS's own naive-mpc-optim already fetches that sensor's live
        # reading and blends it into its own first forecast step whenever the
        # load method isn't "list" -- unconditionally, on every MPC run,
        # whether or not a companion live-value profile is even configured.
        # Left unset, alpha/beta there default to a hard-coded 50/50 split
        # inside EMHASS, entirely disconnected from number.MixBetaNumber.
        # Pinning them here to the same weight makes that automatic
        # correction mean what the slider says. Harmless for the profiles
        # that do supply their own list (EMHASS's own correction is skipped
        # there, since both series are already "list" methods) and inert for
        # a day-ahead run (EMHASS never applies it outside naive-mpc-optim).
        payload["alpha"] = round(1 - inputs.mix_beta, 4)
        payload["beta"] = round(inputs.mix_beta, 4)

    if inputs.soc_init is not None:
        # A fraction in [0, 1]; the percentage form belongs only to display.
        payload["soc_init"] = round(inputs.soc_init, 4)
        # EMHASS pins net battery throughput over the horizon to
        # (soc_init - soc_final) -- a hard equality up to 0.17.x, softly
        # penalised far above the dearest import slot from 0.18, so the target
        # is honoured whenever physics allows either way. The value comes from
        # terminal.decide_end_soc; without one, fall back to pinning the end
        # to the start. The fallback is sent explicitly rather than left to
        # EMHASS's own soc_final default, since a default change on EMHASS's
        # side would otherwise change plans here without warning.
        if inputs.soc_final is not None:
            payload["soc_final"] = round(inputs.soc_final, 4)
        else:
            payload["soc_final"] = payload["soc_init"]

    # -- settings -------------------------------------------------------------
    payload.update(_battery_settings(inputs.battery))
    payload.update(_hybrid_inverter_settings(inputs.hybrid_inverter))
    import_floor = _import_floor_w(inputs, horizon_end)
    payload["maximum_power_from_grid"] = resolve_grid_limit(
        inputs.grid_import_limit_w, inputs.grid.import_max_w, import_floor
    )
    # Export gets the same override with no floor of its own: nothing has to
    # leave the property the way the house load has to be served, so a low
    # limit only costs revenue -- except with EMHASS's own curtailment off,
    # where surplus PV has nowhere else to go. That case is documented rather
    # than guessed at, since the surplus depends on a PV forecast the limit
    # sensor knows nothing about.
    payload["maximum_power_to_grid"] = resolve_grid_limit(
        inputs.grid_export_limit_w, inputs.grid.export_max_w
    )
    if (
        inputs.grid_import_limit_w is not None
        and inputs.grid_import_limit_w < import_floor <= inputs.grid.import_max_w
    ):
        warnings.append(
            f"The grid import limit sensor read "
            f"{inputs.grid_import_limit_w:.0f} W, below the "
            f"{import_floor:.0f} W the house is forecast to draw anyway. Raised "
            "to that value, since a lower limit has no feasible plan."
        )
    # Demand charge on the horizon's peak import. Sent unconditionally rather
    # than from _battery_settings: EMHASS prices it off the grid variable, so
    # it must keep working for a plant with no battery at all, and that helper
    # returns early when the battery is off.
    payload["capacity_cost_per_kw"] = inputs.grid.capacity_cost_per_kw
    # EMHASS's own PV curtailment, a plant_conf parameter reachable through
    # runtimeparams via its associations.csv. With it off there is no
    # `P_PV_curtailment` column at all, so strategy.decide_curtailment's
    # primary rule has nothing to act on. Omitted entirely when unset rather
    # than sent as False: an entry saved before this setting existed must not
    # silently override whatever the add-on already has configured.
    if inputs.grid.compute_curtailment is not None:
        payload["compute_curtailment"] = inputs.grid.compute_curtailment

    deferrable, load_order, deferrable_warnings = _deferrable_settings(inputs, step)
    payload.update(deferrable)
    warnings.extend(deferrable_warnings)

    group_settings, group_warnings = _deferrable_load_group_settings(inputs.load_groups, load_order)
    payload.update(group_settings)
    warnings.extend(group_warnings)

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
    its own length, so it is built from the full load count with an empty entry
    for every ordinary load. A list containing only the thermal ones would
    silently drop the rest off the end of the optimisation.

    A parked load gets an empty entry even if it is thermal: its temperature
    targets are exactly the kind of demand that would contradict the zero
    operating hours it is being sent with.
    """
    thermal_by_index = {
        index: load.thermal
        for index, load in enumerate(inputs.loads)
        if load.thermal is not None and load.wants_to_run
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
        # Cycle cost per kWh of throughput, charged against the objective
        # EMHASS maximises. Both default to 0.0 -- EMHASS's own default -- so
        # an untouched config sends the same numbers it always did.
        "weight_battery_discharge": battery.weight_battery_discharge,
        "weight_battery_charge": battery.weight_battery_charge,
        # Dwell penalties on the SOC level. EMHASS only builds each constraint
        # when its cost and threshold are both above zero, so the thresholds
        # ride along unconditionally and simply sit inert at cost 0.0.
        "battery_soc_deficit_threshold": battery.soc_deficit_threshold,
        "battery_soc_deficit_cost": battery.soc_deficit_cost,
        "battery_soc_surplus_threshold": battery.soc_surplus_threshold,
        "battery_soc_surplus_cost": battery.soc_surplus_cost,
        # Quadratic C-rate penalty, gated the same way on stress_cost > 0.
        "battery_stress_cost": battery.stress_cost,
        "battery_stress_segments": battery.stress_segments,
    }


def _hybrid_inverter_settings(hybrid: HybridInverterConfig) -> dict[str, Any]:
    """Describe a shared-inverter plant's AC-side throughput to EMHASS.

    Always sent, on or off: EMHASS's own ``inverter_is_hybrid`` gates whether
    any of the rest is used at all, so leaving it disabled makes the other
    values inert rather than wrong -- but the flag itself must be asserted on
    every request the same way every other setting here is, so a value left
    over in EMHASS's own persisted config is never what decides the answer.
    """
    if not hybrid.enabled:
        return {"inverter_is_hybrid": False}
    return {
        "inverter_is_hybrid": True,
        "inverter_ac_output_max": hybrid.ac_output_max_w,
        "inverter_ac_input_max": hybrid.ac_input_max_w,
        "inverter_efficiency_dc_ac": hybrid.efficiency_dc_ac,
        "inverter_efficiency_ac_dc": hybrid.efficiency_ac_dc,
    }


def operating_timesteps(operating_hours: float, step_minutes: int) -> int:
    """The whole number of timesteps a requested run time corresponds to.

    EMHASS turns operating hours into an *energy equality*:
    ``sum(p) * time_step == nominal_power * operating_hours``. A
    semi-continuous load can only draw 0 or exactly its nominal power, so the
    only energies it can reach are whole multiples of
    ``nominal_power * time_step`` -- a request that is not a whole number of
    timesteps has no solution at all, and EMHASS reports an infeasible problem
    (or silently falls back to its relaxed LP) rather than rounding for us.

    Rounding is to the nearest step, except that any non-zero request is
    floored at one step: "run this for six minutes" turning into "never run" is
    a worse answer than a slightly longer run.
    """
    if operating_hours <= 0:
        return 0
    return max(1, round(operating_hours * 60 / step_minutes))


@dataclass(slots=True)
class _LoadSettings:
    """One load's row across every per-load array EMHASS expects."""

    nominal_power_w: float
    minimum_power_w: float
    hours: float
    timesteps: int
    start_index: int
    end_index: int
    semi_continuous: bool
    single_constant: bool
    startup_penalty: float
    max_startups: int
    on_time_steps: int
    off_time_steps: int
    current_state: bool
    current_power_w: float
    completed_timesteps: int
    current_on_timesteps: int
    current_off_timesteps: int


def _park(load: DeferrableLoad) -> _LoadSettings:
    """Describe a load that must not run, without removing it from the run.

    Leaving a load out is what used to happen, and it made every *other*
    load's ``P_deferrable{k}`` number shift underneath it as loads were
    disabled or requests came and went -- so no automation or dashboard could
    refer to "deferrable 2" and mean anything durable. Sending it parked keeps
    the numbering a fixed property of the system.

    Everything that could make a zero-hour load do something is neutralised
    rather than passed through. In particular the current state is reported as
    off and its completed timesteps as zero however the appliance is actually
    behaving: telling EMHASS that a load is running, or has already run, while
    also demanding it total zero energy is a contradiction, and EMHASS answers
    a contradiction by declaring the whole problem infeasible -- taking every
    other load and the battery down with it.

    The nominal power is kept truthful because EMHASS wants a positive power
    per load, and a parked load reaches zero energy through its run time, not
    through its rating.
    """
    return _LoadSettings(
        nominal_power_w=load.nominal_power_w,
        minimum_power_w=0.0,
        hours=0.0,
        timesteps=0,
        start_index=0,
        end_index=0,
        semi_continuous=True,
        single_constant=False,
        startup_penalty=0.0,
        max_startups=0,
        on_time_steps=0,
        off_time_steps=0,
        current_state=False,
        current_power_w=0.0,
        completed_timesteps=0,
        current_on_timesteps=0,
        current_off_timesteps=0,
    )


def _describe(
    load: DeferrableLoad, inputs: PayloadInputs, step: timedelta, warnings: list[str]
) -> _LoadSettings:
    """Describe a load that is asking for run time."""
    # Quantise here rather than in the entity: the number the user typed is
    # theirs to see, and the timestep it has to fit is only known when a
    # request is built.
    steps = operating_timesteps(load.operating_hours, inputs.time_step_minutes)
    quantised = steps * inputs.time_step_minutes / 60
    if not math.isclose(quantised, load.operating_hours, rel_tol=1e-9, abs_tol=1e-9):
        warnings.append(
            f"{load.name}: {load.operating_hours:g} h is not a whole number of "
            f"{inputs.time_step_minutes}-minute timesteps, so EMHASS is being "
            f"asked for {quantised:g} h ({steps} timesteps) instead."
        )

    # What actually still has to fit in the window is the run *remaining*,
    # not the full target: def_current_operating_timesteps already tells
    # EMHASS to credit completed_timesteps against the energy/timestep
    # requirement internally, and the window has to shrink the same way or
    # the two disagree. Left at the full `steps`, a load nearing the end of
    # its target keeps failing the "does it fit" check and the deadline
    # recovery below as if nothing had run yet -- forcing a fresh full-width
    # window pinned to "now" on every request instead of settling into
    # whatever's actually left.
    remaining_steps = max(steps - load.completed_timesteps, 0)

    # After the quantisation above, so the feasibility check inside knows
    # how many timesteps actually have to fit in the window it produces.
    window = resolve_load_window(
        name=load.name,
        earliest=load.earliest_start,
        latest=load.latest_end,
        deadline=load.deadline_at,
        grid_start=inputs.now,
        step=step,
        horizon_steps=inputs.horizon_steps,
        operating_steps=remaining_steps,
        start_at=load.start_at,
        end_at=load.end_at,
    )
    warnings.extend(window.warnings)

    # EMHASS's own sentinel for "unconstrained start" is 0, the same index a
    # beyond-horizon window gets clamped to -- so passing hours through here
    # would tell EMHASS the load may start right now, which is exactly what
    # its window (quiet hours, a surplus block, ...) says it may not do yet.
    # Zeroing the target is the only way to say "not this cycle": the window
    # comes back around and asks properly once the horizon reaches it.
    if window.opens_beyond_horizon:
        quantised = 0.0
        steps = 0

    # A single-constant load that is already running gets *pinned* by EMHASS
    # the moment it has any operating requirement at all: an unbroken block
    # can't be split, so EMHASS keeps a currently-on single-constant load
    # running from t=0 for its full requested duration regardless of
    # start_timestep, widening the window mask to make room for it. That is
    # correct when the window still covers "now" -- the pin just continues
    # the block the window already opened. It is wrong when the window has
    # moved past now (this load's own surplus block ended and a later,
    # unrelated one is what got computed): the load would be pinned to a
    # fresh multi-hour run it has no business starting immediately. Asking
    # for nothing this cycle avoids the pin entirely and leaves the load free
    # to turn off; the later block gets asked for normally, on its own
    # cycle, once "now" actually reaches it and current_state has caught up.
    if load.current_state and load.single_constant and window.start_index > 0:
        warnings.append(
            f"{load.name}: still running from an earlier decision, but its window "
            f"now starts at step {window.start_index}; asking for 0 hours this run "
            f"instead of pinning it to the later block."
        )
        quantised = 0.0
        steps = 0

    # A floor above the ceiling has no feasible power at all, and EMHASS
    # reports that as an infeasible problem with no hint as to which load
    # caused it.
    minimum_power_w = load.minimum_power_w
    if load.minimum_power_w > load.nominal_power_w:
        warnings.append(
            f"{load.name}: minimum power {load.minimum_power_w:g} W exceeds its "
            f"nominal {load.nominal_power_w:g} W; using the nominal power as the floor."
        )
        minimum_power_w = load.nominal_power_w

    return _LoadSettings(
        nominal_power_w=load.nominal_power_w,
        minimum_power_w=minimum_power_w,
        hours=quantised,
        timesteps=steps,
        start_index=window.start_index,
        end_index=window.end_index,
        semi_continuous=load.semi_continuous,
        single_constant=load.single_constant,
        startup_penalty=load.startup_penalty,
        max_startups=load.max_startups,
        on_time_steps=operating_timesteps(
            load.minimum_on_time_minutes / 60, inputs.time_step_minutes
        ),
        off_time_steps=operating_timesteps(
            load.minimum_off_time_minutes / 60, inputs.time_step_minutes
        ),
        current_state=load.current_state,
        current_power_w=load.current_power_w,
        completed_timesteps=load.completed_timesteps,
        current_on_timesteps=load.current_on_timesteps,
        current_off_timesteps=load.current_off_timesteps,
    )


def _deferrable_settings(
    inputs: PayloadInputs, step: timedelta
) -> tuple[dict[str, Any], list[str], list[str]]:
    # Every configured load, always, in the registry's stable order: a load's
    # deferrable number is meant to be as durable as the load itself.
    loads = list(inputs.loads)
    if not loads:
        return {"number_of_deferrable_loads": 0}, [], []

    warnings: list[str] = []
    described = [
        _describe(load, inputs, step, warnings) if load.wants_to_run else _park(load)
        for load in loads
    ]

    settings: dict[str, Any] = {
        "number_of_deferrable_loads": len(described),
        "nominal_power_of_deferrable_loads": [row.nominal_power_w for row in described],
        "minimum_power_of_deferrable_loads": [row.minimum_power_w for row in described],
        "operating_hours_of_each_deferrable_load": [row.hours for row in described],
        "start_timesteps_of_each_deferrable_load": [row.start_index for row in described],
        "end_timesteps_of_each_deferrable_load": [row.end_index for row in described],
        "treat_deferrable_load_as_semi_cont": [row.semi_continuous for row in described],
        "set_deferrable_load_single_constant": [row.single_constant for row in described],
        "set_deferrable_startup_penalty": [row.startup_penalty for row in described],
        "set_deferrable_max_startups": [row.max_startups for row in described],
        # Minimum dwell time once a load switches on/off, protecting
        # compressor-driven loads from short-cycling. Paired with the current
        # on/off streak below so EMHASS can enforce it correctly across
        # restarts rather than assuming every load just switched.
        "def_minimum_on_time": [row.on_time_steps for row in described],
        "def_minimum_off_time": [row.off_time_steps for row in described],
        # Feeding current state back stops EMHASS charging a startup penalty for
        # a load that is already running, and stops it re-scheduling work that
        # has already been done today.
        "def_current_state": [row.current_state for row in described],
        "def_current_operating_timesteps": [row.completed_timesteps for row in described],
        "def_current_on_timesteps": [row.current_on_timesteps for row in described],
        "def_current_off_timesteps": [row.current_off_timesteps for row in described],
        # Companion has no per-load "max acceptable cost" feature of its own, so
        # this is always disabled -- but it must still be sent, sized to the
        # active load count. EMHASS's own optimisation.py indexes this list
        # directly with no bounds check (unlike every sibling array above,
        # which all have a `k < len(...)` guard), so if this is left to the
        # persisted config instead, a stale shorter list left over from a
        # previous, larger load count throws an uncaught IndexError and 500s
        # the entire request. A zero disables the feature exactly as the
        # key's absence would, but immune to whatever is on disk.
        "deferrable_load_max_cost": [0.0 for _ in described],
    }

    if any(row.current_power_w for row in described):
        settings["def_current_power"] = [row.current_power_w for row in described]

    if inputs.action == ACTION_MPC:
        # Exact integers, with no hours-to-timesteps division for EMHASS to do
        # and no float remainder to survive. EMHASS honours this key only on the
        # branch that takes `prediction_horizon` (utils.treat_runtimeparams) --
        # it is deliberately absent from associations.csv -- so the day-ahead
        # request has to keep making do with the quantised hours above.
        settings["operating_timesteps_of_each_deferrable_load"] = [
            row.timesteps for row in described
        ]

    return settings, [load.subentry_id for load in loads], warnings


def _deferrable_load_group_settings(
    load_groups: list[DeferrableLoadGroup], load_order: list[str]
) -> tuple[dict[str, Any], list[str]]:
    """Describe cross-load relationships (a shared budget or mutual exclusion).

    Each group's members are resolved from subentry ids into the
    ``"deferrable{k}"`` name EMHASS expects, using their position in
    ``load_order``. A member no longer present there (the load was deleted or
    renamed since the group was created) is dropped with a warning rather than
    failing the whole request; a group left with fewer than two valid members
    is meaningless and dropped entirely.
    """
    warnings: list[str] = []
    groups: list[dict[str, Any]] = []

    for group in load_groups:
        names: list[str] = []
        for subentry_id in group.load_subentry_ids:
            if subentry_id not in load_order:
                warnings.append(
                    f"Load group {group.name!r}: a referenced load no longer "
                    "exists, dropping it from the group."
                )
                continue
            names.append(f"deferrable{load_order.index(subentry_id)}")

        if len(names) < 2:
            warnings.append(
                f"Load group {group.name!r}: fewer than 2 valid loads remain, "
                "dropping the group entirely."
            )
            continue

        entry: dict[str, Any] = {"names": names, "mutual_exclusion": group.mutual_exclusion}
        if group.max_power_w is not None:
            entry["max_power"] = group.max_power_w
        groups.append(entry)

    if not groups:
        return {}, warnings
    return {"deferrable_load_groups": groups}, warnings

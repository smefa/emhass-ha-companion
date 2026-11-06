"""Build the EMHASS runtime-parameter payload.

Companion-owned static settings come from the shared resolver. Live inputs and
load progress are added here; backend connections and saved ML models remain
backend-owned prerequisites.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
import logging
import math
from typing import Any

from homeassistant.util import dt as dt_util

from .const import (
    ACTION_MPC,
    BATTERY_LOCKOUT_PRICE_FACTOR,
    BATTERY_LOCKOUT_PRICE_FLOOR,
    BATTERY_LOCKOUT_SELF_CONSUMPTION_BIGM,
    COST_FUN_SELF_CONSUMPTION,
    DEFAULT_COST_FUN,
)
from .effective_settings import resolve_effective_settings
from .models import (
    ROW_ZERO_NEAREST,
    BatteryConfig,
    DeferrableLoad,
    DeferrableLoadGroup,
    GridConfig,
    HybridInverterConfig,
    Series,
    row_zero_start,
)
from .thermal import build_def_load_config
from .water_tank import TankShortfall, WaterTankConfig, _resolve_endpoint

_LOGGER = logging.getLogger(__name__)

# Messages that describe normal operation (a day-ahead price not yet published,
# a duration that does not fit the timestep). They stay in the payload warnings
# for the dashboard and diagnostics, but are not log-worthy as warnings.
_EXPECTED_WARNING_MARKERS = (
    " only covered until ",
    " is not a whole number of ",
)

# The previous build's messages, so an unchanged message is logged once rather
# than on every run.
_last_logged: set[str] = set()


def _log_warnings(warnings: list[str]) -> None:
    global _last_logged
    current = set(warnings)
    for message in warnings:
        if message in _last_logged:
            continue
        if any(marker in message for marker in _EXPECTED_WARNING_MARKERS):
            _LOGGER.info("%s", message)
        else:
            _LOGGER.warning("%s", message)
    _last_logged = current


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


def window_occurrences(
    earliest: time | None,
    latest: time | None,
    grid_start: datetime,
    step: timedelta,
    horizon_steps: int,
    minimum_off: timedelta = timedelta(0),
) -> list[tuple[int, int]]:
    """Every full recurrence of a daily window that fits the horizon.

    Returns ``(start_index, end_index)`` pairs. The first is exactly what
    :func:`window_to_timesteps` gives (clipped when already open, end 0 when
    unconstrained). Later ones are whole windows with an exclusive end index
    inside the horizon; the caller clamps an end on the horizon edge to 0 as it
    does for the first. A partly covered window is left out and picked up by a
    later run once the horizon reaches it.

    Each later window is built from the previous *canonical* close, not by
    shifting the first (which may have been clipped to "now"). Wall-clock times
    that do not exist on a DST night resolve through the water-tank endpoint
    policy. Only a window with both ends set and a gap to the next at least
    ``minimum_off`` repeats; anything else gives the single first window.
    """
    first = resolve_load_window(
        name="",
        earliest=earliest,
        latest=latest,
        deadline=None,
        grid_start=grid_start,
        step=step,
        horizon_steps=horizon_steps,
    )
    result = [(first.start_index, first.end_index)]
    if earliest is None or latest is None or earliest == latest:
        return result

    local_start = dt_util.as_local(grid_start)
    timezone = local_start.tzinfo
    origin = local_start.astimezone(UTC)
    step_seconds = step.total_seconds()

    def elapsed_steps(moment: datetime) -> float:
        return (moment - origin).total_seconds() / step_seconds

    _, canonical_close = _resolve_wall_clock(earliest, latest, local_start)
    if canonical_close is None:
        return result
    previous_close = canonical_close.astimezone(UTC)
    previous_end_index = math.floor(elapsed_steps(previous_close))
    off_steps = math.ceil(minimum_off / step)
    close_wall = canonical_close.astimezone(timezone).replace(tzinfo=None)

    # Each pass advances at least a day of wall time, so this bounds the loop
    # even if the horizon is huge or every occurrence is skipped.
    for _ in range(horizon_steps + 2):
        opens_wall = datetime.combine(close_wall.date(), earliest)
        if opens_wall < close_wall:
            opens_wall += timedelta(days=1)
        closes_wall = datetime.combine(opens_wall.date(), latest)
        while closes_wall <= opens_wall:
            closes_wall += timedelta(days=1)

        opens = _resolve_endpoint(opens_wall, timezone)
        closes = _resolve_endpoint(closes_wall, timezone)
        close_wall = closes_wall

        start_index = round(elapsed_steps(opens))
        if start_index >= horizon_steps:
            break
        # Rounded to the grid the two windows can touch even when the wall
        # clock leaves a gap, so the off time is checked in steps as well.
        if (
            closes <= opens
            or opens - previous_close < minimum_off
            or start_index - previous_end_index < off_steps
        ):
            _LOGGER.debug("Skipping window occurrence %s-%s", opens_wall, closes_wall)
            if closes > opens:
                break
            continue
        previous_close = closes
        previous_end_index = math.floor(elapsed_steps(closes))

        end_index = math.floor(elapsed_steps(closes))
        if start_index < 0 or end_index > horizon_steps:
            break
        if end_index <= start_index:
            continue
        result.append((start_index, end_index))
    return result


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
    use_pv: bool | None = None
    """Configured PV enablement; standalone callers may infer it from their series."""
    load: Series | None = None
    buy_price: Series | None = None
    sell_price: Series | None = None
    price_source_end: datetime | None = None
    """How far the real price source reached, when ``buy_price`` was stretched
    past it with a forecast. None means ``buy_price.end`` is the source's end."""
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
    flat_demand_charge_per_kw: float = 0.0
    """The manual network option's single rate, sent as ``capacity_cost_per_kw``
    across the whole horizon (no window, no memory) whenever no real demand
    charge is configured."""
    network_demand_charge_configured: bool = False
    """Whether the selected network profile defines a ``demand_charge`` at
    all -- set independently of whether it can currently be priced, so that a
    windowed tariff on a backend without the window mask still zeroes
    ``capacity_cost_per_kw`` here rather than falling back to the unrelated
    manual number (see coordinator.demand_charge_pricing)."""
    demand_charge_rate_per_kw: float | list[float] | None = None
    """The effective ``capacity_cost_per_kw`` -- already converted from the
    tariff sheet's rate by ``peaks.effective_rate_per_kw`` -- or None when the
    network profile's demand charge cannot be safely priced right now
    (backend too old, or a real window on a backend with no window mask).
    A list of two or more rates selects EMHASS K>1 (0.18.3+); a scalar (or
    single-element list) stays K=1."""
    current_period_peak_w: float | list[float] | None = None
    """``PeakTracker.floor_kw`` in watts, MPC only -- see
    docs/network_tariffs_plan.md, "The incurred-peak floor". List of K when
    multi-component."""
    capacity_interval_timesteps: int | list[int] = 1
    """N for ``capacity_charge_interval_timesteps`` -- the tariff's own
    measurement interval in optimizer timesteps, from
    ``DemandChargePricing.interval_timesteps``. Sent only when > 1; see
    planning/capacity_interval_plan.md. List of K when components differ."""
    capacity_interval_history_w: list[float] | list[list[float]] | None = None
    """``capacity_charge_current_interval_history`` -- the still-open
    interval's elapsed timesteps in watts, oldest to newest, from
    ``EmhassCoordinator._capacity_interval_history_w``. Only meaningful
    alongside :attr:`capacity_interval_timesteps` > 1. List-of-lists when
    K>1."""
    capacity_limit_w: float | None = None
    """An explicit ``capacity_limit:`` block's ceiling
    (``subscribed_kw - headroom_kw``, in watts). None when the profile
    defines none. Composes with (can only lower) the scalar import limit
    inside ``capacity_limit_window`` -- see docs/network_tariffs_plan.md,
    "Mechanism 3"."""
    capacity_limit_window: Callable[[datetime], bool] | None = None
    """Predicate for :attr:`capacity_limit_w`'s own window. Always set
    together with it."""
    demand_fallback_ceiling_w: float | None = None
    """``number.*_peak_target`` in watts -- the ceiling enforced, inside the
    demand charge's own window, whenever ``network_demand_charge_configured``
    is true but :attr:`demand_charge_rate_per_kw` is None (the peak cannot be
    safely priced right now). This is the v0.18.0 fallback: a windowed demand
    charge enforced as a ceiling rather than a price."""
    demand_window: Callable[[datetime], bool] | None = None
    """Predicate for the demand charge's own window, consulted only when
    :attr:`demand_fallback_ceiling_w` is in effect. For K>1 this is the
    union of every component window."""
    demand_charge_window: Callable[[datetime], bool] | list[Callable[[datetime], bool]] | None = (
        None
    )
    """Predicate for the demand charge's own window, consulted only when the
    peak *is* being priced (:attr:`demand_charge_rate_per_kw` is set) and the
    coordinator has already confirmed the backend can mask it -- see
    docs/network_tariffs_plan.md, "The window mask". Builds
    ``capacity_charge_window``, MPC only, same restriction as
    ``current_period_peak``. List of K predicates when multi-component."""
    send_charge_power_derating: bool = True
    """Whether this backend accepts ``battery_charge_power_derating``
    (EMHASS 0.18.3+). The table still lives on :attr:`battery`; this only
    gates sending it. Unknown/older backends keep the flat charge-power max."""
    send_soc_final_reward: bool = True
    """Whether this backend accepts ``battery_soc_final_reward_factor``
    (EMHASS 0.18.5+). The factor still lives on :attr:`battery`; this only
    gates sending it. Unknown/older backends keep the two-sided soft target."""
    row_zero_mode: str = ROW_ZERO_NEAREST
    """Rounding asserted through ``method_ts_round`` and used by every array."""
    backend_version: str | None = None
    num_lags: int | None = None

    def row_zero(self, step: timedelta) -> datetime:
        """The timestamp EMHASS will give this request's first plan row."""
        return row_zero_start(self.now, step, self.row_zero_mode)


@dataclass(frozen=True, slots=True)
class ExtraRow:
    """An internal EMHASS row for a later occurrence of a daily load's window."""

    parent: int
    """Index of the real load (in ``load_order``) this row belongs to."""
    name: str
    """For diagnostics, e.g. ``"Dishwasher (day 2)"``."""


@dataclass(slots=True)
class PayloadResult:
    """A built payload plus anything the user should know about it."""

    payload: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    load_order: list[str] = field(default_factory=list)
    """Subentry ids in the order they map to EMHASS's ``P_deferrable{k}``."""
    extra_rows: list[ExtraRow] = field(default_factory=list)
    """Internal rows appended after the real loads, one per further daily window.

    Row ``len(load_order) + i`` is ``extra_rows[i]``; folding its plan column
    into ``load_order[extra_rows[i].parent]`` restores the real numbering.
    """
    defaulted_forecast_methods: frozenset[str] = frozenset()
    tank_shortfalls: dict[str, TankShortfall] = field(default_factory=dict)
    """Hot-water tanks (by subentry id) whose floor was lowered to what they can reach."""


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


def build_capacity_limit_array(
    *,
    grid_scalar_w: int,
    ceilings: Sequence[tuple[Callable[[datetime], bool], float]],
    grid_start: datetime,
    step: timedelta,
    count: int,
    load: Series | None,
) -> tuple[list[int], list[str]]:
    """The per-timestep import ceiling a network profile's ``capacity_limit``
    and/or demand-window fallback impose.

    Every element starts at ``grid_scalar_w`` -- the ordinary scalar limit,
    itself already the tighter of the connection rating, a live sensor
    reading and the horizon's forecast peak (see ``resolve_grid_limit``) --
    and is lowered further by whichever of ``ceilings`` windows covers that
    timestep. Composition can therefore only ever lower the scalar, never
    raise it, keeping grid_limits.md's rule intact across every source.

    Each timestep a ceiling applies to is then floored at *that timestep's
    own* forecast load, not the horizon's peak: a per-timestep cap that dips
    below one hour's own forecast draw makes the whole run infeasible, and
    EMHASS answers infeasible for the entire problem, not for that hour. See
    docs/network_tariffs_plan.md, "Mechanism 3".
    """
    values: list[int] = []
    raised_at: list[datetime] = []
    for index in range(count):
        start = grid_start + step * index
        limit = float(grid_scalar_w)
        active = False
        for in_window, ceiling_w in ceilings:
            if in_window(start):
                active = True
                limit = min(limit, ceiling_w)
        if active:
            floor_w = (load.value_at(start) or 0.0) if load else 0.0
            if floor_w > limit:
                raised_at.append(start)
                limit = floor_w
        values.append(math.floor(limit))
    warnings: list[str] = []
    if raised_at:
        warnings.append(
            f"The network tariff's capacity limit had to be raised above its target at "
            f"{len(raised_at)} timestep(s), between "
            f"{dt_util.as_local(raised_at[0]):%Y-%m-%d %H:%M} and "
            f"{dt_util.as_local(raised_at[-1]):%Y-%m-%d %H:%M}, to stay above the forecast load -- "
            "a plan cannot be built below what the house is expected to draw."
        )
    return values, warnings


def build_capacity_charge_window_mask(
    *,
    in_window: Callable[[datetime], bool],
    start: datetime,
    step: timedelta,
    count: int,
) -> list[float]:
    """The ``capacity_charge_window`` mask: ``1.0`` where a timestep's local
    start falls inside the demand charge's own window, ``0.0`` elsewhere.

    Same alignment as ``load_cost_forecast`` -- ``start`` must already be
    floored onto the timestep grid, as ``build_capacity_limit_array``'s
    ``grid_start`` is -- and always exactly ``count`` long: EMHASS falls back
    to an unmasked (all-ones) peak with only a warning on a short or invalid
    vector, which would silently reintroduce the over-shaving this mask
    exists to prevent.
    """
    return [1.0 if in_window(start + step * index) else 0.0 for index in range(count)]


def _battery_lockout_weights(
    inputs: PayloadInputs,
    grid_start: datetime,
    horizon_end: datetime,
    step: timedelta,
    count: int,
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Per-timestep ``weight_battery_discharge``, pricing every flagged load's window.

    See planning/battery_lockaout_plan.md. Returns ``{}`` unless the battery is
    enabled and some load actually has a window to price -- so an install
    that never uses the feature (or has the battery off) sees the plain
    scalar weight it always has, byte-identical.

    Every element of a flagged load's ``battery_lockout_windows`` counts
    independently -- a held window and a live while_running window can be
    disjoint (see :class:`models.DeferrableLoad`), and this union is a
    per-timestep OR across all of them, across all loads, never a merged span.

    The price is derived from this run's own buy price rather than fixed: a
    hardcoded number would be wrong at a different currency scale, and the
    floor covers a missing or all-zero price series. Windowed to the horizon
    rather than the whole series, which can run well past it. See Test C in
    the plan for where the real break-even sits against a tariff spread -- the
    factor leaves roughly 20x headroom over it under profit/cost.

    Under ``self-consumption`` EMHASS's objective multiplies grid import by a
    hardcoded bigM of 1e3 while applying ``weight_battery_discharge`` without
    that markup, so the lockout price is scaled by the same bigM or the
    battery still wins against grid inside the window.
    """
    windows = [window for load in inputs.loads for window in load.battery_lockout_windows]
    if not settings["set_use_battery"] or not windows:
        return {}
    horizon_prices = inputs.buy_price.window(grid_start, horizon_end) if inputs.buy_price else None
    max_buy_price = max(horizon_prices.values, default=0.0) if horizon_prices else 0.0
    price = max(BATTERY_LOCKOUT_PRICE_FACTOR * max_buy_price, BATTERY_LOCKOUT_PRICE_FLOOR)
    if settings["costfun"] == COST_FUN_SELF_CONSUMPTION:
        price *= BATTERY_LOCKOUT_SELF_CONSUMPTION_BIGM
    baseline = settings["weight_battery_discharge"]
    values = (
        [baseline[min(index, len(baseline) - 1)] for index in range(count)]
        if isinstance(baseline, list) and baseline
        else [baseline if not isinstance(baseline, list) else 0.0] * count
    )
    for index in range(count):
        start = grid_start + step * index
        if any(window_start <= start < window_end for window_start, window_end in windows):
            values[index] = price
    return {"weight_battery_discharge": values}


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


@dataclass(slots=True)
class _Pin:
    """One load's timestep-0 pin, as EMHASS will actually enforce it."""

    index: int
    draw_w: float
    """What the pin makes the load draw: the pin itself, or the nominal power
    for a semi-continuous load, which EMHASS forces on at nominal rather than
    pinning to a wattage."""
    floor_w: float
    """The least it can be pinned to: its minimum power rounded up to a whole
    watt if it modulates (pins are sent in whole watts, rounded down, so a
    floor of 1400.5 W would otherwise go out as 1400 W, below the minimum),
    all of it if it is semi-continuous."""
    forced_w: float
    """What it still draws with no pin at all: a minimum on-time that
    ``def_current_state`` keeps in force (see :func:`_forced_on_w`), else 0."""
    kept_w: float = 0.0


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _value_covering(series: Any, at: datetime) -> float | None:
    """A payload forecast's value in force at ``at``.

    ``Series.to_payload`` sends a timestamp-to-value mapping, hold-last like
    :meth:`Series.value_at`; a bare list (from a profile's extra settings)
    starts at timestep 0. None when neither answers for ``at``.
    """
    if isinstance(series, list):
        return _numeric(series[0]) if series else None
    if not isinstance(series, dict):
        return None
    best: tuple[datetime, Any] | None = None
    for key, value in series.items():
        when = dt_util.parse_datetime(key) if isinstance(key, str) else None
        if when is None or when > at:
            continue
        if best is None or when > best[0]:
            best = (when, value)
    return None if best is None else _numeric(best[1])


def _forced_on_w(payload: dict[str, Any], k: int, *, state: bool) -> float:
    """What EMHASS forces load ``k`` to draw at timestep 0 from its current
    state alone, with no ``def_current_power`` pin.

    Mirrors the two blocks of EMHASS's ``optimization.py`` that turn a load
    reported as running into a forced-on timestep 0: a single-constant load
    with run time still owed is held on (block A), and any other load whose
    on-streak is short of its minimum on-time is held on for the remainder
    (block B). Neither is a pin, so neither can be shrunk -- releasing one
    would mean misreporting the load's state, or cutting a compressor's
    minimum run short. They can only be counted.
    """
    if not state:
        return 0.0

    def _get(key: str, default: Any = 0) -> Any:
        values = payload.get(key)
        return values[k] if isinstance(values, list) and k < len(values) else default

    required = _numeric(_get("operating_timesteps_of_each_deferrable_load", None))
    completed = _numeric(_get("def_current_operating_timesteps")) or 0.0
    if required is not None and required - completed <= 0:
        return 0.0  # target met: EMHASS releases the load (cots-satisfied)
    if required is None and not _numeric(_get("operating_hours_of_each_deferrable_load")):
        return 0.0
    nominal = _numeric(_get("nominal_power_of_deferrable_loads")) or 0.0
    if _get("set_deferrable_load_single_constant", False):
        return nominal
    minimum_on = _numeric(_get("def_minimum_on_time")) or 0.0
    on_steps = _numeric(_get("def_current_on_timesteps")) or 0.0
    if minimum_on <= 0 or on_steps >= minimum_on:
        return 0.0
    if _get("treat_deferrable_load_as_semi_cont", False):
        return nominal
    return min(_numeric(_get("minimum_power_of_deferrable_loads")) or 0.0, nominal)


def _fit_pins_to_import_limit(
    payload: dict[str, Any], names: Sequence[str], house_live_w: float | None, now: datetime
) -> list[str]:
    """Shrink or release timestep-0 pins the grid import limit cannot carry.

    ``def_current_power`` is a hard equality on timestep 0 (a forced-on
    timestep for a semi-continuous load), so a pin is not something EMHASS
    can move out of the way. Pinned loads plus the house beyond the import
    limit at timestep 0 is an infeasible problem -- the whole plan lost, not
    just this load's. The live fuse limit falling while a car charges is
    exactly that (2026-10-01 22:00).

    The budget is grid import only: PV and the battery may cover a pin, but
    neither is guaranteed (an empty battery, night), and a controllable
    charger is what is meant to back off. Raising the import floor instead
    would override the fuse limit for the whole horizon to protect one
    timestep. Loads EMHASS forces on from their current state alone
    (:func:`_forced_on_w`) are taken off the budget first, since nothing here
    can move them.

    Modulating pins come down first, toward their minimum power and in
    proportion to their room above it. If that is still not enough, pins are
    released -- sent as 0, which EMHASS reads as "no pin" -- last load first,
    down to whatever their state still forces; any released pin that then
    fits again is taken back, and whatever is still spare goes back to the
    modulating pins still held. A released load may be rescheduled; a shrunk
    one keeps running at less. Single-constant and thermal loads are not
    pinned (EMHASS ignores their pins), only counted.

    Mutates ``payload["def_current_power"]`` in place and returns a warning
    per load it changed, plus one if what is forced still does not fit.
    MPC only -- see the caller.
    """
    powers = payload.get("def_current_power")
    if not isinstance(powers, list):
        powers = []
    limit = payload.get("maximum_power_from_grid")
    grid_w = _numeric(limit[0] if isinstance(limit, list) and limit else limit)
    if grid_w is None:
        return []
    house_w = _value_covering(payload.get("load_power_forecast"), now)
    if house_w is None:
        house_w = house_live_w
    house_w = max(house_w or 0.0, 0.0)

    def _flag(key: str, k: int) -> Any:
        values = payload.get(key)
        return values[k] if isinstance(values, list) and k < len(values) else None

    states = payload.get("def_current_state")
    count = len(states) if isinstance(states, list) else 0
    pins: list[_Pin] = []
    fixed: list[tuple[int, float]] = []
    for k in range(count):
        if _flag("def_load_config", k):
            continue  # thermal: governed by its temperature, neither pinned nor forced
        nominal = _numeric(_flag("nominal_power_of_deferrable_loads", k))
        if nominal is None:
            continue  # a sequence load's list-valued power: not pinned either
        state = bool(states[k])
        forced = _forced_on_w(payload, k, state=state)
        sent = _numeric(powers[k]) if k < len(powers) else None
        if not sent or sent <= 0 or _flag("set_deferrable_load_single_constant", k):
            if forced > 0:
                fixed.append((k, forced))
            continue
        if _flag("treat_deferrable_load_as_semi_cont", k):
            pins.append(_Pin(k, nominal, nominal, forced))
        else:
            minimum = _numeric(_flag("minimum_power_of_deferrable_loads", k)) or 0.0
            floor = min(float(math.ceil(minimum)), sent)
            pins.append(_Pin(k, sent, floor, min(forced, floor)))

    def _name(k: int) -> str:
        return names[k] if k < len(names) else f"deferrable{k}"

    fixed_w = sum(watts for _k, watts in fixed)
    budget_w = grid_w - house_w - fixed_w
    excess = sum(pin.draw_w for pin in pins) - budget_w
    if excess <= 0.5:
        return []

    for pin in pins:
        pin.kept_w = pin.draw_w
    room = sum(pin.draw_w - pin.floor_w for pin in pins)
    if room > 0:
        cut = min(excess, room)
        for pin in pins:
            pin.kept_w -= cut * (pin.draw_w - pin.floor_w) / room
        excess -= cut
    released: set[int] = set()
    for pin in reversed(pins):
        if excess <= 0.5:
            break
        excess -= pin.kept_w - pin.forced_w
        pin.kept_w = pin.forced_w
        released.add(pin.index)
    spare = -excess
    # Releasing last-first can free more than was needed: a big pin released
    # after a small one leaves room the small one fits in again.
    for pin in pins:
        if pin.index in released and pin.floor_w - pin.forced_w <= spare:
            spare -= pin.floor_w - pin.forced_w
            pin.kept_w = pin.floor_w
            released.discard(pin.index)
    for pin in pins:
        if spare <= 0:
            break
        if pin.index not in released and pin.floor_w < pin.draw_w:
            give = min(spare, pin.draw_w - pin.kept_w)
            pin.kept_w += give
            spare -= give

    warnings: list[str] = []
    budget_text = (
        f"timestep 0 has only {max(budget_w, 0.0):.0f} W of grid import left after the "
        f"house's {house_w:.0f} W" + (f" and {fixed_w:.0f} W of loads held on" if fixed_w else "")
    )
    for pin in pins:
        prefix = (
            f"{_name(pin.index)}: pinned at {pin.draw_w:.0f} W as already running, "
            f"but {budget_text}"
        )
        if pin.index in released:
            powers[pin.index] = 0.0
            warnings.append(f"{prefix}; not pinned this run, so EMHASS may reschedule it.")
        elif pin.kept_w < pin.draw_w - 0.5:
            # Whole watts, rounded down to stay inside the budget. floor_w is
            # already a whole watt, so this can never land below the minimum.
            powers[pin.index] = float(math.floor(pin.kept_w))
            warnings.append(f"{prefix}; pinned at {powers[pin.index]:.0f} W instead.")
    if spare < -0.5:
        held = ", ".join(
            f"{_name(k)} {watts:.0f} W"
            for k, watts in [*fixed, *((p.index, p.forced_w) for p in pins if p.forced_w)]
        )
        warnings.append(
            f"Timestep 0 still needs {-spare:.0f} W more than the grid import limit "
            f"allows after releasing every pin: loads held on by their current state "
            f"(single-constant run or minimum on-time) cannot be moved: {held or 'none'}."
        )
    if "def_current_power" in payload and not any(_numeric(value) for value in powers):
        # Nothing left pinned: say so by omission, the shape every request
        # with no running load already has.
        del payload["def_current_power"]
    return warnings


def build_payload(inputs: PayloadInputs) -> PayloadResult:
    """Assemble one request with coverage and positional arrays aligned to row zero."""
    step = timedelta(minutes=inputs.time_step_minutes)
    horizon_end = inputs.row_zero(step) + step * inputs.horizon_steps
    effective = resolve_effective_settings(
        battery=inputs.battery,
        grid=inputs.grid,
        hybrid=inputs.hybrid_inverter,
        time_step_minutes=inputs.time_step_minutes,
        cost_fun=inputs.cost_fun,
        row_zero_mode=inputs.row_zero_mode,
        num_lags=inputs.num_lags
        or (
            max(1, math.ceil(inputs.horizon_steps * inputs.time_step_minutes / 1440))
            * (1440 // inputs.time_step_minutes)
        ),
        profile_settings=inputs.extra_settings,
        backend_version=inputs.backend_version,
        send_derating=inputs.send_charge_power_derating,
        send_soc_final_reward=inputs.send_soc_final_reward,
        use_pv=bool(inputs.pv) if inputs.use_pv is None else inputs.use_pv,
    )
    warnings = list(effective.warnings)
    payload = effective.runtime()

    if inputs.action == ACTION_MPC:
        # Counted in timesteps, not hours -- a frequent source of horizons that
        # are wrong by a factor of two.
        payload["prediction_horizon"] = inputs.horizon_steps
        # A per-timestep array (maximum_power_from_grid as a list, below) must
        # be exactly this long on MPC -- see _prepare_power_limit_array's
        # silent collapse-to-first-element failure mode.
        capacity_array_steps = inputs.horizon_steps
    else:
        hours = inputs.horizon_steps * inputs.time_step_minutes / 60
        delta_forecast_daily = max(1, math.ceil(hours / 24))
        payload["delta_forecast_daily"] = delta_forecast_daily
        # Day-ahead's own array length is whole days' worth of timesteps, not
        # horizon_steps -- see docs/network_tariffs_plan.md, "Mechanism 3".
        capacity_array_steps = delta_forecast_daily * (24 * 60 // inputs.time_step_minutes)

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
            # Filled on the payload's grid rather than the price source's own,
            # so an hourly source under a 30-minute step lands on the horizon
            # instead of up to one price interval short of it. value_at holds
            # last, so each sub-hour point still carries that hour's price --
            # the extra points add reach, not invented precision.
            series = series.extended_with_previous_day(horizon_end, step)
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
    payload.update(
        _battery_lockout_weights(
            inputs, inputs.row_zero(step), horizon_end, step, capacity_array_steps, payload
        )
    )
    import_floor = _import_floor_w(inputs, horizon_end)
    import_scalar_w = resolve_grid_limit(
        inputs.grid_import_limit_w, inputs.grid.import_max_w, import_floor
    )
    # A network profile's capacity_limit and/or demand-window fallback turn
    # the scalar above into a per-timestep list -- only where either actually
    # applies, so the common case (no network profile, or one with no
    # capacity concern) keeps sending the plain scalar every other test here
    # expects.
    capacity_ceilings: list[tuple[Callable[[datetime], bool], float]] = []
    if inputs.capacity_limit_w is not None and inputs.capacity_limit_window is not None:
        capacity_ceilings.append((inputs.capacity_limit_window, inputs.capacity_limit_w))
    if (
        inputs.network_demand_charge_configured
        and inputs.demand_charge_rate_per_kw is None
        and inputs.demand_fallback_ceiling_w is not None
        and inputs.demand_window is not None
    ):
        capacity_ceilings.append((inputs.demand_window, inputs.demand_fallback_ceiling_w))
    if capacity_ceilings:
        capacity_array, capacity_warnings = build_capacity_limit_array(
            grid_scalar_w=import_scalar_w,
            ceilings=capacity_ceilings,
            grid_start=inputs.row_zero(step),
            step=step,
            count=capacity_array_steps,
            load=inputs.load,
        )
        payload["maximum_power_from_grid"] = capacity_array
        warnings.extend(capacity_warnings)
    else:
        payload["maximum_power_from_grid"] = import_scalar_w
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
            f"The live grid import limit (limit sensor or phase guard) read "
            f"{inputs.grid_import_limit_w:.0f} W, below the "
            f"{import_floor:.0f} W the house is forecast to draw anyway. Raised "
            "to that value, since a lower limit has no feasible plan."
        )
    # Demand charge on the horizon's peak import. Sent unconditionally rather
    # than from _battery_settings: EMHASS prices it off the grid variable, so
    # it must keep working for a plant with no battery at all, and that helper
    # returns early when the battery is off.
    #
    # A network profile's demand charge, once configured, owns this entirely
    # -- it never falls back to the plain manual number below, because that
    # number carries none of the window/memory reasoning the profile does,
    # and mixing the two would silently reintroduce the "too broad" bug
    # docs/network_tariffs_plan.md describes. current_period_peak is the
    # memory half of the same mechanism, and only means anything to EMHASS on
    # naive-mpc-optim.
    if inputs.network_demand_charge_configured:
        if inputs.action == ACTION_MPC and inputs.demand_charge_rate_per_kw is not None:
            rates = inputs.demand_charge_rate_per_kw
            rate_list = rates if isinstance(rates, list) else [rates]
            k = len(rate_list)
            if k == 1:
                payload["capacity_cost_per_kw"] = round(rate_list[0], 4)
            else:
                payload["capacity_cost_per_kw"] = [round(rate, 4) for rate in rate_list]

            if inputs.current_period_peak_w is not None:
                peaks = inputs.current_period_peak_w
                if isinstance(peaks, list):
                    payload["current_period_peak"] = [round(peak) for peak in peaks]
                else:
                    payload["current_period_peak"] = round(peaks)

            if inputs.demand_charge_window is not None:
                windows = inputs.demand_charge_window
                start = inputs.row_zero(step)
                if isinstance(windows, list):
                    payload["capacity_charge_window"] = [
                        build_capacity_charge_window_mask(
                            in_window=predicate,
                            start=start,
                            step=step,
                            count=inputs.horizon_steps,
                        )
                        for predicate in windows
                    ]
                else:
                    payload["capacity_charge_window"] = build_capacity_charge_window_mask(
                        in_window=windows,
                        start=start,
                        step=step,
                        count=inputs.horizon_steps,
                    )

            ns = inputs.capacity_interval_timesteps
            n_list = ns if isinstance(ns, list) else [ns]
            if any(n > 1 for n in n_list):
                if len(n_list) == 1 or len(set(n_list)) == 1:
                    payload["capacity_charge_interval_timesteps"] = n_list[0]
                else:
                    payload["capacity_charge_interval_timesteps"] = list(n_list)
                history = inputs.capacity_interval_history_w
                if k == 1:
                    single = history if isinstance(history, list) else []
                    # Distinguish list[float] from list[list[float]]: K=1
                    # history is flat watts.
                    if single and isinstance(single[0], list):
                        single = single[0]  # type: ignore[assignment]
                    payload["capacity_charge_current_interval_history"] = [
                        round(w)
                        for w in (single or [])  # type: ignore[union-attr]
                    ]
                else:
                    histories: list[list[float]]
                    if history and isinstance(history[0], list):
                        histories = history  # type: ignore[assignment]
                    else:
                        histories = [[] for _ in range(k)]
                    while len(histories) < k:
                        histories.append([])
                    payload["capacity_charge_current_interval_history"] = [
                        [round(w) for w in (histories[i] if i < len(histories) else [])]
                        for i in range(k)
                    ]
        else:
            # Day-ahead, or the peak cannot be safely priced yet (see
            # coordinator.demand_charge_pricing): zero rather than the stale
            # manual number, which EMHASS would otherwise price across the
            # whole horizon with no window and no memory.
            payload["capacity_cost_per_kw"] = 0.0
    else:
        payload["capacity_cost_per_kw"] = inputs.flat_demand_charge_per_kw
    deferrable, load_order, deferrable_warnings, extra_rows = _deferrable_settings(inputs, step)
    row_count = len(load_order) + len(extra_rows)
    payload.update(deferrable)
    payload["is_electric_load"] = [True] * row_count
    payload["cost_forecast_per_deferrable_load"] = [None] * row_count
    warnings.extend(deferrable_warnings)

    group_settings, group_warnings = _deferrable_load_group_settings(
        inputs.load_groups, load_order, extra_rows
    )
    payload.update(group_settings)
    warnings.extend(group_warnings)

    thermal = _thermal_settings(inputs, step, row_count)
    payload.update(thermal)
    tank_shortfalls = _tank_shortfalls(inputs, step)
    warnings.extend(
        f"Water tank {load.name}: its temperature target cannot be reached in time, "
        "so the plan aims for the highest temperature the heater can reach instead."
        for load in inputs.loads
        if load.subentry_id in tank_shortfalls
    )

    # After everything the check reads is in place: the import limit, the
    # blended house load, and def_load_config for which loads are thermal.
    # MPC only, since only there is timestep 0 "now" and a pin a fact.
    if inputs.action == ACTION_MPC:
        warnings.extend(
            _fit_pins_to_import_limit(
                payload,
                [*(load.name for load in inputs.loads), *(row.name for row in extra_rows)],
                inputs.load_live_w,
                inputs.now,
            )
        )

    _log_warnings(warnings)

    return PayloadResult(
        payload=payload,
        warnings=warnings,
        load_order=load_order,
        extra_rows=extra_rows,
        tank_shortfalls=tank_shortfalls,
        defaulted_forecast_methods=frozenset(
            name
            for name in (
                "load_forecast_method",
                "weather_forecast_method",
                "load_cost_forecast_method",
                "production_price_forecast_method",
            )
            if name not in effective.profile_overrides
        ),
    )


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
        thermal_by_index, load_count, inputs.row_zero(step), step, inputs.horizon_steps
    )
    return {"def_load_config": config if config is not None else [{} for _ in range(load_count)]}


def _tank_shortfalls(inputs: PayloadInputs, step: timedelta) -> dict[str, TankShortfall]:
    """Tanks whose schedule asks for more than the heater can deliver in time.

    ``WaterTankConfig.to_emhass`` has already lowered those floors in the
    request; this is only the report, for a warning and a repair issue.
    """
    shortfalls: dict[str, TankShortfall] = {}
    for load in inputs.loads:
        if not isinstance(load.thermal, WaterTankConfig) or not load.wants_to_run:
            continue
        shortfall = load.thermal.first_unreachable_step(
            inputs.row_zero(step), step, inputs.horizon_steps
        )
        if shortfall is not None:
            shortfalls[load.subentry_id] = shortfall
    return shortfalls


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
    off_grid = not math.isclose(quantised, load.operating_hours, rel_tol=1e-9, abs_tol=1e-9)
    if off_grid and not load.hours_from_surplus_budget:
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
        grid_start=inputs.row_zero(step),
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
    # Saying "not this cycle" is the only way out: the window comes back
    # around and asks properly once the horizon reaches it.
    #
    # Parked through :func:`_park` rather than by zeroing the hours here.
    # Zero hours alone does *not* stop EMHASS acting on the load: the
    # current-state trio (def_current_state, def_current_power,
    # def_current_operating_timesteps) drives pins and force-on blocks that
    # are read independently of the operating requirement -- see the
    # single-constant branch below, which is the same trap reached from the
    # other direction, and _park's own docstring for why a zero-hour load
    # must never also claim to be running.
    if window.opens_beyond_horizon:
        return _park(load)

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
    #
    # Zero hours is necessary but not sufficient, which is why this parks the
    # load outright. Block A's pin is gated on def_current_state, and
    # def_current_power is a hard equality on timestep 0 regardless of the
    # operating requirement (planning/run_now_via_block_b.md) -- so leaving
    # the current-state trio truthful while asking for nothing is the exact
    # contradiction _park exists to prevent.
    if load.current_state and load.single_constant and window.start_index > 0:
        warnings.append(
            f"{load.name}: still running from an earlier decision, but its window "
            f"now starts at step {window.start_index}; asking for 0 hours this run "
            f"instead of pinning it to the later block."
        )
        return _park(load)

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
) -> tuple[dict[str, Any], list[str], list[str], list[ExtraRow]]:
    # Every configured load, always, in the registry's stable order: a load's
    # deferrable number is meant to be as durable as the load itself.
    loads = list(inputs.loads)
    if not loads:
        return {"number_of_deferrable_loads": 0}, [], [], []

    warnings: list[str] = []
    loads = [_without_credit_for_later_day(load, inputs, step) for load in loads]
    real = [
        _describe(load, inputs, step, warnings) if load.wants_to_run else _park(load)
        for load in loads
    ]
    real = [
        _spanning_daily_windows(row, load, inputs, step) if load.spans_daily_windows else row
        for row, load in zip(real, loads, strict=True)
    ]
    # Rows for later windows of daily loads go after every real load, so no real
    # load's P_deferrable{k} ever moves. Ordered by occurrence, then by load.
    extras = _extra_window_rows(loads, inputs, step)
    extra_rows = [ExtraRow(parent, name) for _n, parent, name, _row in extras]
    described = [*real, *(row for _n, _parent, _name, row in extras)]

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

    return settings, [load.subentry_id for load in loads], warnings, extra_rows


def _spanning_daily_windows(
    row: _LoadSettings, load: DeferrableLoad, inputs: PayloadInputs, step: timedelta
) -> _LoadSettings:
    """Stretch a water tank's single row over every daily window in the horizon.

    EMHASS gives a tank's row one contiguous heating mask and one cap on starts
    for the whole horizon. The temperature arrays already carry each period's
    deadline, so the mask only has to reach from the first window's opening to
    the last one's closing (heating between windows stays allowed), and the
    start cap grows with the number of windows so a later one is not starved.
    """
    if not load.wants_to_run:
        return row
    # Look a day past the horizon: a window that opens inside it but closes
    # after it is dropped for a load that needs whole windows, yet the tank's
    # temperature targets still reach into it, so its mask must too.
    day_steps = round(timedelta(days=1) / step)
    occurrences = [
        (start, end)
        for start, end in window_occurrences(
            load.earliest_start,
            load.latest_end,
            inputs.row_zero(step),
            step,
            inputs.horizon_steps + day_steps,
            timedelta(minutes=load.minimum_off_time_minutes),
        )
        if start < inputs.horizon_steps
    ]
    if len(occurrences) < 2:
        return row
    end_index = occurrences[-1][1]
    if inputs.action == ACTION_MPC and end_index >= inputs.horizon_steps:
        end_index = 0  # same convention as the first window: unconstrained
    return replace(
        row,
        start_index=occurrences[0][0],
        end_index=end_index,
        max_startups=row.max_startups * len(occurrences),
    )


def _without_credit_for_later_day(
    load: DeferrableLoad, inputs: PayloadInputs, step: timedelta
) -> DeferrableLoad:
    """Drop today's progress from a daily load whose first window opens on a later date.

    Progress is measured per local day. Once today's run is done, the first
    window left in the horizon is tomorrow's, and crediting today's run against
    it would give that day no hours while the day after gets them in full. Only
    loads that get extra rows are touched, so a single-row load is unchanged.
    """
    if not (load.repeats_daily and load.wants_to_run and load.completed_timesteps > 0):
        return load
    occurrences = window_occurrences(
        load.earliest_start,
        load.latest_end,
        inputs.row_zero(step),
        step,
        inputs.horizon_steps,
        timedelta(minutes=load.minimum_off_time_minutes),
    )
    if len(occurrences) < 2 or occurrences[0][0] == 0:
        return load  # one window, or the first is open now: its progress is its own
    opens = dt_util.as_local(inputs.row_zero(step) + step * occurrences[0][0])
    if opens.date() > dt_util.as_local(inputs.now).date():
        return replace(load, completed_timesteps=0)
    return load


def _extra_window_rows(
    loads: list[DeferrableLoad],
    inputs: PayloadInputs,
    step: timedelta,
) -> list[tuple[int, int, str, _LoadSettings]]:
    """Rows for the later windows of daily loads, as ``(n, parent, name, row)``.

    Only the first window carries runtime state: nothing has run in a future
    window, so an extra row has no pin, no running state and no progress, and
    asks for the load's full quantised hours.

    The requirement is described afresh from the load rather than copied from
    the parent's row. That row can be parked for reasons that only concern
    today (a running single-constant load whose first window is not open yet),
    and tomorrow's run is still wanted.
    """
    extras: list[tuple[int, int, str, _LoadSettings]] = []
    for parent, load in enumerate(loads):
        if not (load.repeats_daily and load.wants_to_run):
            continue
        occurrences = window_occurrences(
            load.earliest_start,
            load.latest_end,
            inputs.row_zero(step),
            step,
            inputs.horizon_steps,
            timedelta(minutes=load.minimum_off_time_minutes),
        )
        if len(occurrences) < 2:
            continue
        # Nothing has run in a later window, so describe the load as if nothing
        # had. Zero hours from this means the first window is beyond the horizon
        # (nothing else parks a load that is not running), and so are the later
        # ones.
        full = _describe(
            replace(load, current_state=False, current_power_w=0.0, completed_timesteps=0),
            inputs,
            step,
            [],
        )
        if full.hours <= 0:
            continue
        for n, (start_index, end_index) in enumerate(occurrences[1:], start=2):
            extras.append(
                (
                    n,
                    parent,
                    f"{load.name} (day {n})",
                    replace(
                        full,
                        start_index=start_index,
                        # Same convention as the first window: an end on or
                        # past the horizon edge means unconstrained. Day-ahead
                        # solves whole local days (DST-dependent), so its edge
                        # is not horizon_steps: the end stays explicit there.
                        end_index=(
                            0
                            if inputs.action == ACTION_MPC and end_index >= inputs.horizon_steps
                            else end_index
                        ),
                        current_state=False,
                        current_power_w=0.0,
                        completed_timesteps=0,
                        current_on_timesteps=0,
                        current_off_timesteps=0,
                    ),
                )
            )
    extras.sort(key=lambda item: (item[0], item[1]))
    return extras


def _deferrable_load_group_settings(
    load_groups: list[DeferrableLoadGroup],
    load_order: list[str],
    extra_rows: Sequence[ExtraRow] = (),
) -> tuple[dict[str, Any], list[str]]:
    """Describe cross-load relationships (a shared budget or mutual exclusion).

    Each group's members are resolved from subentry ids into the
    ``"deferrable{k}"`` name EMHASS expects, using their position in
    ``load_order``. A member no longer present there (the load was deleted or
    renamed since the group was created) is dropped with a warning rather than
    failing the whole request; a group left with fewer than two valid members
    is meaningless and dropped entirely.

    A member with rows for later daily windows contributes those rows too, so
    a shared power budget caps the sum and mutual exclusion covers every window.
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
            index = load_order.index(subentry_id)
            names.append(f"deferrable{index}")
            names.extend(
                f"deferrable{len(load_order) + i}"
                for i, row in enumerate(extra_rows)
                if row.parent == index
            )

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

    return {"deferrable_load_groups": groups}, warnings

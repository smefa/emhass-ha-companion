"""Data models for EMHASS Companion.

All datetimes in this module are timezone-aware and normalised to UTC. EMHASS's
plan records are explicitly UTC ("align consumers on UTC", per its
``/api/v1/plan`` schema), and forecast payloads we send carry an explicit offset,
so no part of this integration ever relies on an implicit local timezone.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
import math
from typing import Any, Self

from .const import (
    CONF_BATTERY_SOC_DEFICIT_COST,
    CONF_BATTERY_SOC_DEFICIT_THRESHOLD,
    CONF_BATTERY_SOC_SURPLUS_COST,
    CONF_BATTERY_SOC_SURPLUS_THRESHOLD,
    CONF_BATTERY_STRESS_COST,
    CONF_BATTERY_STRESS_SEGMENTS,
    CONF_CAPACITY_COST_PER_KW,
    CONF_COMPUTE_CURTAILMENT,
    CONF_END_SOC_MODE,
    CONF_GRID_EXPORT_LIMIT_ENTITY,
    CONF_GRID_IMPORT_LIMIT_ENTITY,
    CONF_HYBRID_INVERTER,
    CONF_INVERTER_AC_INPUT_MAX,
    CONF_INVERTER_AC_OUTPUT_MAX,
    CONF_INVERTER_EFFICIENCY_AC_DC,
    CONF_INVERTER_EFFICIENCY_DC_AC,
    CONF_WEIGHT_BATTERY_CHARGE,
    CONF_WEIGHT_BATTERY_DISCHARGE,
    DEFAULT_BATTERY_DYNAMIC_MAX,
    DEFAULT_BATTERY_DYNAMIC_MIN,
    DEFAULT_BATTERY_SOC_DEFICIT_COST,
    DEFAULT_BATTERY_SOC_DEFICIT_THRESHOLD,
    DEFAULT_BATTERY_SOC_SURPLUS_COST,
    DEFAULT_BATTERY_SOC_SURPLUS_THRESHOLD,
    DEFAULT_BATTERY_STRESS_COST,
    DEFAULT_BATTERY_STRESS_SEGMENTS,
    DEFAULT_CAPACITY_COST_PER_KW,
    DEFAULT_CHARGE_EFFICIENCY,
    DEFAULT_DISCHARGE_EFFICIENCY,
    DEFAULT_END_SOC_MODE,
    DEFAULT_GRID_EXPORT_MAX,
    DEFAULT_GRID_IMPORT_MAX,
    DEFAULT_INVERTER_EFFICIENCY,
    DEFAULT_SELF_CONSUME_THRESHOLD_W,
    DEFAULT_SOC_MAX,
    DEFAULT_SOC_MIN,
    DEFAULT_SOC_TARGET,
    DEFAULT_WEIGHT_BATTERY_CHARGE,
    DEFAULT_WEIGHT_BATTERY_DISCHARGE,
)
from .thermal import ThermalConfig


class SeriesError(ValueError):
    """Raised when a time series cannot be built or is unusable."""


@dataclass(frozen=True, slots=True)
class Point:
    """A single timestamped value."""

    time: datetime
    value: float


class Series:
    """An immutable, sorted, de-duplicated time series.

    This is the common currency between profiles (which produce it), the tariff
    engine (which transforms it) and the payload builder (which serialises it).
    """

    __slots__ = ("_points", "_times")

    def __init__(self, points: Iterable[Point]) -> None:
        # Later entries win on duplicate timestamps. That matters when a profile
        # concatenates overlapping attributes (a "today" and "tomorrow" pair often
        # share a boundary timestamp) and the newer block should take precedence.
        merged: dict[datetime, float] = {}
        for point in points:
            when = point.time
            if when.tzinfo is None:
                raise SeriesError(f"Naive datetime in series: {when!r}")
            merged[when.astimezone(UTC)] = float(point.value)
        times = tuple(sorted(merged))
        self._points = tuple(Point(when, merged[when]) for when in times)
        # Kept alongside the points purely so the hold-last lookups can binary
        # search. Every plan-derived entity calls value_at at each clock tick,
        # and a linear walk over ~96 points times ten loads times their sensors
        # is thousands of redundant comparisons a tick on a Raspberry Pi.
        self._times = times

    # -- construction ---------------------------------------------------------

    @classmethod
    def empty(cls) -> Self:
        return cls(())

    @classmethod
    def concat(cls, series: Iterable[Series]) -> Self:
        return cls(point for item in series for point in item)

    # -- container protocol ---------------------------------------------------

    def __iter__(self) -> Iterator[Point]:
        return iter(self._points)

    def __len__(self) -> int:
        return len(self._points)

    def __bool__(self) -> bool:
        return bool(self._points)

    def __repr__(self) -> str:
        if not self._points:
            return "Series(empty)"
        return (
            f"Series({len(self._points)} points, "
            f"{self.start.isoformat()} .. {self.end.isoformat()})"
        )

    # -- properties -----------------------------------------------------------

    @property
    def start(self) -> datetime:
        if not self._points:
            raise SeriesError("Empty series has no start")
        return self._points[0].time

    @property
    def end(self) -> datetime:
        if not self._points:
            raise SeriesError("Empty series has no end")
        return self._points[-1].time

    @property
    def values(self) -> tuple[float, ...]:
        return tuple(point.value for point in self._points)

    # -- transforms -----------------------------------------------------------

    def scaled(self, factor: float = 1.0, offset: float = 0.0) -> Series:
        return Series(Point(p.time, p.value * factor + offset) for p in self._points)

    def map_values(self, func) -> Series:
        return Series(Point(p.time, func(p.value)) for p in self._points)

    def blend_at(self, when: datetime, live_value: float, beta: float) -> Series:
        """Blend a live/measured value into the point covering ``when``.

        Mirrors EMHASS's own ``Forecast.get_mix_forecast`` correction
        (``forecast.py``): ``beta`` weights the live value, ``1 - beta``
        weights the forecast. Only that one point changes -- deliberately
        *not* ``self._points[0]``: a series fetched from a real profile (e.g.
        a PV forecast starting at local midnight) commonly starts well before
        ``when``, and EMHASS's MPC solver only looks at rows from ``when``
        onward, so correcting index 0 would blend a live reading into a
        timestep nobody uses. Hold-last semantics, matching ``value_at``: the
        point that changes is the last one at or before ``when``. A series
        with no such point (``when`` is before every point, or the series is
        empty) passes through untouched since there is nothing to correct.
        """
        if not self._points:
            return self
        index = self._index_at(when)
        if index is None:
            return self
        target = self._points[index]
        blended = (1 - beta) * target.value + beta * live_value
        points = list(self._points)
        points[index] = Point(target.time, blended)
        return Series(points)

    def window(self, start: datetime, end: datetime) -> Series:
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        return Series(p for p in self._points if start <= p.time < end)

    def window_covering(self, start: datetime, end: datetime) -> Series:
        """:meth:`window`, keeping the point that is in force *at* ``start``.

        ``window`` drops everything before ``start``, which discards the one
        point that says what holds at ``start`` itself whenever the series is
        coarser than the boundary it is cut on: hourly prices cut at a
        quarter-hour boundary lose the hour they are inside, and
        :meth:`value_at` -- which deliberately refuses to extrapolate backwards
        -- then answers None until the next hour begins. For a series published
        next to a plan that starts at ``start``, that is a sensor reading
        "unknown" across the very window it is describing.

        The covering point is re-stamped to ``start`` rather than kept at its
        own timestamp. Its value is untouched and stays true, since hold-last
        semantics mean it is in force at ``start``; the re-stamping is what
        keeps the result spanning exactly ``[start, end)``, so a series
        published alongside a plan shares that plan's extent instead of
        overhanging it on the left by up to one point.

        A series that already has a point exactly on ``start`` is returned
        unchanged -- the re-stamp is then a no-op, and this is plain
        :meth:`window`.
        """
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        covering = [point for point in self._points if point.time <= start]
        return Series(
            ([Point(start, covering[-1].value)] if covering else [])
            + [point for point in self._points if start < point.time < end]
        )

    # -- queries --------------------------------------------------------------

    def value_at(self, when: datetime) -> float | None:
        """Value of the interval containing ``when`` (hold-last semantics).

        Returns ``None`` before the first point rather than extrapolating
        backwards, so callers can distinguish "no data yet" from a real zero.
        """
        index = self._index_at(when)
        return None if index is None else self._points[index].value

    def _index_at(self, when: datetime) -> int | None:
        """Index of the last point at or before ``when``, or None if there is none.

        ``bisect_right`` over the parallel timestamps built in ``__init__``:
        the points are sorted and de-duplicated there, which is exactly the
        precondition a binary search needs.
        """
        position = bisect_right(self._times, when.astimezone(UTC))
        return position - 1 if position else None

    def covers(self, until: datetime) -> bool:
        """Whether the series extends to ``until``.

        EMHASS hold-last-fills a timestamped forecast onto its own grid, so a
        series that stops short is *silently* extended with its final value
        instead of raising. Checking coverage here is what turns that into a
        visible warning rather than a plan quietly built on a flat-lined price.
        """
        return bool(self._points) and self.end >= until.astimezone(UTC)

    def extended_with_previous_day(self, until: datetime, step: timedelta | None = None) -> Series:
        """Fill the gap beyond the last known point by repeating the day before.

        For a day-ahead price source (e.g. Nord Pool before its ~13:00
        publication) that does not yet cover the full horizon, this is a much
        better guess than plain hold-last: EMHASS's own fallback flat-lines
        the horizon at whatever the final known price happens to be, which for
        a series that stops at local midnight is usually one of the cheapest
        hours of the day (last night) -- held across the whole of tomorrow.
        Repeating yesterday's shape at least preserves the peak/off-peak
        pattern.

        Falls back to hold-last (via :meth:`value_at`'s own behaviour) once
        there is no 24h-earlier point left to copy, e.g. more than a day past
        the original coverage, or when the series holds less than a day of
        history to begin with.

        ``step`` names the spacing to extend at, for callers that know the
        grid they want rather than the one this series happens to have. A
        series of a single point has no spacing to infer at all -- a brand
        new load sensor with one reading being exactly that case -- and
        guessing an hour there would hand EMHASS a forecast four times
        coarser than the optimisation it feeds.
        """
        if not self._points or self.covers(until):
            return self
        until = until.astimezone(UTC)
        step = step or self.step() or timedelta(hours=1)
        day = timedelta(days=1)
        points = list(self._points)
        cursor = self.end + step
        while cursor <= until:
            value = self.value_at(cursor - day)
            if value is None:
                # Before the series even started a day ago -- nothing to
                # copy, so hold the last known value instead of guessing.
                value = points[-1].value
            points.append(Point(cursor, value))
            cursor += step
        return Series(points)

    def resampled(self, step: timedelta) -> Series:
        """Average onto a wall-clock grid of ``step``-wide buckets.

        Recorder history arrives at whatever rate the sensor happens to
        report -- a house power sensor updating every few seconds puts tens
        of thousands of points into a series EMHASS will immediately
        resample down to its own grid anyway. Doing it here instead keeps
        the request proportional to the horizon rather than to the sensor's
        update rate.

        Each bucket carries the *time-weighted* mean across it, holding each
        point's value until the next one -- the same piecewise-constant
        reading of a series that :meth:`value_at` uses. A plain mean over
        the samples falling in a bucket would be wrong for recorder data,
        which only emits a row when a sensor *changes*: a value that held
        steady for ten minutes would count for no more than one that held
        for five seconds.

        Bucket timestamps are floored to a multiple of ``step`` from the
        Unix epoch, so for any step dividing an hour they land on whole
        wall-clock boundaries -- no stray seconds, and a grid that lines up
        with both EMHASS's own (anchored the same way) and the forecast
        sources whose points already arrive on the hour or half-hour. That
        alignment is what lets :meth:`extended_with_previous_day` find a
        real point exactly 24h back, and what stops :meth:`__init__`
        treating 17:06:00 and 17:06:04.871334 as two separate readings.
        """
        if not self._points or step <= timedelta(0):
            return self
        size = step.total_seconds()
        totals: dict[int, float] = {}
        spans: dict[int, float] = {}
        for index, point in enumerate(self._points):
            begin = point.time.timestamp()
            if index + 1 < len(self._points):
                finish = self._points[index + 1].time.timestamp()
            else:
                # The final reading has nothing after it to bound its span;
                # let it carry to the end of its own bucket so that a series
                # of a single point still resamples to that one point.
                finish = (begin // size + 1) * size
            while begin < finish:
                bucket = int(begin // size)
                edge = min((bucket + 1) * size, finish)
                weight = edge - begin
                totals[bucket] = totals.get(bucket, 0.0) + point.value * weight
                spans[bucket] = spans.get(bucket, 0.0) + weight
                begin = edge
        return Series(
            Point(
                datetime.fromtimestamp(bucket * size, UTC),
                totals[bucket] / spans[bucket],
            )
            for bucket in sorted(totals)
            if spans[bucket] > 0
        )

    def step(self) -> timedelta | None:
        """The most common spacing between points, or None if indeterminate."""
        if len(self._points) < 2:
            return None
        gaps: dict[timedelta, int] = {}
        for previous, current in zip(self._points, self._points[1:], strict=False):
            gap = current.time - previous.time
            gaps[gap] = gaps.get(gap, 0) + 1
        return max(gaps, key=lambda gap: gaps[gap])

    # -- serialisation --------------------------------------------------------

    def to_payload(self) -> dict[str, float]:
        """Serialise for an EMHASS runtime parameter.

        Emitted as an ISO-timestamp -> value mapping rather than a bare list.
        A bare list that is shorter than EMHASS's forecast grid is only logged as
        an error and leaves the parameter unset, producing a confusing failure
        further inside the optimiser; the mapping form is resampled and aligned
        by EMHASS itself. Offsets are always explicit, so a timestamp is never
        reinterpreted in the server's local timezone.
        """
        return {p.time.isoformat(): p.value for p in self._points}

    def to_attribute(self) -> list[dict[str, Any]]:
        """Serialise for a Home Assistant entity attribute."""
        return [{"time": p.time.isoformat(), "value": p.value} for p in self._points]


# --- Configuration models ----------------------------------------------------


@dataclass(slots=True)
class BatteryConfig:
    """Battery parameters, sent with every optimisation request."""

    enabled: bool = False
    capacity_wh: float = 0.0
    charge_power_max_w: float = 0.0
    discharge_power_max_w: float = 0.0
    soc_min: float = DEFAULT_SOC_MIN
    soc_max: float = DEFAULT_SOC_MAX
    soc_target: float = DEFAULT_SOC_TARGET
    end_soc_mode: str = DEFAULT_END_SOC_MODE
    """How the end-of-horizon SOC target is chosen -- see terminal.py. In
    Optimized mode ``soc_target`` doubles as the reserve floor the computed
    target never plans below."""
    charge_efficiency: float = DEFAULT_CHARGE_EFFICIENCY
    discharge_efficiency: float = DEFAULT_DISCHARGE_EFFICIENCY
    no_discharge_to_grid: bool = False
    """Forbids the battery from exporting -- it may still power the house, just
    not sell. EMHASS's own default is True, which silently blocks arbitrage
    even when a plan would otherwise discharge into a price spike; False here
    so a plant that can export does, unless this is turned on deliberately."""
    weight_battery_discharge: float = DEFAULT_WEIGHT_BATTERY_DISCHARGE
    weight_battery_charge: float = DEFAULT_WEIGHT_BATTERY_CHARGE
    """Cycle cost per kWh of throughput, in the tariff's own currency. Charged
    against the objective EMHASS maximises, so a plan only cycles when the
    price spread it is chasing clears the wear. Zero cycles freely."""
    soc_deficit_threshold: float = DEFAULT_BATTERY_SOC_DEFICIT_THRESHOLD
    soc_deficit_cost: float = DEFAULT_BATTERY_SOC_DEFICIT_COST
    soc_surplus_threshold: float = DEFAULT_BATTERY_SOC_SURPLUS_THRESHOLD
    soc_surplus_cost: float = DEFAULT_BATTERY_SOC_SURPLUS_COST
    """Dwell costs, per kWh past a threshold per hour held there -- a soft
    preference for the middle of the range, unlike the hard soc_min/soc_max
    bounds. Each half stays inert until its own cost goes above zero."""
    stress_cost: float = DEFAULT_BATTERY_STRESS_COST
    stress_segments: int = DEFAULT_BATTERY_STRESS_SEGMENTS
    """Quadratic penalty on charge/discharge power, and the piecewise-linear
    segment count EMHASS approximates that curve with."""
    self_consume_threshold_w: float = DEFAULT_SELF_CONSUME_THRESHOLD_W
    """Below this |P_grid|, the plan wants no grid exchange at all -- hand the
    battery to the inverter's own self-consumption mode instead of forcing it.
    See strategy.decide_battery."""
    no_charge_from_grid: bool = False
    """Forbids charging the battery from the grid -- it may still charge from
    excess PV, just not buy power to do it. Sent as set_nocharge_from_grid.
    EMHASS's own default is already False, so an untouched config changes
    nothing."""
    battery_first_priority: bool = False
    """Prefers draining the battery before importing from the grid, mainly
    useful on a flat tariff. Sent as set_battery_first_priority. A soft
    penalty rather than a hard constraint on EMHASS's side, so it cannot make
    a plan infeasible -- import while the battery is charged is heavily
    penalised, not forbidden."""
    dynamic_enabled: bool = False
    """Caps how fast (dis)charge power may ramp between timesteps. Sent as
    set_battery_dynamic; off by default, matching EMHASS."""
    dynamic_max: float = DEFAULT_BATTERY_DYNAMIC_MAX
    dynamic_min: float = DEFAULT_BATTERY_DYNAMIC_MIN
    """Ramp limits as a fraction of the battery's own power max, sent as
    battery_dynamic_max/battery_dynamic_min. Only meaningful once
    dynamic_enabled is on; EMHASS's own defaults (0.9 / -0.9) ride along inert
    otherwise."""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> BatteryConfig:
        data = data or {}
        return cls(
            enabled=bool(data.get("use_battery", False)),
            capacity_wh=float(data.get("capacity_wh", 0) or 0),
            charge_power_max_w=float(data.get("charge_power_max_w", 0) or 0),
            discharge_power_max_w=float(data.get("discharge_power_max_w", 0) or 0),
            soc_min=float(data.get("soc_min", DEFAULT_SOC_MIN)),
            soc_max=float(data.get("soc_max", DEFAULT_SOC_MAX)),
            soc_target=float(data.get("soc_target", DEFAULT_SOC_TARGET)),
            end_soc_mode=str(data.get(CONF_END_SOC_MODE, DEFAULT_END_SOC_MODE)),
            charge_efficiency=float(data.get("charge_efficiency", DEFAULT_CHARGE_EFFICIENCY)),
            discharge_efficiency=float(
                data.get("discharge_efficiency", DEFAULT_DISCHARGE_EFFICIENCY)
            ),
            no_discharge_to_grid=bool(data.get("no_discharge_to_grid", False)),
            weight_battery_discharge=float(
                data.get(CONF_WEIGHT_BATTERY_DISCHARGE, DEFAULT_WEIGHT_BATTERY_DISCHARGE)
            ),
            weight_battery_charge=float(
                data.get(CONF_WEIGHT_BATTERY_CHARGE, DEFAULT_WEIGHT_BATTERY_CHARGE)
            ),
            soc_deficit_threshold=float(
                data.get(CONF_BATTERY_SOC_DEFICIT_THRESHOLD, DEFAULT_BATTERY_SOC_DEFICIT_THRESHOLD)
            ),
            soc_deficit_cost=float(
                data.get(CONF_BATTERY_SOC_DEFICIT_COST, DEFAULT_BATTERY_SOC_DEFICIT_COST)
            ),
            soc_surplus_threshold=float(
                data.get(CONF_BATTERY_SOC_SURPLUS_THRESHOLD, DEFAULT_BATTERY_SOC_SURPLUS_THRESHOLD)
            ),
            soc_surplus_cost=float(
                data.get(CONF_BATTERY_SOC_SURPLUS_COST, DEFAULT_BATTERY_SOC_SURPLUS_COST)
            ),
            stress_cost=float(data.get(CONF_BATTERY_STRESS_COST, DEFAULT_BATTERY_STRESS_COST)),
            # Segment count is a solver knob, so it must reach EMHASS as an int
            # -- a float here would be a type error on a config entry that
            # round-tripped through a NumberSelector.
            stress_segments=int(
                data.get(CONF_BATTERY_STRESS_SEGMENTS, DEFAULT_BATTERY_STRESS_SEGMENTS)
            ),
            self_consume_threshold_w=float(
                data.get("self_consume_threshold_w", DEFAULT_SELF_CONSUME_THRESHOLD_W)
            ),
            no_charge_from_grid=bool(data.get("no_charge_from_grid", False)),
            battery_first_priority=bool(data.get("battery_first_priority", False)),
            dynamic_enabled=bool(data.get("battery_dynamic", False)),
            dynamic_max=float(data.get("battery_dynamic_max", DEFAULT_BATTERY_DYNAMIC_MAX)),
            dynamic_min=float(data.get("battery_dynamic_min", DEFAULT_BATTERY_DYNAMIC_MIN)),
        )


@dataclass(slots=True)
class GridConfig:
    """Grid connection limits."""

    import_max_w: float = DEFAULT_GRID_IMPORT_MAX
    export_max_w: float = DEFAULT_GRID_EXPORT_MAX
    capacity_cost_per_kw: float = DEFAULT_CAPACITY_COST_PER_KW
    """Demand charge on the highest import power over the horizon, per kW. A
    grid setting rather than a battery one -- deferrable loads shave a peak
    with no battery involved. Zero is a genuine no-op inside EMHASS."""
    compute_curtailment: bool | None = None
    """Whether EMHASS itself should optimise PV curtailment, sent as a runtime
    parameter so the answer travels with the run instead of living only in the
    add-on's stored configuration. ``None`` means the entry predates this
    setting: nothing is sent and EMHASS keeps whatever it has, which is the
    only way to avoid switching off an add-on-side setting nobody asked us to
    touch."""
    import_limit_entity: str | None = None
    export_limit_entity: str | None = None
    """Sensors read on every run whose value replaces the matching static limit
    above, for a connection whose usable limit moves. Never raises the static
    number -- that stays the physical ceiling. Held here rather than beside
    ``soc_entity`` on EmhassConfig so that everything the grid step collects
    travels together into ``payload.resolve_grid_limit``."""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> GridConfig:
        data = data or {}
        compute_curtailment = data.get(CONF_COMPUTE_CURTAILMENT)
        return cls(
            import_max_w=float(data.get("grid_import_max_w", DEFAULT_GRID_IMPORT_MAX)),
            export_max_w=float(data.get("grid_export_max_w", DEFAULT_GRID_EXPORT_MAX)),
            capacity_cost_per_kw=float(
                data.get(CONF_CAPACITY_COST_PER_KW, DEFAULT_CAPACITY_COST_PER_KW)
            ),
            import_limit_entity=data.get(CONF_GRID_IMPORT_LIMIT_ENTITY) or None,
            export_limit_entity=data.get(CONF_GRID_EXPORT_LIMIT_ENTITY) or None,
            compute_curtailment=(
                None if compute_curtailment is None else bool(compute_curtailment)
            ),
        )


@dataclass(slots=True)
class HybridInverterConfig:
    """AC-side throughput of a single inverter shared by PV and battery.

    Meaningless for a plant with two separate inverters -- one for PV, one for
    the battery -- since each then has its own, independent AC limit and
    nothing is shared. EMHASS's own `inverter_is_hybrid` gates all of this;
    disabled makes the rest inert rather than wrong, so it's safe to collect
    them from the same form regardless of the toggle.
    """

    enabled: bool = False
    ac_output_max_w: float = 0.0
    ac_input_max_w: float = 0.0
    efficiency_dc_ac: float = DEFAULT_INVERTER_EFFICIENCY
    efficiency_ac_dc: float = DEFAULT_INVERTER_EFFICIENCY

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> HybridInverterConfig:
        data = data or {}
        ac_output_max_w = float(data.get(CONF_INVERTER_AC_OUTPUT_MAX, 0) or 0)
        return cls(
            enabled=bool(data.get(CONF_HYBRID_INVERTER, False)),
            ac_output_max_w=ac_output_max_w,
            # EMHASS itself falls back to the output limit when no separate
            # input limit is configured (optimization.py's
            # _add_hybrid_inverter_constraints: "if p_nom_inverter_input is
            # None: p_nom_inverter_input = p_nom_inverter_output") -- mirrored
            # here so a blank or 0 input field means the same thing to EMHASS
            # as it does to a user who never touched it.
            ac_input_max_w=float(data.get(CONF_INVERTER_AC_INPUT_MAX, 0) or 0) or ac_output_max_w,
            efficiency_dc_ac=float(
                data.get(CONF_INVERTER_EFFICIENCY_DC_AC, DEFAULT_INVERTER_EFFICIENCY)
                or DEFAULT_INVERTER_EFFICIENCY
            ),
            efficiency_ac_dc=float(
                data.get(CONF_INVERTER_EFFICIENCY_AC_DC, DEFAULT_INVERTER_EFFICIENCY)
                or DEFAULT_INVERTER_EFFICIENCY
            ),
        )


@dataclass(slots=True)
class DeferrableLoad:
    """One deferrable load, as sent in a single optimisation request.

    A snapshot, produced by :meth:`deferrable.DeferrableRuntime.to_load`. The
    live, user-adjustable state lives in that registry; this type only ever
    describes what one request was asked to solve.

    ``earliest_start`` / ``latest_end`` are wall-clock times. EMHASS wants
    *timestep indices relative to the moment the optimisation is launched*, so
    they must be re-derived on every single request — see
    :func:`payload.window_to_timesteps`. Storing wall-clock here and converting
    late is what keeps a window from drifting as the day progresses.

    ``deadline_at`` is the other kind of end constraint: an absolute instant,
    already anchored to the moment the run was requested. The two are
    independent and both may apply — see :func:`payload.resolve_load_window`.

    ``start_at`` / ``end_at`` are a third: an absolute window derived for this
    one request rather than configured, which is how a surplus load says "only
    while the sun is actually spare" (see :mod:`surplus`). Unlike a deadline
    they are not relaxed when the run no longer fits — running outside them is
    the one thing such a load must never do.
    """

    subentry_id: str
    name: str
    nominal_power_w: float
    operating_hours: float
    minimum_power_w: float = 0.0
    earliest_start: time | None = None
    latest_end: time | None = None
    deadline_at: datetime | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    semi_continuous: bool = True
    single_constant: bool = False
    startup_penalty: float = 0.0
    max_startups: int = 0
    minimum_on_time_minutes: float = 0.0
    minimum_off_time_minutes: float = 0.0

    hours_from_surplus_budget: bool = False
    """Whether ``operating_hours`` came from a surplus budget rather than a
    user-set value.

    A surplus load's hours are ``credit_wh / nominal_w`` (see
    :mod:`surplus`) -- a continuous division that essentially never lands on
    a timestep boundary. The off-grid-hours warning in
    :func:`payload._describe` exists to flag a *user* mistake (a typed value
    that doesn't fit the step), so it is skipped here: quantising a budget is
    routine, not something to report every solve.
    """

    wants_to_run: bool = True
    """Whether this load is asking for its hours on this run.

    *Not* whether it is sent to EMHASS. Every configured load is always sent,
    so that a load's ``P_deferrable{k}`` number is a fixed property of the
    system rather than something that shifts as loads are switched off or
    requests come and go. A load that wants nothing is sent parked at zero
    operating hours instead of being left out — see ``payload._park``.
    """

    # Runtime state fed back to EMHASS so it does not re-charge a startup penalty
    # for an already-running load, nor re-schedule work already completed today.
    current_state: bool = False
    current_power_w: float = 0.0
    completed_timesteps: int = 0
    # How many whole timesteps this load has been continuously on/off,
    # fed back to EMHASS alongside minimum_on/off_time so it can enforce the
    # dwell time correctly across restarts (def_current_on/off_timesteps).
    current_on_timesteps: int = 0
    current_off_timesteps: int = 0

    thermal: ThermalConfig | None = None
    """Set only for a thermal load, whose temperature is optimised instead of
    its run time."""


@dataclass(slots=True)
class DeferrableLoadGroup:
    """A cross-load relationship: a shared power budget or mutual exclusion.

    Unlike :class:`DeferrableLoad`, a group is not itself sent as a load --
    it references other loads by the ``load_order`` position EMHASS expects
    (see :func:`payload._deferrable_load_group_settings`).
    """

    subentry_id: str
    name: str
    load_subentry_ids: tuple[str, ...]
    max_power_w: float | None = None
    mutual_exclusion: bool = False


# --- Plan models -------------------------------------------------------------


@dataclass(slots=True)
class PlanRow:
    """One timestep of an EMHASS optimisation plan.

    Units and sign conventions follow EMHASS's plan output schema (major 1):
    power in W, ``p_grid`` positive = import, ``p_batt`` positive = discharge,
    and ``soc`` a **fraction in [0, 1]** -- the CSV/JSON form, not the
    percentage that EMHASS's own published sensors carry.
    """

    timestamp: datetime
    p_load: float | None = None
    p_pv: float | None = None
    p_grid: float | None = None
    p_batt: float | None = None
    p_pv_curtailment: float | None = None
    """PV the optimiser chose not to produce, in W.

    Only present when PV curtailment is enabled on the EMHASS side. Independent
    of the battery decision: a plan can curtail while idle, and charge while not
    curtailing.
    """
    p_hybrid_inverter: float | None = None
    """AC-side power across a hybrid inverter, in W.

    Only present when ``inverter_is_hybrid`` is set on the EMHASS side. Positive
    is DC to AC -- delivered to the house or grid -- and negative is AC to DC,
    which is the battery charging from the grid across the DC bus. Not the same
    quantity as ``p_batt``: this is what crosses the inverter after PV and
    battery have been netted on the DC side.
    """
    soc: float | None = None
    unit_load_cost: float | None = None
    unit_prod_price: float | None = None
    optim_status: str | None = None
    cost: float | None = None
    deferrables: tuple[float, ...] = ()

    temperatures: dict[int, float] = field(default_factory=dict)
    """Predicted temperature per *thermal* load, keyed by deferrable number.

    A dict rather than a tuple because only thermal loads have the column
    (``predicted_temp_heater{k}``); indexing a positional sequence would shift
    every thermal load after an ordinary one."""

    @property
    def soc_percent(self) -> float | None:
        """SOC as a percentage.

        Scaling lives here and only here. Doing it at every call site is how
        consumers end up double-scaling -- the single most common bug against
        this schema, per EMHASS's own documentation.
        """
        return None if self.soc is None else self.soc * 100.0

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> PlanRow:
        deferrables: list[float] = []
        index = 0
        while (value := record.get(f"P_deferrable{index}")) is not None:
            deferrables.append(_as_float(value) or 0.0)
            index += 1

        temperatures = {
            k: temperature
            for k in range(index)
            if (temperature := _as_float(record.get(f"predicted_temp_heater{k}"))) is not None
        }

        # `costfun` decomposes into one or more cost_fun_<name> columns; the
        # meaningful figure for a plan total is their sum.
        cost_columns = [
            _as_float(value) for key, value in record.items() if key.startswith("cost_fun_")
        ]
        cost_values = [value for value in cost_columns if value is not None]

        return cls(
            timestamp=parse_utc(record["timestamp"]),
            p_load=_as_float(record.get("P_Load")),
            p_pv=_as_float(record.get("P_PV")),
            p_grid=_as_float(record.get("P_grid")),
            p_batt=_as_float(record.get("P_batt")),
            p_pv_curtailment=_as_float(record.get("P_PV_curtailment")),
            p_hybrid_inverter=_as_float(record.get("P_hybrid_inverter")),
            soc=_as_float(record.get("SOC_opt")),
            unit_load_cost=_as_float(record.get("unit_load_cost")),
            unit_prod_price=_as_float(record.get("unit_prod_price")),
            optim_status=record.get("optim_status"),
            cost=sum(cost_values) if cost_values else None,
            deferrables=tuple(deferrables),
            temperatures=temperatures,
        )


@dataclass(slots=True)
class Plan:
    """A parsed EMHASS optimisation plan."""

    generated_at: datetime
    schema_version: str
    rows: list[PlanRow] = field(default_factory=list)

    # Lookup caches. A plan is read far more often than it is built: the clock
    # fires async_update_listeners every timestep and every plan-derived entity
    # re-reads at that moment, so row_at and series() are called hundreds of
    # times per tick against rows that never change in between. Rebuilt
    # whenever ``rows`` is replaced or grows -- see :meth:`_cache`.
    _cached_for: list[PlanRow] | None = field(default=None, init=False, repr=False, compare=False)
    _timestamps: tuple[datetime, ...] = field(default=(), init=False, repr=False, compare=False)
    _series_cache: dict[str, Series] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def _cache(self) -> tuple[datetime, ...]:
        """The rows' timestamps, rebuilt only when ``rows`` changed identity or length.

        Rows are treated as immutable once a plan is parsed (nothing in this
        integration edits one in place), so identity plus length is enough to
        tell a fresh plan from the one already cached.
        """
        if self._cached_for is not self.rows or len(self._timestamps) != len(self.rows):
            self._cached_for = self.rows
            self._timestamps = tuple(row.timestamp for row in self.rows)
            self._series_cache = {}
        return self._timestamps

    @classmethod
    def from_response(cls, payload: dict[str, Any]) -> Plan | None:
        """Parse a ``/api/v1/plan`` response, or None if no run has happened."""
        if payload.get("status") != "ok" or not payload.get("plan"):
            return None
        return cls(
            generated_at=parse_utc(payload["generated_at"]),
            schema_version=str(payload.get("emhass_schema_version") or ""),
            rows=[PlanRow.from_record(record) for record in payload["plan"]],
        )

    @property
    def step(self) -> timedelta | None:
        """The plan's own timestep, taken from its first two rows."""
        if len(self.rows) < 2:
            return None
        return self.rows[1].timestamp - self.rows[0].timestamp

    def row_at(self, when: datetime) -> PlanRow | None:
        """The row whose interval contains ``when`` (hold-last semantics).

        A moment *just before* the plan starts counts as the first row. EMHASS
        aligns its horizon to the next timestep boundary after the optimisation
        is launched, so for the first minutes after every single run "now" sits
        in front of row zero -- and reporting no plan there would blank every
        plan-derived sensor on a regular cycle, for a plan that is in fact
        perfectly fresh. The cost is acting on the first row up to one timestep
        early, which is a far smaller error than having no answer at all.

        Anything earlier than that really is uncovered and still returns None.
        """
        when = when.astimezone(UTC)
        timestamps = self._cache()
        if not timestamps:
            return None

        first = self.rows[0]
        if when < first.timestamp:
            step = self.step
            if step is not None and when >= first.timestamp - step:
                return first
            return None

        position = bisect_right(timestamps, when)
        return self.rows[position - 1] if position else None

    def series(self, attribute: str) -> Series:
        """One column of the plan as a series, memoised per attribute.

        Several sensors publish the same column, and each rebuilds it on every
        update; a Series costs a sort and a dict pass to construct, which is
        wasted work for a plan that cannot change between runs.
        """
        self._cache()
        if (cached := self._series_cache.get(attribute)) is None:
            cached = self._series_cache[attribute] = Series(
                Point(row.timestamp, value)
                for row in self.rows
                if (value := getattr(row, attribute)) is not None
            )
        return cached

    def deferrable_series(self, index: int) -> Series:
        return Series(
            Point(row.timestamp, row.deferrables[index])
            for row in self.rows
            if index < len(row.deferrables)
        )

    def temperature_series(self, index: int) -> Series:
        """Predicted temperature over the horizon for one thermal load."""
        return Series(
            Point(row.timestamp, value)
            for row in self.rows
            if (value := row.temperatures.get(index)) is not None
        )

    @property
    def total_cost(self) -> float | None:
        values = [row.cost for row in self.rows if row.cost is not None]
        return sum(values) if values else None

    @property
    def horizon_end(self) -> datetime | None:
        return self.rows[-1].timestamp if self.rows else None


@dataclass(slots=True)
class LastRun:
    """A parsed ``/api/v1/last-run`` response."""

    status: str
    timestamp: datetime | None = None
    action: str | None = None
    duration_seconds: float | None = None
    emhass_version: str | None = None
    schema_version: str | None = None
    infeasible: bool | None = None
    error_message: str | None = None
    stage_times: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @classmethod
    def from_response(cls, payload: dict[str, Any]) -> LastRun:
        raw_timestamp = payload.get("timestamp")
        return cls(
            status=payload.get("status", "no-run"),
            timestamp=parse_utc(raw_timestamp) if raw_timestamp else None,
            action=payload.get("action"),
            duration_seconds=payload.get("duration_total_seconds"),
            emhass_version=payload.get("emhass_version"),
            schema_version=payload.get("schema_version"),
            infeasible=payload.get("infeasible"),
            error_message=payload.get("error_message"),
            stage_times=payload.get("stage_times") or {},
        )


# --- helpers -----------------------------------------------------------------


def parse_utc(value: Any) -> datetime:
    """Parse an ISO 8601 timestamp to an aware UTC datetime."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        # datetime.fromisoformat handles "Z" from 3.11 onwards, but EMHASS
        # timestamps are the one external input we cannot afford to misparse.
        if text.endswith(("z", "Z")):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise SeriesError(f"Timestamp without timezone: {value!r}")
    return parsed.astimezone(UTC)


def ceil_to_step(when: datetime, step: timedelta) -> datetime:
    """The grid boundary at or after ``when``, on the same grid as :meth:`Series.resampled`.

    A horizon end is ``now`` plus a whole number of timesteps, so it inherits
    whatever fraction of a second ``now`` carried and almost never lands on a
    boundary itself. Asking a grid-aligned series to cover it verbatim would
    always leave it one boundary short -- reported as a coverage warning on
    every run, and filled by EMHASS holding the last value flat -- for a
    series that in truth reaches the end of the last timestep anyone asked
    about.
    """
    size = step.total_seconds()
    if size <= 0:
        return when.astimezone(UTC)
    stamp = when.astimezone(UTC).timestamp()
    return datetime.fromtimestamp(math.ceil(stamp / size) * size, UTC)


def floor_to_step(when: datetime, step: timedelta) -> datetime:
    """The grid boundary at or before ``when``, on :meth:`Series.resampled`'s grid.

    The counterpart to :func:`ceil_to_step`, for the other side of the same
    problem. Where that one moves a *fill target* forward so a series reaches
    past an off-grid horizon, this moves the *requirement* back onto the grid:
    a point at the last boundary at or before the horizon already describes
    the timestep the horizon ends inside, so demanding a later one asks for a
    price nobody publishes and reports a shortfall of seconds as if it were
    a missing day.
    """
    size = step.total_seconds()
    if size <= 0:
        return when.astimezone(UTC)
    stamp = when.astimezone(UTC).timestamp()
    return datetime.fromtimestamp(math.floor(stamp / size) * size, UTC)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) else result

"""Data models for EMHASS Companion.

All datetimes in this module are timezone-aware and normalised to UTC. EMHASS's
plan records are explicitly UTC ("align consumers on UTC", per its
``/api/v1/plan`` schema), and forecast payloads we send carry an explicit offset,
so no part of this integration ever relies on an implicit local timezone.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
import math
from typing import Any, Self

from .const import (
    CONF_END_SOC_MODE,
    CONF_HYBRID_INVERTER,
    CONF_INVERTER_AC_INPUT_MAX,
    CONF_INVERTER_AC_OUTPUT_MAX,
    CONF_INVERTER_EFFICIENCY_AC_DC,
    CONF_INVERTER_EFFICIENCY_DC_AC,
    DEFAULT_CHARGE_EFFICIENCY,
    DEFAULT_CURTAIL_ON_NEGATIVE_PRICE,
    DEFAULT_DISCHARGE_EFFICIENCY,
    DEFAULT_END_SOC_MODE,
    DEFAULT_GRID_EXPORT_MAX,
    DEFAULT_GRID_IMPORT_MAX,
    DEFAULT_INVERTER_EFFICIENCY,
    DEFAULT_SELF_CONSUME_THRESHOLD_W,
    DEFAULT_SOC_MAX,
    DEFAULT_SOC_MIN,
    DEFAULT_SOC_TARGET,
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

    __slots__ = ("_points",)

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
        self._points = tuple(Point(when, merged[when]) for when in sorted(merged))

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
        when = when.astimezone(UTC)
        index = None
        for i, point in enumerate(self._points):
            if point.time > when:
                break
            index = i
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

    # -- queries --------------------------------------------------------------

    def value_at(self, when: datetime) -> float | None:
        """Value of the interval containing ``when`` (hold-last semantics).

        Returns ``None`` before the first point rather than extrapolating
        backwards, so callers can distinguish "no data yet" from a real zero.
        """
        when = when.astimezone(UTC)
        found: float | None = None
        for point in self._points:
            if point.time > when:
                break
            found = point.value
        return found

    def covers(self, until: datetime) -> bool:
        """Whether the series extends to ``until``.

        EMHASS hold-last-fills a timestamped forecast onto its own grid, so a
        series that stops short is *silently* extended with its final value
        instead of raising. Checking coverage here is what turns that into a
        visible warning rather than a plan quietly built on a flat-lined price.
        """
        return bool(self._points) and self.end >= until.astimezone(UTC)

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
    self_consume_threshold_w: float = DEFAULT_SELF_CONSUME_THRESHOLD_W
    """Below this |P_grid|, the plan wants no grid exchange at all -- hand the
    battery to the inverter's own self-consumption mode instead of forcing it.
    See strategy.decide_battery."""

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
            self_consume_threshold_w=float(
                data.get("self_consume_threshold_w", DEFAULT_SELF_CONSUME_THRESHOLD_W)
            ),
        )


@dataclass(slots=True)
class GridConfig:
    """Grid connection limits."""

    import_max_w: float = DEFAULT_GRID_IMPORT_MAX
    export_max_w: float = DEFAULT_GRID_EXPORT_MAX
    curtail_on_negative_price: bool = DEFAULT_CURTAIL_ON_NEGATIVE_PRICE
    """Curtail to zero export whenever the plan's sell price is negative and the
    battery has no headroom left to absorb the surplus instead. Off by default:
    it is a second optimiser competing with EMHASS's own curtailment cost
    function. See strategy.decide_curtailment."""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> GridConfig:
        data = data or {}
        return cls(
            import_max_w=float(data.get("grid_import_max_w", DEFAULT_GRID_IMPORT_MAX)),
            export_max_w=float(data.get("grid_export_max_w", DEFAULT_GRID_EXPORT_MAX)),
            curtail_on_negative_price=bool(
                data.get("curtail_on_negative_price", DEFAULT_CURTAIL_ON_NEGATIVE_PRICE)
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
        if not self.rows:
            return None

        first = self.rows[0]
        if when < first.timestamp:
            step = self.step
            if step is not None and when >= first.timestamp - step:
                return first
            return None

        found: PlanRow | None = None
        for row in self.rows:
            if row.timestamp > when:
                break
            found = row
        return found

    def series(self, attribute: str) -> Series:
        return Series(
            Point(row.timestamp, value)
            for row in self.rows
            if (value := getattr(row, attribute)) is not None
        )

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


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) else result

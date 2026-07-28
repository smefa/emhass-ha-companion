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
    DEFAULT_CHARGE_EFFICIENCY,
    DEFAULT_DISCHARGE_EFFICIENCY,
    DEFAULT_GRID_EXPORT_MAX,
    DEFAULT_GRID_IMPORT_MAX,
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
    charge_efficiency: float = DEFAULT_CHARGE_EFFICIENCY
    discharge_efficiency: float = DEFAULT_DISCHARGE_EFFICIENCY

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
            charge_efficiency=float(data.get("charge_efficiency", DEFAULT_CHARGE_EFFICIENCY)),
            discharge_efficiency=float(
                data.get("discharge_efficiency", DEFAULT_DISCHARGE_EFFICIENCY)
            ),
        )


@dataclass(slots=True)
class GridConfig:
    """Grid connection limits."""

    import_max_w: float = DEFAULT_GRID_IMPORT_MAX
    export_max_w: float = DEFAULT_GRID_EXPORT_MAX

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> GridConfig:
        data = data or {}
        return cls(
            import_max_w=float(data.get("grid_import_max_w", DEFAULT_GRID_IMPORT_MAX)),
            export_max_w=float(data.get("grid_export_max_w", DEFAULT_GRID_EXPORT_MAX)),
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
    """

    subentry_id: str
    name: str
    nominal_power_w: float
    operating_hours: float
    earliest_start: time | None = None
    latest_end: time | None = None
    semi_continuous: bool = True
    single_constant: bool = False
    startup_penalty: float = 0.0
    enabled: bool = True

    # Runtime state fed back to EMHASS so it does not re-charge a startup penalty
    # for an already-running load, nor re-schedule work already completed today.
    current_state: bool = False
    current_power_w: float = 0.0
    completed_timesteps: int = 0

    thermal: ThermalConfig | None = None
    """Set only for a thermal load, whose temperature is optimised instead of
    its run time."""


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
    soc: float | None = None
    unit_load_cost: float | None = None
    unit_prod_price: float | None = None
    optim_status: str | None = None
    cost: float | None = None
    deferrables: tuple[float, ...] = ()

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
            soc=_as_float(record.get("SOC_opt")),
            unit_load_cost=_as_float(record.get("unit_load_cost")),
            unit_prod_price=_as_float(record.get("unit_prod_price")),
            optim_status=record.get("optim_status"),
            cost=sum(cost_values) if cost_values else None,
            deferrables=tuple(deferrables),
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

    def row_at(self, when: datetime) -> PlanRow | None:
        """The row whose interval contains ``when`` (hold-last semantics)."""
        when = when.astimezone(UTC)
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

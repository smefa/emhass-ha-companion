"""Temporary battery state-of-charge reserve sent as EMHASS ``soc_floor``.

Used for storm prep or planned outage protection: the user sets a target SoC
percentage and a ``[start_at, end_at)`` window. On ``naive-mpc-optim`` runs
targeting EMHASS 0.19.0 or newer, Companion translates the active or upcoming
window into EMHASS's soft per-step ``soc_floor`` list (fractions in ``[0, 1]``
during the window, ``None`` outside it).

Steps before ``start_at`` are ``None`` so EMHASS can charge cheaply ahead of
the window; steps from ``start_at`` to ``end_at`` carry the floor fraction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil, isfinite
from typing import Any

from homeassistant.util import dt as dt_util

STATUS_OFF = "off"
STATUS_SCHEDULED = "scheduled"
STATUS_ACTIVE = "active"
STATUSES: tuple[str, ...] = (STATUS_OFF, STATUS_SCHEDULED, STATUS_ACTIVE)

MAX_REASON_LENGTH = 80


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return dt_util.as_utc(value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE))
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class BatteryReserve:
    """One scheduled or active battery reserve window."""

    target_soc_pct: float
    start_at: datetime
    end_at: datetime
    reason: str = ""

    def __post_init__(self) -> None:
        if (
            isinstance(self.target_soc_pct, bool)
            or not isinstance(self.target_soc_pct, int | float)
            or not isfinite(self.target_soc_pct)
            or not 0 < self.target_soc_pct <= 100
        ):
            raise ValueError("target_soc_pct must be between 0 and 100")
        start_utc = _ensure_utc(self.start_at)
        end_utc = _ensure_utc(self.end_at)
        if end_utc <= start_utc:
            raise ValueError("end_at must be after start_at")
        object.__setattr__(self, "target_soc_pct", float(self.target_soc_pct))
        object.__setattr__(self, "start_at", start_utc)
        object.__setattr__(self, "end_at", end_utc)
        object.__setattr__(self, "reason", str(self.reason or "").strip()[:MAX_REASON_LENGTH])

    def is_expired(self, now: datetime) -> bool:
        return _ensure_utc(now) >= self.end_at

    def is_active(self, now: datetime) -> bool:
        now_utc = _ensure_utc(now)
        return self.start_at <= now_utc < self.end_at

    def is_scheduled(self, now: datetime) -> bool:
        return _ensure_utc(now) < self.start_at

    def status(self, now: datetime) -> str:
        if self.is_expired(now):
            return STATUS_OFF
        if self.is_active(now):
            return STATUS_ACTIVE
        return STATUS_SCHEDULED

    def soc_floor_array(
        self, grid_start: datetime, step: timedelta, count: int
    ) -> list[float | None] | None:
        """Per-step EMHASS ``soc_floor`` fractions, or ``None`` if outside horizon."""
        if count <= 0:
            return None
        origin = _ensure_utc(grid_start)
        fraction = round(self.target_soc_pct / 100.0, 4)
        values: list[float | None] = []
        any_floor = False
        for index in range(count):
            step_start = origin + step * index
            step_end = step_start + step
            if step_end > self.start_at and step_start < self.end_at:
                values.append(fraction)
                any_floor = True
            else:
                values.append(None)
        return values if any_floor else None

    def to_json(self) -> dict[str, Any]:
        return {
            "target_soc_pct": self.target_soc_pct,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "reason": self.reason,
        }


def lead_in_start_time(
    start_at: datetime,
    end_at: datetime,
    *,
    step: timedelta,
    row_zero: datetime,
    current_soc: float | None,
    target_soc_pct: float,
    hours_to_charge: float,
    charge_efficiency: float = 1.0,
) -> datetime:
    """Shift an immediate start past step 0 when the battery needs to charge up.

    EMHASS 0.19.0 holds ``soc_init`` rather than charging whenever step 0
    carries a floor above the current SoC. Leaving ``None`` lead-in steps lets
    EMHASS charge up to ``target_soc_pct`` before the floor engages.
    """
    start_utc = _ensure_utc(start_at)
    end_utc = _ensure_utc(end_at)
    origin = _ensure_utc(row_zero)
    step_end = origin + step
    if start_utc >= step_end:
        return start_utc
    target_frac = target_soc_pct / 100.0
    if current_soc is None or current_soc >= target_frac:
        return start_utc
    eff = max(charge_efficiency, 0.1)
    if isfinite(hours_to_charge) and hours_to_charge > 0:
        lead_seconds = (hours_to_charge / eff) * 3600.0
        lead_steps = max(1, ceil(lead_seconds / step.total_seconds()))
    else:
        lead_steps = 1
    candidate = origin + step * lead_steps
    if candidate < end_utc:
        return candidate
    if step_end < end_utc:
        return step_end
    return start_utc


def from_json(raw: Any) -> BatteryReserve | None:
    """Restore a persisted reserve, returning ``None`` if absent or invalid."""
    if not isinstance(raw, dict):
        return None
    start_raw = raw.get("start_at")
    end_raw = raw.get("end_at")
    target_raw = raw.get("target_soc_pct")
    if not isinstance(start_raw, str) or not isinstance(end_raw, str) or target_raw is None:
        return None
    start_dt = dt_util.parse_datetime(start_raw)
    end_dt = dt_util.parse_datetime(end_raw)
    if start_dt is None or end_dt is None:
        return None
    try:
        return BatteryReserve(
            target_soc_pct=float(target_raw),
            start_at=start_dt,
            end_at=end_dt,
            reason=str(raw.get("reason", "")),
        )
    except (TypeError, ValueError):
        return None

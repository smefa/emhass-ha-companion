"""Scale a bundled reference household load shape to a user-declared average.

EMHASS's own ``load_forecast_method: typical`` ships a comparable reference
curve, grouped by month and day-of-week and averaged, but its only scale knob
is ``maximum_power_from_grid`` -- the plant's real grid import limit, a poor
stand-in for how much a household actually draws on average. This module does
the same grouping against a bundled year of real household data, but scales
the result to a value the user gives directly.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
import json
from pathlib import Path

from homeassistant.util import dt as dt_util

_REFERENCE_PATH = Path(__file__).parent / "data" / "typical_load_reference.json"
_STEP_MINUTES = 30
_SLOTS_PER_DAY = 24 * 60 // _STEP_MINUTES


@lru_cache(maxsize=1)
def _reference() -> tuple[object, list[float | None]]:
    """The bundled curve: a UTC start time and a value every 30 minutes.

    Loaded once and cached -- it's read on every dayahead and MPC run.
    """
    raw = json.loads(_REFERENCE_PATH.read_text(encoding="utf-8"))
    if raw["step_minutes"] != _STEP_MINUTES:
        raise ValueError(
            f"Reference data step is {raw['step_minutes']} min, expected {_STEP_MINUTES}"
        )
    start = dt_util.parse_datetime(raw["start"])
    if start is None:
        raise ValueError(f"Unparseable reference start timestamp: {raw['start']!r}")
    return start, raw["values"]


@lru_cache(maxsize=1)
def _reference_average_w() -> float:
    """The bundled household's own overall average power, in watts."""
    _, values = _reference()
    present = [value for value in values if value is not None]
    return sum(present) / len(present)


def typical_day_records(target_day: date, average_w: float) -> list[dict[str, object]]:
    """Half-hourly load records for one local calendar day, scaled to ``average_w``.

    Historic points are matched to ``target_day`` by local month and
    day-of-week, then averaged per half-hour slot -- the same grouping
    EMHASS's own ``typical`` method uses. Each slot's average is then scaled
    by ``average_w`` divided by the reference household's own overall average,
    so the result's mean power is exactly what the caller asked for rather
    than a side effect of an unrelated grid limit.
    """
    start, values = _reference()
    scale = average_w / _reference_average_w()
    step = timedelta(minutes=_STEP_MINUTES)

    totals = [0.0] * _SLOTS_PER_DAY
    counts = [0] * _SLOTS_PER_DAY
    for index, value in enumerate(values):
        if value is None:
            continue
        local = dt_util.as_local(start + index * step)
        if local.month != target_day.month or local.weekday() != target_day.weekday():
            continue
        slot = local.hour * 2 + (1 if local.minute >= 30 else 0)
        totals[slot] += value
        counts[slot] += 1

    # A slot with no matching historic day at all falls back to the flat
    # average rather than raising -- defensive only, the shipped reference
    # data has at least two samples in every one of its 4032 buckets.
    midnight = dt_util.start_of_local_day(target_day)
    return [
        {
            "time": (midnight + slot * step).isoformat(),
            "value": round(totals[slot] / counts[slot] * scale if counts[slot] else average_w, 1),
        }
        for slot in range(_SLOTS_PER_DAY)
    ]

"""Raise PV caution when the sun runs behind the plan.

EMHASS 0.18.4 accepts a P10 PV forecast next to P50 and blends the two at
ingestion::

    estimate = bias * P10 + (1 - bias) * P50

with ``weather_forecast_pv_quantile_bias`` as ``bias`` in ``[0, 1]``. Bias 0 is
P50 exactly; the optimizer itself never learns there were two series. Nothing
in EMHASS moves the bias, so this module does: when the sun delivered over the
last few hours falls clearly short of what the forecast promised, the rest of
the day is planned a step closer to P10.

The signal is deliberately narrow:

* Only intervals with real sun count -- planned PV above a share of the day's
  planned peak, and not the first hour after sunrise, where a few watts of
  timing error read as a large ratio.
* Curtailed intervals do not count. A full battery with export capped throws
  sun away on purpose; low output there is not a forecast miss.
* ``planned`` is P50, never the blended estimate the plan actually used. The
  controller must measure the forecast, not its own correction, or the first
  step shrinks the very gap that justified it and the bias stalls or
  oscillates.

Caution moves in small steps per run and starts every day at zero. Solcast's
automated dampening also learns from recent shortfalls, so both can react to
the same miss; small steps and a morning reset bound how far that compounds.

Pure computation; reading sensors and remembering the state between runs is the
coordinator's job.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

BIAS_MIN = 0.0
BIAS_MAX = 1.0


@dataclass(frozen=True, slots=True)
class CautionConfig:
    """Tuning knobs; the defaults are the Phase 1 backtest's starting point."""

    window: timedelta = timedelta(hours=3)
    """How far back actual PV is compared with the plan."""

    min_share: float = 0.10
    """Planned PV below this share of the day's planned peak is not real sun."""

    sunrise_skip: timedelta = timedelta(hours=1)
    """Intervals starting this soon after sunrise are ignored."""

    min_coverage: timedelta = timedelta(hours=1)
    """Hold the bias unless at least this much qualifying time is in the window."""

    behind: float = 0.85
    """Ratio below which caution steps up."""

    ahead: float = 1.0
    """Ratio at or above which caution steps back down (never below 0)."""

    step: float = 0.1
    """How far one step moves the bias."""

    min_step_interval: timedelta = timedelta(hours=1)
    """At most one step per this long, however often the companion runs.

    The window overlaps from one run to the next, so without this a run every
    five minutes would count the same shortfall a dozen times and reach full
    caution within the hour.
    """


@dataclass(frozen=True, slots=True)
class Sample:
    """One past interval: what P50 promised against what the panels made."""

    start: datetime
    duration: timedelta
    planned_w: float
    actual_w: float
    curtailed: bool = False


@dataclass(frozen=True, slots=True)
class CautionState:
    """What survives between runs: the bias and the day it belongs to."""

    day: date | None = None
    bias: float = BIAS_MIN
    ratio: float | None = None
    """Actual over planned in the last window, or ``None`` if too little sun."""

    coverage: timedelta = timedelta(0)
    """Qualifying time the last ratio was measured over."""

    stepped_at: datetime | None = None
    """When the bias last moved; ``None`` until the first step of the day."""


def window_ratio(
    samples: Iterable[Sample],
    *,
    now: datetime,
    peak_w: float,
    sunrise: datetime | None,
    config: CautionConfig,
) -> tuple[float | None, timedelta]:
    """Energy-weighted actual/planned over the qualifying part of the window.

    Returns ``(None, coverage)`` when less than ``config.min_coverage`` of the
    window had real, uncurtailed sun -- a ratio over a sliver of an hour is
    noise, and holding is the safe answer.
    """
    since = now - config.window
    floor_w = peak_w * config.min_share
    planned_wh = actual_wh = 0.0
    coverage = timedelta(0)
    for sample in samples:
        if sample.start < since or sample.start + sample.duration > now:
            continue
        if sample.curtailed or sample.planned_w <= 0 or sample.planned_w < floor_w:
            continue
        if sunrise is not None and sample.start < sunrise + config.sunrise_skip:
            continue
        hours = sample.duration.total_seconds() / 3600
        planned_wh += sample.planned_w * hours
        actual_wh += max(sample.actual_w, 0.0) * hours
        coverage += sample.duration
    if coverage < config.min_coverage or planned_wh <= 0:
        return None, coverage
    return actual_wh / planned_wh, coverage


def advance(
    state: CautionState,
    samples: Iterable[Sample],
    *,
    now: datetime,
    peak_w: float,
    sunrise: datetime | None,
    config: CautionConfig | None = None,
) -> CautionState:
    """Take one run's step from ``state``.

    The ratio is refreshed every run; the bias moves at most once per
    ``config.min_step_interval``. ``now`` must be local time: its date decides
    the daily reset, which is at local midnight (or the first run after it).
    """
    config = config or CautionConfig()
    today = now.date()
    if state.day != today:
        state = CautionState(day=today)
    ratio, coverage = window_ratio(samples, now=now, peak_w=peak_w, sunrise=sunrise, config=config)
    state = replace(state, ratio=ratio, coverage=coverage)
    if ratio is None:
        return state
    if state.stepped_at is not None and now - state.stepped_at < config.min_step_interval:
        return state
    if ratio < config.behind:
        bias = state.bias + config.step
    elif ratio >= config.ahead:
        bias = state.bias - config.step
    else:
        return state
    bias = round(min(max(bias, BIAS_MIN), BIAS_MAX), 6)
    return replace(state, bias=bias, stepped_at=now)


def blend(p50: float, p10: float, bias: float) -> float:
    """EMHASS's own ingestion blend, for callers that need the planned value."""
    bias = min(max(bias, BIAS_MIN), BIAS_MAX)
    return bias * p10 + (1 - bias) * p50

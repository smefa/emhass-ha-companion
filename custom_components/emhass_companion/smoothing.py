"""Trailing time-weighted average for a sensor read at irregular intervals.

Shared between :class:`~.sensor.NetHouseLoadSensor` (event-driven, wants its
own recorded state smoothed) and the coordinator's live-PV read (pulled once
per MPC run, wants the value it blends into the forecast smoothed the same
way) -- both average a signal that changes at its own pace, not on a fixed
clock.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta


def _time_weighted_average(
    samples: deque[tuple[datetime, float]], window_start: datetime, now: datetime
) -> float | None:
    """The mean of a piecewise-constant signal over ``[window_start, now]``.

    ``samples`` holds ``(timestamp, value)`` pairs in ascending order, each
    one holding until the next (or until ``now`` for the last). A sample
    entirely before ``window_start`` contributes nothing -- rather than
    assume what the signal was doing before we started observing it, the
    average is just taken over whatever history is actually available, which
    is shorter than the full window right after startup or a reload.
    """
    mean = 0.0
    total_weight = 0.0
    for index, (timestamp, value) in enumerate(samples):
        segment_start = max(timestamp, window_start)
        segment_end = samples[index + 1][0] if index + 1 < len(samples) else now
        segment_end = min(segment_end, now)
        if segment_end <= segment_start:
            continue
        weight = (segment_end - segment_start).total_seconds()
        total_weight += weight
        # Accumulate the mean itself rather than sum/total. The naive form
        # evaluates (value * weight) / weight for a single segment, and that
        # is not exactly `value` for every float weight: a signal sitting
        # still at 3000 W reads back as 3000.0000000000005 when the segment
        # happens to be ~12 microseconds long. Since segments are wall-clock
        # gaps, which weight lands on a lossy pair is pure chance -- so the
        # error surfaced as an intermittent, ordering-dependent failure
        # rather than a reproducible one. This form keeps a constant signal
        # exactly constant whatever the weights, and is the same value
        # mathematically.
        mean += (value - mean) * (weight / total_weight)

    if total_weight <= 0:
        return samples[-1][1] if samples else None
    return mean


class TimeWeightedAverage:
    """Tracks a piecewise-constant signal and averages it over a trailing window."""

    def __init__(self) -> None:
        self._history: deque[tuple[datetime, float]] = deque()

    def record(self, now: datetime, value: float | None, window: timedelta) -> None:
        """Add a sample, or note that none is available right now.

        ``value`` of ``None`` (the source is unavailable, unknown, or
        missing) clears the history rather than just skipping the append:
        the alternative -- silently letting the last known value's segment
        keep extending through the gap -- would mean a source that comes
        back after being offline for an hour gets that whole hour counted as
        "held at its last reading" the next time the window is averaged,
        which is a guess this class has no basis for making.
        """
        if value is None:
            self._history.clear()
            return

        self._history.append((now, value))
        window_start = now - window
        # Drop samples entirely superseded by a later one before window_start;
        # the most recent such sample is kept as the average's starting edge.
        while len(self._history) >= 2 and self._history[1][0] <= window_start:
            self._history.popleft()

    def average(self, now: datetime, window: timedelta) -> float | None:
        return _time_weighted_average(self._history, now - window, now)

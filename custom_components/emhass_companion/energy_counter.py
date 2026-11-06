"""Counter arithmetic shared by live metering and offline legacy repair."""

from __future__ import annotations

import math

RESET_FRACTION = 0.1
"""A decrease exceeding 10% starts a new counter cycle.

Smaller decreases are corrections: keep the accepted high-water reading until
the counter recovers. This is a heuristic, not proof that a device reset.
"""


def advance_counter(previous: float | None, reading: float) -> tuple[float | None, float]:
    """Return the accepted baseline and new energy for a nonnegative counter.

    Invalid readings leave the baseline untouched. The first valid reading
    establishes a baseline without counting its absolute value. A reset counts
    its post-reset reading; a correction neither counts nor lowers the baseline.
    """
    if not math.isfinite(reading) or reading < 0:
        return previous, 0.0
    if previous is None:
        return reading, 0.0
    if reading < previous:
        if reading < previous * (1 - RESET_FRACTION):
            return reading, reading
        return previous, 0.0
    return reading, reading - previous

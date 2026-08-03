"""Tests for aligning the MPC tick to clock slots.

``async_track_time_interval`` fires every N minutes from whenever the
integration happened to start -- e.g. :07, :22, :37 for a 15-minute interval
started at :07. ``_slot_trigger_kwargs`` instead produces
``async_track_time_change`` kwargs that land on the same clock marks as the
optimisation timesteps (:00, :15, :30, ...), or ``None`` when the interval
can't tile the clock evenly and the caller must fall back to the rolling
interval.
"""

from __future__ import annotations

import pytest

from custom_components.emhass_companion.schedule import _slot_trigger_kwargs


@pytest.mark.parametrize(
    ("step_minutes", "expected"),
    [
        (15, {"minute": [0, 15, 30, 45], "second": 0}),
        (5, {"minute": [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55], "second": 0}),
        (30, {"minute": [0, 30], "second": 0}),
        (60, {"minute": [0], "second": 0}),
        (120, {"hour": [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22], "minute": 0, "second": 0}),
    ],
)
def test_intervals_that_tile_the_clock_get_aligned_kwargs(step_minutes, expected):
    assert _slot_trigger_kwargs(step_minutes) == expected


@pytest.mark.parametrize("step_minutes", [0, -15, 7, 40, 90])
def test_intervals_that_cannot_tile_the_clock_fall_back_to_none(step_minutes):
    """7 doesn't divide 60; 40 divides neither 60 nor repeats hourly; 90 is
    neither a sub-hour divisor nor a whole number of hours."""
    assert _slot_trigger_kwargs(step_minutes) is None

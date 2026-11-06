"""Recorded history smoothing in the card core (runs the real JS under node)."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

CORE = (
    Path(__file__).parents[1]
    / "custom_components"
    / "emhass_companion"
    / "frontend"
    / "emhass-core.js"
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

_SCRIPT = """
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const fn = src.match(/function smoothSeries[\\s\\S]*?\\n}\\n/)[0];
const smoothSeries = new Function(fn + "; return smoothSeries;")();
const { points, windowMs, endT } = JSON.parse(process.argv[2]);
console.log(JSON.stringify(smoothSeries(points, windowMs, endT)));
"""

MIN = 60_000


def _smooth(points: list[dict], end_t: float | None) -> list[dict]:
    args = json.dumps({"points": points, "windowMs": 15 * MIN, "endT": end_t})
    out = subprocess.run(
        ["node", "-e", _SCRIPT, str(CORE), args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


def test_a_battery_that_stopped_is_not_drawn_as_still_discharging() -> None:
    # Real shape: ~1.2 kW for a while, then one 0 W point that stays the state
    # for hours. Averaging that last point with the readings before it drew a
    # flat ~1.1 kW discharge across the whole idle stretch.
    run = [{"t": i * 20_000, "v": 1200.0 + (i % 3) * 20} for i in range(60)]
    stop = run[-1]["t"] + 20_000
    points = [*run, {"t": stop, "v": 0.0}]

    smoothed = _smooth(points, stop + 4 * 60 * MIN)

    assert smoothed[-1]["v"] == 0.0


def test_jitter_inside_a_busy_stretch_is_still_smoothed() -> None:
    points = [{"t": i * 20_000, "v": 1000.0 + (400.0 if i % 2 else -400.0)} for i in range(40)]

    smoothed = _smooth(points, points[-1]["t"] + 20_000)

    middle = [p["v"] for p in smoothed[10:30]]
    assert max(middle) - min(middle) < 100


def test_a_long_held_point_in_the_middle_keeps_its_value() -> None:
    points = [
        {"t": 0, "v": 1200.0},
        {"t": 2 * MIN, "v": 1250.0},
        {"t": 4 * MIN, "v": 0.0},
        {"t": 120 * MIN, "v": 800.0},
        {"t": 122 * MIN, "v": 810.0},
    ]

    smoothed = _smooth(points, 123 * MIN)

    assert smoothed[2]["v"] == 0.0

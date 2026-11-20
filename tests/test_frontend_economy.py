"""Economy card recorder timestamp compatibility (runs the real JS under node)."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

CARD = (
    Path(__file__).parents[1] / "custom_components/emhass_companion/frontend/emhass-economy-card.js"
)
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

_SCRIPT = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const fn = src.match(/function monthlyDeltas[\s\S]*?\n}\n/)[0];
const monthlyDeltas = new Function(fn + "; return monthlyDeltas;")();
console.log(JSON.stringify(monthlyDeltas(JSON.parse(process.argv[2]))));
"""


@pytest.mark.parametrize("iso_dates", [False, True])
def test_recorder_months_produce_chart_points(iso_dates: bool) -> None:
    # Actual HA monthly response timestamps and cumulative cost totals.
    starts = [1788213600000, 1790805600000, 1793487600000]
    dates = ["2026-08-31T22:00:00Z", "2026-09-30T22:00:00Z", "2026-10-31T23:00:00Z"]
    rows = [
        {"start": date if iso_dates else start, "sum": total}
        for start, date, total in zip(starts, dates, [-113.4206, 521.9669, 1187.0618], strict=True)
    ]
    out = subprocess.run(
        ["node", "-e", _SCRIPT, str(CARD), json.dumps(rows)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    points = json.loads(out)
    assert [point["t"] for point in points] == starts[1:]
    assert [point["v"] for point in points] == pytest.approx([635.3875, 665.0949])

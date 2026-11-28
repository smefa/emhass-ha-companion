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


_TIPS_SCRIPT = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const grab = (name) => src.match(new RegExp(`function ${name}\\([\\s\\S]*?\\n}\\n`))[0];
class Node {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.style = {}; this.textContent = ""; }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(key, value) { this.attrs[key] = value; }
  querySelector(tag) { return this.children.find((c) => c.tag === tag) || null; }
}
function svg(tag, attrs, parent) {
  const node = new Node(tag);
  for (const key of Object.keys(attrs || {})) node.setAttribute(key, attrs[key]);
  if (parent) parent.appendChild(node);
  return node;
}
const mountChart = (root) => root;
const monthlyBars = new Function(
  "svg", "mountChart",
  grab("addTip") + grab("moneyText") + grab("monthlyBars") + "; return monthlyBars;",
)(svg, mountChart);
const t = Date.UTC(2026, 10, 15);
const root = monthlyBars([{ t, v: 1050.5 }], [{ t, v: 320.25 }], { locale: { language: "en" } }, "SEK");
const tips = root.children
  .filter((c) => c.querySelector("title"))
  .map((c) => [c.querySelector("title").textContent, c.attrs.y, c.attrs.height]);
const zero = monthlyBars([{ t, v: 0.001 }], [{ t, v: 0 }], { locale: { language: "en" } }, "");
const zeroTips = zero.children
  .filter((c) => c.querySelector("title"))
  .map((c) => c.querySelector("title").textContent);
console.log(JSON.stringify({ tips, zeroTips }));
"""


def test_monthly_bars_carry_hover_values() -> None:
    """Each month bar has a full-height hover column that names its value."""
    out = subprocess.run(
        ["node", "-e", _TIPS_SCRIPT, str(CARD)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result = json.loads(out)
    # Full-height hover columns, so even a zero-height bar can be hovered.
    assert result["tips"] == [
        ["Nov 2026 balance: -1050.50 SEK", 0, 78],
        ["Nov 2026 savings: 320.25 SEK", 0, 78],
    ]
    # A sub-cent cost reads 0.00, never "-0.00".
    assert result["zeroTips"] == ["Nov 2026 balance: 0.00", "Nov 2026 savings: 0.00"]

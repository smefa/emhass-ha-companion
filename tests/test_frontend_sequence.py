"""Sequence loads in the deferrable cards' view model (runs the real JS under node)."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

FRONTEND = Path(__file__).parents[1] / "custom_components" / "emhass_companion" / "frontend"
CORE = FRONTEND / "emhass-core.js"
CARDS = FRONTEND / "emhass-deferrable-cards.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

# Pulls single top-level functions out of the bundles, the same way the
# smoothing test does: the modules import each other by URL and cannot be
# required as they are.
_SCRIPT = """
const fs = require("fs");
const core = fs.readFileSync(process.argv[1], "utf8");
const cards = fs.readFileSync(process.argv[2], "utf8");
const grab = (src, name) => src.match(new RegExp("function " + name + "[\\\\s\\\\S]*?\\\\n}\\\\n"))[0];
const formatHours = (h) => h.toFixed(2) + " h";
const sequenceOf = new Function(grab(core, "sequenceOf") + "; return sequenceOf;")();
const cycleRuns = new Function(grab(core, "cycleRuns") + "; return cycleRuns;")();
const progressOf = new Function("formatHours", grab(cards, "progressOf") + "; return progressOf;")(formatHours);
const { op, args } = JSON.parse(process.argv[3]);
const fns = { sequenceOf, cycleRuns, progressOf };
console.log(JSON.stringify(fns[op](...args)));
"""

H = 3_600_000


def _run(op: str, *args):
    out = subprocess.run(
        ["node", "-e", _SCRIPT, str(CORE), str(CARDS), json.dumps({"op": op, "args": args})],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


def _iso(ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


def test_a_single_value_load_has_no_sequence() -> None:
    assert _run("sequenceOf", {"attributes": {"reason": "x"}}, {"attributes": {}}) is None


def test_the_power_box_reads_the_peak_of_the_list() -> None:
    seq = _run(
        "sequenceOf",
        {"attributes": {"power_sequence": [150, 2000, 0]}},
        {"attributes": {}},
    )
    assert seq["peak"] == 2000
    assert seq["steps"] == 3
    assert seq["step"] is None


def test_a_zero_watt_stage_stays_one_run() -> None:
    """windows() splits at every 0 W point; the cycle blocks keep one wash one run."""
    points = [
        {"t": 0, "v": 150},
        {"t": H, "v": 2000},
        {"t": 2 * H, "v": 0},
        {"t": 3 * H, "v": 1000},
        {"t": 4 * H, "v": 0},
    ]
    runs = _run("cycleRuns", [{"start": _iso(0), "end": _iso(4 * H)}], points)
    assert runs == [{"start": 0, "end": 4 * H, "watts": 2000}]


def test_progress_comes_from_the_cycle_while_one_runs() -> None:
    view = {
        "sequence": {"step": 3, "steps": 6, "doneToday": False},
        "ranToday": 0.5,
        "needed": 1.5,
    }
    assert _run("progressOf", view) == {"v": "Step 3", "s": "of 6"}


def test_a_finished_cycle_reads_done_not_time_left() -> None:
    view = {
        "sequence": {"step": None, "steps": 6, "doneToday": True},
        "ranToday": 0.75,
        "needed": 1.5,
    }
    assert _run("progressOf", view) == {"v": "Done", "s": "cycle finished today"}


def test_a_single_value_load_keeps_measured_progress() -> None:
    view = {"sequence": None, "ranToday": 0.5, "needed": 1.5}
    assert _run("progressOf", view) == {"v": "0.50 h", "s": "of 1.50 h"}


def test_done_today_comes_from_the_backend_flag() -> None:
    seq = _run(
        "sequenceOf",
        {"attributes": {"power_sequence": [150, 2000], "sequence_done": True}},
        {"attributes": {}},
    )
    assert seq["doneToday"] is True
    running = _run(
        "sequenceOf",
        {
            "attributes": {
                "power_sequence": [150, 2000],
                "sequence_done": True,
                "sequence_step": 1,
                "sequence_steps": 4,
            }
        },
        {"attributes": {}},
    )
    assert running["doneToday"] is False

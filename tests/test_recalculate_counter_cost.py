"""Offline legacy repair previews never mutate source exports or Home Assistant."""

from __future__ import annotations

import csv
from datetime import date
import importlib.util
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

_PATH = Path(__file__).parent.parent / "scripts" / "recalculate_counter_cost.py"
_spec = importlib.util.spec_from_file_location("recalculate_counter_cost", _PATH)
repair = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = repair
_spec.loader.exec_module(repair)

DAY = date(2026, 11, 1)
ZONE = ZoneInfo("Europe/Stockholm")


def _csv(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(fields)
        writer.writerows(rows)
    return path


@pytest.fixture
def exports(tmp_path):
    energy = _csv(
        tmp_path / "energy.csv",
        ["entity_id", "state", "last_changed"],
        [
            ["sensor.import", 19.424, "2026-11-01T13:28:12.624Z"],
            ["sensor.import", 19.438, "2026-11-01T13:28:33.580Z"],
            ["sensor.import", 19.433, "2026-11-01T13:28:33.608Z"],
            ["sensor.import", "unavailable", "2026-11-01T13:28:40Z"],
            ["sensor.import", 19.446, "2026-11-01T13:28:47.599Z"],
            ["sensor.import", 19.463, "2026-11-01T13:29:17.980Z"],
            ["sensor.import", 19.470, "2026-11-01T13:30:00Z"],
        ],
    )
    cost = _csv(
        tmp_path / "cost.csv",
        ["entity_id", "state", "last_changed", "extra"],
        [
            ["sensor.cost", "52.0000", "2026-11-01T13:28:12.628Z", "keep"],
            ["sensor.cost", "108.4833", "2026-11-01T13:29:17.982Z", "keep"],
            ["sensor.cost", "1.2345", "2026-11-02T00:00:00Z", "other day"],
        ],
    )
    prices = _csv(
        tmp_path / "prices.csv",
        ["start", "end", "buy"],
        [
            ["2026-11-01T13:00:00Z", "2026-11-01T14:00:00Z", "2.9"],
        ],
    )
    return energy, cost, prices


def _args(energy):
    return ["--energy", str(energy), "--day", "2026-11-01", "--timezone", "Europe/Stockholm"]


def test_dry_run_reports_energy_without_inventing_a_tariff(exports, capsys):
    energy, _, _ = exports
    before = energy.read_bytes()
    assert repair.main(_args(energy)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["excess_import_kwh"] == pytest.approx(19.438)
    assert report["excess_import_cost"] is None
    assert report["home_assistant_modified"] is False
    assert energy.read_bytes() == before


def test_explicit_export_preserves_inputs_other_days_and_columns(exports, tmp_path, capsys):
    energy, cost, prices = exports
    before = [path.read_bytes() for path in exports]
    output = tmp_path / "corrected.csv"
    args = [
        *_args(energy),
        "--cost",
        str(cost),
        "--prices",
        str(prices),
        "--output",
        str(output),
        "--confirm-legacy-tracking",
    ]
    assert repair.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["excess_import_cost"] == pytest.approx(19.438 * 2.9)
    with output.open() as stream:
        rows = list(csv.DictReader(stream))
    assert [row["state"] for row in rows] == ["52.0000", "52.1131", "1.2345"]
    assert [row["extra"] for row in rows] == ["keep", "keep", "other day"]
    assert [path.read_bytes() for path in exports] == before
    with pytest.raises(SystemExit):
        repair.main(args)
    assert [path.read_bytes() for path in exports] == before


@pytest.mark.parametrize("confirmed,priced", [(False, True), (True, False)])
def test_export_requires_tariffs_and_confirmation(exports, tmp_path, confirmed, priced):
    energy, cost, prices = exports
    output = tmp_path / "no.csv"
    args = [*_args(energy), "--cost", str(cost), "--output", str(output)]
    if confirmed:
        args.append("--confirm-legacy-tracking")
    if priced:
        args += ["--prices", str(prices)]
    with pytest.raises(SystemExit):
        repair.main(args)
    assert not output.exists()


@pytest.mark.parametrize(
    "rows",
    [
        [["2026-11-01T14:00:00Z", "2026-11-01T15:00:00Z", 2]],
        [["2026-11-01T13:28:33.600Z", "2026-11-01T14:00:00Z", 2]],
        [["2026-11-01T13:00:00Z", "2026-11-01T14:00:00Z", "nan"]],
        [
            ["2026-11-01T13:00:00Z", "2026-11-01T14:00:00Z", 2],
            ["2026-11-01T13:00:00Z", "2026-11-01T14:00:00Z", 3],
        ],
    ],
)
def test_missing_boundary_straddling_invalid_and_overlapping_tariffs_fail_closed(exports, rows):
    energy, _, prices = exports
    _csv(prices, ["start", "end", "buy"], rows)
    with pytest.raises(SystemExit):
        repair.main([*_args(energy), "--prices", str(prices)])


def test_zero_and_negative_tariffs_are_valid(exports):
    energy, _, prices = exports
    _, samples = repair.read_history(energy)
    excess = repair.counter_excess(samples, DAY, ZONE, 1)
    for price in (0, -1):
        _csv(
            prices,
            ["start", "end", "buy"],
            [["2026-11-01T13:00:00Z", "2026-11-01T14:00:00Z", price]],
        )
        assert sum(repair.price_excess(excess, prices)) == pytest.approx(19.438 * price)


def test_daily_reset_is_not_an_excess_and_other_days_are_excluded(tmp_path):
    path = _csv(
        tmp_path / "energy.csv",
        ["entity_id", "state", "last_changed"],
        [
            ["sensor.import", 24.335, "2026-10-31T23:00:00Z"],
            ["sensor.import", 0.002, "2026-10-31T23:00:02.931Z"],
            ["sensor.import", 19.438, "2026-11-01T13:00:00Z"],
            ["sensor.import", 19.433, "2026-11-01T13:01:00Z"],
            ["sensor.import", 0, "2026-11-01T23:00:00Z"],
            ["sensor.import", 1, "2026-11-02T13:00:00Z"],
            ["sensor.import", 0.999, "2026-11-02T13:01:00Z"],
        ],
    )
    _, samples = repair.read_history(path)
    excess = repair.counter_excess(samples, DAY, ZONE, 1)
    assert len(excess) == 1
    assert excess[0].kwh == pytest.approx(19.433)


@pytest.mark.parametrize(
    "rows",
    [
        [["sensor.import", 1, "2026-11-01T13:00:00"]],
        [["sensor.import", 1, "2026-11-01T13:00:00Z"], ["sensor.other", 2, "2026-11-01T13:01:00Z"]],
        [
            ["sensor.import", 1, "2026-11-01T13:00:00Z"],
            ["sensor.import", 2, "2026-11-01T13:00:00Z"],
        ],
    ],
)
def test_ambiguous_history_is_rejected(tmp_path, rows):
    path = _csv(tmp_path / "bad.csv", ["entity_id", "state", "last_changed"], rows)
    with pytest.raises(ValueError):
        repair.read_history(path)


def test_cost_and_energy_coverage_are_required(exports):
    energy, cost, prices = exports
    _, samples = repair.read_history(energy)
    _, costs = repair.read_history(cost)
    excess = repair.counter_excess(samples, DAY, ZONE, 1)
    amounts = repair.price_excess(excess, prices)
    with pytest.raises(ValueError, match="bracket"):
        repair.corrected_rows(costs[1:], samples, excess, amounts, DAY, ZONE)
    with pytest.raises(ValueError, match="cover"):
        repair.corrected_rows(costs, samples[:-2], excess, amounts, DAY, ZONE)

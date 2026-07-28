"""Tests for the core data models."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.emhass_companion.models import (
    LastRun,
    Plan,
    Point,
    Series,
    SeriesError,
    parse_utc,
)

T0 = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)


def _series(*values: float, step_minutes: int = 30) -> Series:
    return Series(
        Point(T0 + timedelta(minutes=step_minutes * i), value) for i, value in enumerate(values)
    )


# --- Series ------------------------------------------------------------------


def test_series_sorts_and_deduplicates_with_later_winning():
    series = Series(
        [
            Point(T0 + timedelta(minutes=30), 2.0),
            Point(T0, 1.0),
            Point(T0, 9.0),
        ]
    )
    assert len(series) == 2
    assert series.values == (9.0, 2.0)
    assert series.start == T0


def test_series_normalises_offsets_to_utc():
    """Points arriving with different offsets must compare and sort correctly."""
    series = Series(
        [
            Point(parse_utc("2026-07-28T12:00:00+02:00"), 1.0),  # 10:00 UTC
            Point(parse_utc("2026-07-28T10:30:00Z"), 2.0),
        ]
    )
    assert series.start == T0
    assert series.values == (1.0, 2.0)


def test_naive_datetimes_are_rejected():
    with pytest.raises(SeriesError, match="Naive"):
        Series([Point(datetime(2026, 7, 28, 10, 0), 1.0)])


def test_value_at_holds_the_last_value():
    series = _series(1.0, 2.0)
    assert series.value_at(T0 + timedelta(minutes=10)) == 1.0
    assert series.value_at(T0 + timedelta(minutes=45)) == 2.0


def test_value_at_returns_none_before_the_series_starts():
    """ "No data yet" must be distinguishable from a real zero."""
    assert _series(1.0).value_at(T0 - timedelta(minutes=1)) is None


def test_covers_detects_a_short_series():
    series = _series(1.0, 2.0)
    assert series.covers(T0 + timedelta(minutes=30))
    assert not series.covers(T0 + timedelta(hours=5))


def test_step_finds_the_dominant_spacing():
    assert _series(1.0, 2.0, 3.0, step_minutes=15).step() == timedelta(minutes=15)
    assert Series.empty().step() is None


def test_to_payload_uses_explicit_offsets():
    """EMHASS reads a timestamp without an offset as its own local time."""
    payload = _series(1.0).to_payload()
    assert list(payload) == ["2026-07-28T10:00:00+00:00"]


def test_scaled_applies_factor_then_offset():
    assert _series(2.0).scaled(10, 1).values == (21.0,)


def test_empty_series_is_falsy_and_has_no_bounds():
    empty = Series.empty()
    assert not empty
    with pytest.raises(SeriesError):
        _ = empty.start


# --- parse_utc ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "2026-07-28T10:00:00Z",  # EMHASS plan records use the Z suffix
        "2026-07-28T10:00:00+00:00",
        "2026-07-28T12:00:00+02:00",
    ],
)
def test_parse_utc_accepts_iso_forms(raw):
    assert parse_utc(raw) == T0


def test_parse_utc_rejects_a_timestamp_without_a_zone():
    with pytest.raises(SeriesError, match="without timezone"):
        parse_utc("2026-07-28T10:00:00")


# --- Plan --------------------------------------------------------------------

PLAN_RESPONSE = {
    "status": "ok",
    "generated_at": "2026-07-28T10:00:00Z",
    "emhass_schema_version": "1.0",
    "plan": [
        {
            "timestamp": "2026-07-28T10:00:00Z",
            "P_Load": 2826.0,
            "P_PV": 8024.0,
            "P_grid": -1138.48,
            "P_batt": 0.0,
            "SOC_opt": 0.098,
            "unit_load_cost": 0.84,
            "unit_prod_price": 0.04,
            "optim_status": "Optimal",
            "cost_fun_profit": -0.24,
            "P_deferrable0": 0.0,
            "P_deferrable1": 6176.0,
            "P_deferrable2": 0.0,
        },
        {
            "timestamp": "2026-07-28T10:30:00Z",
            "P_Load": 2900.0,
            "P_PV": 7800.0,
            "P_grid": -900.0,
            "P_batt": -500.0,
            "SOC_opt": 0.12,
            "cost_fun_profit": -0.18,
            "P_deferrable0": 0.0,
            "P_deferrable1": 6176.0,
            "P_deferrable2": 0.0,
        },
    ],
}


def test_plan_parses_records():
    plan = Plan.from_response(PLAN_RESPONSE)
    assert plan is not None
    assert len(plan.rows) == 2
    assert plan.rows[0].p_load == 2826.0
    assert plan.rows[0].optim_status == "Optimal"


def test_soc_is_a_fraction_and_scales_once():
    """The single most common bug against this schema is double-scaling SOC."""
    plan = Plan.from_response(PLAN_RESPONSE)
    row = plan.rows[0]
    assert row.soc == 0.098
    assert row.soc_percent == pytest.approx(9.8)


def test_deferrable_columns_are_discovered_dynamically():
    plan = Plan.from_response(PLAN_RESPONSE)
    assert plan.rows[0].deferrables == (0.0, 6176.0, 0.0)
    assert plan.deferrable_series(1).values == (6176.0, 6176.0)


def test_cost_columns_are_summed():
    row = Plan.from_response(PLAN_RESPONSE).rows[0]
    assert row.cost == pytest.approx(-0.24)
    assert Plan.from_response(PLAN_RESPONSE).total_cost == pytest.approx(-0.42)


def test_row_at_uses_hold_last_semantics():
    plan = Plan.from_response(PLAN_RESPONSE)
    row = plan.row_at(datetime(2026, 7, 28, 10, 20, tzinfo=UTC))
    assert row.p_load == 2826.0


def test_no_run_yields_no_plan():
    assert Plan.from_response({"status": "no-run", "generated_at": None, "plan": None}) is None


def test_grid_sign_convention_is_preserved():
    """Positive is import, negative is export; flipping it inverts every decision."""
    plan = Plan.from_response(PLAN_RESPONSE)
    assert plan.rows[0].p_grid < 0  # exporting
    assert plan.rows[1].p_batt < 0  # charging


# --- LastRun -----------------------------------------------------------------


def test_last_run_parses_an_infeasible_result():
    last_run = LastRun.from_response(
        {
            "status": "infeasible",
            "timestamp": "2026-07-28T10:00:00Z",
            "action": "naive-mpc-optim",
            "infeasible": True,
            "error_message": None,
            "duration_total_seconds": 1.5,
            "emhass_version": "0.17.9",
            "schema_version": "1.0",
            "stage_times": {"pv_forecast": 1.2},
        }
    )
    assert not last_run.ok
    assert last_run.infeasible is True
    assert last_run.stage_times["pv_forecast"] == 1.2


def test_last_run_handles_never_having_run():
    last_run = LastRun.from_response({"status": "no-run", "timestamp": None})
    assert not last_run.ok
    assert last_run.timestamp is None

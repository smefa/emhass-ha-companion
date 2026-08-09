"""Tests for the core data models."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.emhass_companion.config_flow import _battery_storage_from_input
from custom_components.emhass_companion.const import DEFAULT_INVERTER_EFFICIENCY
from custom_components.emhass_companion.models import (
    BatteryConfig,
    GridConfig,
    HybridInverterConfig,
    LastRun,
    Plan,
    PlanRow,
    Point,
    Series,
    SeriesError,
    parse_utc,
)

T0 = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)


# --- HybridInverterConfig ------------------------------------------------------


def test_hybrid_inverter_config_defaults_to_disabled():
    config = HybridInverterConfig.from_dict(None)
    assert config.enabled is False
    assert config.efficiency_dc_ac == 0.97
    assert config.efficiency_ac_dc == 0.97


def test_ac_input_max_falls_back_to_ac_output_max_when_blank():
    """Mirrors EMHASS's own fallback in optimization.py's
    _add_hybrid_inverter_constraints: a plant with no separate input limit is
    assumed symmetric, not zero-capacity."""
    config = HybridInverterConfig.from_dict(
        {"hybrid_inverter": True, "inverter_ac_output_max_w": 5000}
    )
    assert config.ac_input_max_w == 5000


def test_ac_input_max_falls_back_when_explicitly_zero():
    config = HybridInverterConfig.from_dict(
        {
            "hybrid_inverter": True,
            "inverter_ac_output_max_w": 5000,
            "inverter_ac_input_max_w": 0,
        }
    )
    assert config.ac_input_max_w == 5000


def test_ac_input_max_is_kept_when_genuinely_different():
    config = HybridInverterConfig.from_dict(
        {
            "hybrid_inverter": True,
            "inverter_ac_output_max_w": 5000,
            "inverter_ac_input_max_w": 6000,
        }
    )
    assert config.ac_input_max_w == 6000


@pytest.mark.parametrize("stored_efficiency", [None, 0, 0.0])
def test_efficiency_falls_back_to_the_default_when_blank_or_zero(stored_efficiency):
    """A 0 efficiency would mean 100% conversion loss -- clearly never what a
    user meant by leaving the field unset."""
    data = {"hybrid_inverter": True}
    if stored_efficiency is not None:
        data["inverter_efficiency_dc_ac"] = stored_efficiency
        data["inverter_efficiency_ac_dc"] = stored_efficiency
    config = HybridInverterConfig.from_dict(data)
    assert config.efficiency_dc_ac == DEFAULT_INVERTER_EFFICIENCY
    assert config.efficiency_ac_dc == DEFAULT_INVERTER_EFFICIENCY


def test_efficiency_is_kept_when_genuinely_set():
    config = HybridInverterConfig.from_dict(
        {"inverter_efficiency_dc_ac": 0.97, "inverter_efficiency_ac_dc": 0.98}
    )
    assert config.efficiency_dc_ac == 0.97
    assert config.efficiency_ac_dc == 0.98


# --- BatteryConfig -------------------------------------------------------------


def test_battery_cycle_costs_default_to_priced_discharge_and_free_charge():
    """Discharge carries the shipped throughput cost; charge stays free so the
    same cycle is not paid for twice (it is already bought at the import price)."""
    config = BatteryConfig.from_dict({"use_battery": True, "capacity_wh": 25600})
    assert config.weight_battery_discharge == 0.02
    assert config.weight_battery_charge == 0.0


def test_battery_cycle_costs_are_read_from_stored_options():
    config = BatteryConfig.from_dict(
        {
            "use_battery": True,
            "weight_battery_discharge": 0.06,
            "weight_battery_charge": 0.01,
        }
    )
    assert config.weight_battery_discharge == 0.06
    assert config.weight_battery_charge == 0.01


def test_battery_cycle_costs_survive_the_form_round_trip():
    """The battery step stores its values verbatim except for the SOC percents,
    so a cost typed into the form must come back out unscaled."""
    stored = _battery_storage_from_input(
        {
            "use_battery": True,
            "weight_battery_discharge": 0.06,
            "weight_battery_charge": 0.01,
            "soc_min": 10,
        }
    )
    assert stored["weight_battery_discharge"] == 0.06
    config = BatteryConfig.from_dict(stored)
    assert config.weight_battery_discharge == 0.06
    assert config.weight_battery_charge == 0.01
    # The percent fields still convert; these two must not have been caught up in it.
    assert config.soc_min == 0.10


def test_battery_soc_and_stress_defaults():
    """The deficit pair ships live -- it keeps the plan off the soc_min floor
    without ever making a problem infeasible. The surplus pair and the stress
    cost stay at EMHASS's own inert defaults."""
    config = BatteryConfig.from_dict({"use_battery": True})
    assert config.soc_deficit_threshold == 0.10
    assert config.soc_deficit_cost == 0.05
    assert config.soc_surplus_threshold == 0.90
    assert config.soc_surplus_cost == 0.0
    assert config.stress_cost == 0.0
    assert config.stress_segments == 10


def test_stress_segments_is_coerced_to_int():
    """A NumberSelector hands back a float, but EMHASS uses this as a count."""
    config = BatteryConfig.from_dict({"use_battery": True, "battery_stress_segments": 16.0})
    assert config.stress_segments == 16
    assert isinstance(config.stress_segments, int)


def test_soc_comfort_thresholds_convert_from_percent_on_the_form_path():
    """They join soc_min/soc_max in _SOC_PERCENT_FIELDS, so a threshold typed as
    30% has to reach EMHASS as 0.30 -- while the costs beside them do not scale."""
    stored = _battery_storage_from_input(
        {
            "use_battery": True,
            "battery_soc_deficit_threshold": 30,
            "battery_soc_deficit_cost": 0.02,
            "battery_soc_surplus_threshold": 85,
            "battery_soc_surplus_cost": 0.03,
        }
    )
    assert stored["battery_soc_deficit_threshold"] == 0.30
    assert stored["battery_soc_surplus_threshold"] == 0.85
    assert stored["battery_soc_deficit_cost"] == 0.02
    config = BatteryConfig.from_dict(stored)
    assert config.soc_deficit_threshold == 0.30
    assert config.soc_surplus_threshold == 0.85
    assert config.soc_surplus_cost == 0.03


def test_grid_capacity_charge_defaults_to_zero():
    assert GridConfig.from_dict({}).capacity_cost_per_kw == 0.0


def test_grid_capacity_charge_is_read_from_stored_options():
    assert GridConfig.from_dict({"capacity_cost_per_kw": 45.0}).capacity_cost_per_kw == 45.0


def test_compute_curtailment_is_unset_for_an_entry_that_predates_it():
    """None, not False: an entry saved before this setting existed must leave
    the add-on's own compute_curtailment alone rather than switch it off."""
    assert GridConfig.from_dict({}).compute_curtailment is None


def test_compute_curtailment_is_read_from_stored_options():
    assert GridConfig.from_dict({"compute_curtailment": True}).compute_curtailment is True
    assert GridConfig.from_dict({"compute_curtailment": False}).compute_curtailment is False


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


def test_extended_with_previous_day_is_a_no_op_when_already_covering():
    series = _series(1.0, 2.0)
    extended = series.extended_with_previous_day(T0 + timedelta(minutes=30))
    assert extended is series


def test_extended_with_previous_day_is_a_no_op_on_an_empty_series():
    assert not Series.empty().extended_with_previous_day(T0)


def test_extended_with_previous_day_repeats_yesterdays_shape():
    """A 24h series ending at local midnight (today's Nord Pool prices, no
    tomorrow yet) should have tomorrow filled with today's own price shape,
    hour-for-hour, not flat-lined at the last (typically cheap, late-night)
    price."""
    today = [10.0, 20.0, 30.0] + [0.1] * 21  # last hour of the day is cheap
    series = _series(*today, step_minutes=60)
    until = T0 + timedelta(hours=27)  # 3h into tomorrow
    extended = series.extended_with_previous_day(until)
    assert extended.covers(until)
    # Tomorrow's first three hours repeat today's first three hours, not
    # today's flat, cheap tail.
    assert extended.values[24:27] == (10.0, 20.0, 30.0)


def test_extended_with_previous_day_falls_back_to_hold_last_without_history():
    """Less than a day of data to begin with -- nothing 24h back to copy, so
    it degrades to the old hold-last behaviour instead of guessing."""
    series = _series(1.0, 2.0, step_minutes=60)  # only 1h of history
    until = T0 + timedelta(hours=3)
    extended = series.extended_with_previous_day(until)
    assert extended.covers(until)
    assert extended.values == (1.0, 2.0, 2.0, 2.0)


def test_to_payload_uses_explicit_offsets():
    """EMHASS reads a timestamp without an offset as its own local time."""
    payload = _series(1.0).to_payload()
    assert list(payload) == ["2026-07-28T10:00:00+00:00"]


def test_scaled_applies_factor_then_offset():
    assert _series(2.0).scaled(10, 1).values == (21.0,)


def test_blend_at_beta_zero_keeps_the_forecast():
    series = _series(100.0, 200.0)
    assert series.blend_at(T0, 9000.0, beta=0).values == (100.0, 200.0)


def test_blend_at_beta_one_uses_only_the_live_value():
    series = _series(100.0, 200.0)
    assert series.blend_at(T0, 9000.0, beta=1).values == (9000.0, 200.0)


def test_blend_at_beta_half_splits_evenly():
    series = _series(100.0, 200.0)
    assert series.blend_at(T0, 300.0, beta=0.5).values == (200.0, 200.0)


def test_blend_at_touches_only_the_point_covering_when():
    series = _series(100.0, 200.0, 300.0)
    blended = series.blend_at(T0, 0.0, beta=1)
    assert blended.values == (0.0, 200.0, 300.0)
    assert blended.start == series.start
    assert blended.end == series.end


def test_blend_at_touches_the_current_point_not_the_series_start():
    """The bug this guards: a series starting well before ``when`` (e.g. a PV
    forecast starting at local midnight, blended hours later) must have its
    *current* point corrected, not its earliest one.
    """
    series = _series(100.0, 200.0, 300.0)
    when = T0 + timedelta(minutes=30)  # covers the second point, not the first
    blended = series.blend_at(when, 0.0, beta=1)
    assert blended.values == (100.0, 0.0, 300.0)


def test_blend_at_before_every_point_is_a_no_op():
    series = _series(100.0, 200.0)
    when = T0 - timedelta(minutes=30)
    assert series.blend_at(when, 9000.0, beta=1).values == (100.0, 200.0)


def test_blend_at_on_an_empty_series_is_a_no_op():
    blended = Series.empty().blend_at(T0, 9000.0, beta=1)
    assert not blended
    assert blended.values == ()


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


# --- plan lookup across the launch-to-boundary gap ----------------------------


def _plan_rows(start, count, step_minutes=15):
    from custom_components.emhass_companion.models import PlanRow

    return [
        PlanRow(timestamp=start + timedelta(minutes=step_minutes * i), deferrables=[float(i)])
        for i in range(count)
    ]


def test_a_moment_just_before_the_plan_starts_uses_the_first_row():
    """EMHASS aligns its horizon to the next timestep boundary after launch, so
    "now" sits in front of row zero for the first minutes after every run.

    Reporting no plan there blanks every plan-derived sensor on a regular
    cycle, for a plan that is in fact perfectly fresh.
    """
    start = datetime(2026, 7, 31, 12, 30, tzinfo=UTC)
    plan = Plan(generated_at=start, schema_version="1.0", rows=_plan_rows(start, 4))
    row = plan.row_at(datetime(2026, 7, 31, 12, 27, 16, tzinfo=UTC))
    assert row is not None
    assert row.timestamp == start


def test_a_moment_more_than_one_step_early_is_still_uncovered():
    start = datetime(2026, 7, 31, 12, 30, tzinfo=UTC)
    plan = Plan(generated_at=start, schema_version="1.0", rows=_plan_rows(start, 4))
    assert plan.row_at(datetime(2026, 7, 31, 12, 10, tzinfo=UTC)) is None


def test_hold_last_semantics_are_unchanged_inside_the_plan():
    start = datetime(2026, 7, 31, 12, 30, tzinfo=UTC)
    plan = Plan(generated_at=start, schema_version="1.0", rows=_plan_rows(start, 4))
    row = plan.row_at(datetime(2026, 7, 31, 12, 52, tzinfo=UTC))
    assert row is not None
    assert row.timestamp == datetime(2026, 7, 31, 12, 45, tzinfo=UTC)


def test_a_single_row_plan_has_no_step_to_reach_back_with():
    start = datetime(2026, 7, 31, 12, 30, tzinfo=UTC)
    plan = Plan(generated_at=start, schema_version="1.0", rows=_plan_rows(start, 1))
    assert plan.step is None
    assert plan.row_at(datetime(2026, 7, 31, 12, 29, tzinfo=UTC)) is None


def test_predicted_temperatures_are_parsed_per_thermal_load():
    """Only thermal loads have the column, so the mapping is by deferrable
    number -- a positional sequence would shift every thermal load that sits
    after an ordinary one."""
    record = {
        "timestamp": "2026-07-28T10:00:00+00:00",
        "P_deferrable0": 0.0,
        "P_deferrable1": 3000.0,
        "predicted_temp_heater1": 20.42,
    }
    row = PlanRow.from_record(record)
    assert row.temperatures == {1: 20.42}
    assert 0 not in row.temperatures


def test_temperature_series_reads_one_thermal_loads_trajectory():
    rows = [
        PlanRow(
            timestamp=datetime(2026, 7, 28, 10, 0, tzinfo=UTC) + timedelta(minutes=30 * i),
            deferrables=(0.0, 3000.0),
            temperatures={1: 20.0 + i},
        )
        for i in range(3)
    ]
    plan = Plan(
        generated_at=datetime(2026, 7, 28, 10, 0, tzinfo=UTC), schema_version="1.0", rows=rows
    )
    assert plan.temperature_series(1).values == (20.0, 21.0, 22.0)
    assert not plan.temperature_series(0)

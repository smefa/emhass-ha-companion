"""Extra EMHASS rows for the later windows of a daily load."""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from custom_components.emhass_companion.const import ACTION_DAYAHEAD, ACTION_MPC
from custom_components.emhass_companion.models import (
    BatteryConfig,
    DeferrableLoad,
    GridConfig,
    HybridInverterConfig,
)
from custom_components.emhass_companion.payload import PayloadInputs, build_payload

pytestmark = pytest.mark.usefixtures("stockholm_timezone")

# 12:00 Stockholm on 2026-07-28; half-hour steps.
NOW = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)

PER_LOAD_KEYS = (
    "nominal_power_of_deferrable_loads",
    "minimum_power_of_deferrable_loads",
    "operating_hours_of_each_deferrable_load",
    "start_timesteps_of_each_deferrable_load",
    "end_timesteps_of_each_deferrable_load",
    "treat_deferrable_load_as_semi_cont",
    "set_deferrable_load_single_constant",
    "set_deferrable_startup_penalty",
    "set_deferrable_max_startups",
    "def_minimum_on_time",
    "def_minimum_off_time",
    "def_current_state",
    "def_current_operating_timesteps",
    "def_current_on_timesteps",
    "def_current_off_timesteps",
    "deferrable_load_max_cost",
    "operating_timesteps_of_each_deferrable_load",
    "is_electric_load",
    "cost_forecast_per_deferrable_load",
    "def_load_config",
)


def _load(subentry_id="washer", **overrides) -> DeferrableLoad:
    defaults = {
        "subentry_id": subentry_id,
        "name": subentry_id.title(),
        "nominal_power_w": 2000,
        "operating_hours": 2,
        "earliest_start": time(14),
        "latest_end": time(18),
        "repeats_daily": True,
    }
    return DeferrableLoad(**{**defaults, **overrides})


def _build(loads, horizon_steps=96, groups=(), grid=None, action=ACTION_MPC, now=NOW):
    return build_payload(
        PayloadInputs(
            action=action,
            now=now,
            time_step_minutes=30,
            horizon_steps=horizon_steps,
            battery=BatteryConfig(),
            grid=grid or GridConfig(),
            hybrid_inverter=HybridInverterConfig(),
            backend_version="0.18.8",
            loads=loads,
            load_groups=list(groups),
        )
    )


def test_second_window_becomes_an_appended_row():
    plain = DeferrableLoad(subentry_id="pump", name="Pump", nominal_power_w=500, operating_hours=1)
    result = _build([_load(), plain])
    payload = result.payload

    assert result.load_order == ["washer", "pump"]
    assert [(row.parent, row.name) for row in result.extra_rows] == [(0, "Washer (day 2)")]
    assert payload["number_of_deferrable_loads"] == 3
    # Real loads keep their rows; the extra row is last.
    assert payload["start_timesteps_of_each_deferrable_load"] == [4, 0, 52]
    assert payload["end_timesteps_of_each_deferrable_load"] == [12, 0, 60]
    assert payload["operating_hours_of_each_deferrable_load"] == [2.0, 1.0, 2.0]
    assert payload["operating_timesteps_of_each_deferrable_load"] == [4, 2, 4]
    assert payload["nominal_power_of_deferrable_loads"] == [2000, 500, 2000]


def test_every_per_load_array_covers_every_row():
    running = _load(current_state=True, current_power_w=1500.0)
    payload = _build([running, _load("dryer")]).payload
    assert payload["number_of_deferrable_loads"] == 4
    for key in PER_LOAD_KEYS:
        assert len(payload[key]) == 4, key
    assert len(payload["def_current_power"]) == 4


def test_extra_rows_carry_no_runtime_state():
    running = _load(
        current_state=True,
        current_power_w=1500.0,
        completed_timesteps=1,
        current_on_timesteps=3,
        minimum_on_time_minutes=30,
    )
    payload = _build([running]).payload
    assert payload["def_current_state"] == [True, False]
    assert payload["def_current_power"] == [1500.0, 0.0]
    assert payload["def_current_operating_timesteps"] == [1, 0]
    assert payload["def_current_on_timesteps"] == [3, 0]
    assert payload["def_current_off_timesteps"] == [0, 0]
    assert payload["deferrable_load_max_cost"] == [0.0, 0.0]
    # Configuration is copied from the parent.
    assert payload["def_minimum_on_time"] == [1, 1]


def test_extra_rows_are_ordered_by_occurrence_then_load():
    result = _build([_load("a"), _load("b")], horizon_steps=150)
    assert [row.name for row in result.extra_rows] == [
        "A (day 2)",
        "B (day 2)",
        "A (day 3)",
        "B (day 3)",
    ]
    assert result.payload["start_timesteps_of_each_deferrable_load"][2:] == [52, 52, 100, 100]


def test_edge_window_end_is_sent_as_unconstrained():
    # 60-step horizon: the second window ends exactly on the edge.
    payload = _build([_load()], horizon_steps=60).payload
    assert payload["start_timesteps_of_each_deferrable_load"] == [4, 52]
    assert payload["end_timesteps_of_each_deferrable_load"] == [12, 0]


@pytest.mark.parametrize("horizon_steps", [48, 58])
def test_horizon_without_a_whole_second_window_is_unchanged(horizon_steps):
    with_flag = _build([_load()], horizon_steps)
    without = _build([_load(repeats_daily=False)], horizon_steps)
    assert with_flag.payload == without.payload
    assert with_flag.extra_rows == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"repeats_daily": False},
        {"wants_to_run": False},
        {"operating_hours": 0},
        {"minimum_off_time_minutes": 24 * 60},
    ],
)
def test_loads_that_do_not_repeat_keep_one_row(overrides):
    result = _build([_load(**overrides)])
    assert result.extra_rows == []
    assert result.payload["number_of_deferrable_loads"] == 1


def test_running_pin_with_extra_rows_is_handled():
    # Pin fitting runs over arrays that now hold extra rows.
    result = _build([_load(current_state=True, current_power_w=1500.0)])
    assert result.payload["def_current_power"] == [1500.0, 0.0]


def test_group_members_contribute_their_extra_rows():
    from custom_components.emhass_companion.models import DeferrableLoadGroup

    plain = DeferrableLoad(subentry_id="pump", name="Pump", nominal_power_w=500, operating_hours=1)
    groups = [
        DeferrableLoadGroup(
            subentry_id="g",
            name="Budget",
            load_subentry_ids=("washer", "pump"),
            max_power_w=3000,
            mutual_exclusion=False,
        )
    ]
    payload = _build([_load(), plain], groups=groups).payload

    assert payload["deferrable_load_groups"] == [
        {
            "names": ["deferrable0", "deferrable2", "deferrable1"],
            "mutual_exclusion": False,
            "max_power": 3000,
        }
    ]


def test_pin_fitting_names_the_real_load_when_extra_rows_exist():
    car = DeferrableLoad(
        subentry_id="car",
        name="Car",
        nominal_power_w=10000,
        operating_hours=1,
        current_state=True,
        current_power_w=10000.0,
    )
    result = _build([_load(), car], grid=GridConfig(import_max_w=3000.0))

    assert result.payload["number_of_deferrable_loads"] == 3
    # The pin does not fit, so it is released; the extra row never had one.
    assert not any(result.payload.get("def_current_power", []))
    assert any("Car" in warning for warning in result.warnings)


def test_day_ahead_keeps_an_end_that_is_before_its_whole_day_edge():
    # 30 h asked for, but day-ahead solves 48 h: 18:00 tomorrow is not the edge.
    payload = _build([_load()], horizon_steps=60, action=ACTION_DAYAHEAD).payload
    assert payload["end_timesteps_of_each_deferrable_load"] == [12, 60]
    # MPC solves exactly 30 h, where the same end is the edge.
    payload = _build([_load()], horizon_steps=60).payload
    assert payload["end_timesteps_of_each_deferrable_load"] == [12, 0]


def test_progress_is_not_credited_to_a_window_on_a_later_date():
    # 20:00 Stockholm, today's 14:00-18:00 run is done: 4 timesteps.
    late = datetime(2026, 7, 28, 18, 0, tzinfo=UTC)
    done = _load(completed_timesteps=4)
    payload = _build([done], now=late, horizon_steps=96).payload

    assert payload["def_current_operating_timesteps"] == [0, 0]
    assert payload["operating_timesteps_of_each_deferrable_load"] == [4, 4]


def test_progress_still_counts_inside_the_open_window():
    inside = datetime(2026, 7, 28, 13, 0, tzinfo=UTC)  # 15:00 local
    payload = _build([_load(completed_timesteps=2)], now=inside).payload

    assert payload["def_current_operating_timesteps"][0] == 2


def test_progress_is_kept_when_there_is_only_one_window():
    late = datetime(2026, 7, 28, 18, 0, tzinfo=UTC)
    payload = _build([_load(completed_timesteps=4)], now=late, horizon_steps=48).payload

    assert payload["def_current_operating_timesteps"] == [4]


def test_parked_running_load_still_gets_its_later_window_row():
    # Running at noon with today's 14:00-18:00 window still ahead: today's row is
    # parked, but tomorrow's run is a separate requirement and must be asked for.
    result = _build([_load(current_state=True, single_constant=True, current_power_w=1500.0)])

    assert [(row.parent, row.name) for row in result.extra_rows] == [(0, "Washer (day 2)")]
    assert result.payload["operating_hours_of_each_deferrable_load"] == [0.0, 2.0]
    assert result.payload["def_current_state"] == [False, False]


def _tank(**overrides) -> DeferrableLoad:
    return _load("tank", repeats_daily=False, spans_daily_windows=True, max_startups=1, **overrides)


def test_water_tank_keeps_one_row_with_a_mask_spanning_every_window():
    result = _build([_tank()])
    payload = result.payload

    assert result.extra_rows == []
    assert payload["number_of_deferrable_loads"] == 1
    assert payload["start_timesteps_of_each_deferrable_load"] == [4]
    assert payload["end_timesteps_of_each_deferrable_load"] == [60]
    assert payload["set_deferrable_max_startups"] == [2]


def test_water_tank_with_one_window_is_unchanged():
    spanning = _build([_tank()], horizon_steps=30).payload
    plain = _build([_load("tank", repeats_daily=False, max_startups=1)], horizon_steps=30).payload

    assert spanning["start_timesteps_of_each_deferrable_load"] == [4]
    assert (
        spanning["end_timesteps_of_each_deferrable_load"]
        == plain["end_timesteps_of_each_deferrable_load"]
    )
    assert spanning["set_deferrable_max_startups"] == [1]


def test_water_tank_window_that_reaches_the_horizon_edge_is_unconstrained_for_mpc():
    payload = _build([_tank()], horizon_steps=60).payload

    # The second window, 14:00-18:00 tomorrow, closes exactly at the edge.
    assert payload["end_timesteps_of_each_deferrable_load"] == [0]


def test_water_tank_mask_reaches_a_window_that_the_horizon_clips():
    # 48 h from 12:00 ends at 12:00 on the third day; that day's 14:00-18:00
    # window opens after the edge, so it is not needed. Starting at 16:00 the
    # third window opens (14:00) before the edge (16:00) and closes after it.
    now = datetime(2026, 7, 28, 14, 0, tzinfo=UTC)  # 16:00 Stockholm
    payload = _build([_tank()], now=now).payload

    assert payload["end_timesteps_of_each_deferrable_load"] == [0]
    assert payload["set_deferrable_max_startups"] == [3]

"""Tests for the runtime-parameter payload builder.

These cover the specific conventions that are easy to get wrong and produce a
plausible-looking but wrong plan rather than an error.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from homeassistant.util import dt as dt_util
import pytest

from custom_components.emhass_companion.const import ACTION_DAYAHEAD, ACTION_MPC
from custom_components.emhass_companion.models import (
    BatteryConfig,
    DeferrableLoad,
    GridConfig,
    HybridInverterConfig,
    Point,
    Series,
)
from custom_components.emhass_companion.payload import (
    PayloadInputs,
    build_payload,
    in_window,
    operating_timesteps,
    window_to_timesteps,
)

HALF_HOUR = timedelta(minutes=30)
DAY_STEPS = 48  # 24 hours at 30-minute resolution


def _local(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=dt_util.DEFAULT_TIME_ZONE)


# --- window membership -------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "earliest", "latest", "expected"),
    [
        (time(12, 0), time(8, 0), time(18, 0), True),
        (time(7, 0), time(8, 0), time(18, 0), False),
        (time(18, 0), time(8, 0), time(18, 0), False),  # end is exclusive
        # Overnight windows are the case a naive comparison gets wrong.
        (time(23, 0), time(22, 0), time(6, 0), True),
        (time(2, 0), time(22, 0), time(6, 0), True),
        (time(12, 0), time(22, 0), time(6, 0), False),
        (time(6, 0), time(22, 0), time(6, 0), False),
        (time(12, 0), None, None, True),
        (time(12, 0), time(8, 0), None, True),
        (time(4, 0), None, time(6, 0), True),
    ],
)
def test_in_window(moment, earliest, latest, expected):
    assert in_window(moment, earliest, latest) is expected


# --- wall clock to timestep --------------------------------------------------


@pytest.mark.usefixtures("stockholm_timezone")
def test_no_window_is_unconstrained():
    assert window_to_timesteps(None, None, _local(2026, 7, 28, 12), HALF_HOUR, DAY_STEPS) == (0, 0)


@pytest.mark.usefixtures("stockholm_timezone")
def test_future_window_same_day():
    # 12:00 now, window 18:00-20:00 -> opens in 6h (12 steps), closes in 8h (16).
    assert window_to_timesteps(
        time(18, 0), time(20, 0), _local(2026, 7, 28, 12), HALF_HOUR, DAY_STEPS
    ) == (12, 16)


@pytest.mark.usefixtures("stockholm_timezone")
def test_overnight_window_already_open_may_start_now():
    # 23:00 inside a 22:00-06:00 window: the load may run immediately, and the
    # window closes 7 hours out. Pushing the start to tomorrow's 22:00 would
    # waste the cheap overnight hours entirely.
    assert window_to_timesteps(
        time(22, 0), time(6, 0), _local(2026, 7, 28, 23), HALF_HOUR, DAY_STEPS
    ) == (0, 14)


@pytest.mark.usefixtures("stockholm_timezone")
def test_overnight_window_not_yet_open():
    # 20:00, window 22:00-06:00 -> opens in 2h (4 steps), closes in 10h (20).
    assert window_to_timesteps(
        time(22, 0), time(6, 0), _local(2026, 7, 28, 20), HALF_HOUR, DAY_STEPS
    ) == (4, 20)


@pytest.mark.usefixtures("stockholm_timezone")
def test_window_end_beyond_horizon_is_unconstrained():
    # A 6-step horizon cannot reach 20:00, so the end constraint is dropped
    # rather than clamped to something EMHASS would misread.
    _, end = window_to_timesteps(time(18, 0), time(20, 0), _local(2026, 7, 28, 12), HALF_HOUR, 6)
    assert end == 0


@pytest.mark.usefixtures("stockholm_timezone")
def test_window_opening_beyond_horizon_is_dropped():
    assert window_to_timesteps(time(18, 0), time(20, 0), _local(2026, 7, 28, 12), HALF_HOUR, 4) == (
        0,
        0,
    )


@pytest.mark.usefixtures("stockholm_timezone")
def test_indices_are_relative_to_launch_time():
    """The same wall-clock window yields different indices as the day passes.

    This is the whole reason the conversion is redone on every request instead
    of being computed once and stored.
    """
    early = window_to_timesteps(
        time(18, 0), time(20, 0), _local(2026, 7, 28, 10), HALF_HOUR, DAY_STEPS
    )
    later = window_to_timesteps(
        time(18, 0), time(20, 0), _local(2026, 7, 28, 14), HALF_HOUR, DAY_STEPS
    )
    assert early == (16, 20)
    assert later == (8, 12)


@pytest.mark.usefixtures("stockholm_timezone")
def test_window_across_dst_end_uses_wall_clock():
    """Europe/Stockholm gains an hour on 2026-10-25; 22:00->06:00 spans it.

    Users mean wall-clock times, so the window must still end at 06:00 local --
    which is 9 elapsed hours (18 half-hour steps) that night, not 8.
    """
    start, end = window_to_timesteps(
        time(22, 0), time(6, 0), _local(2026, 10, 24, 22), HALF_HOUR, DAY_STEPS
    )
    assert start == 0
    assert end == 18


# --- full payload ------------------------------------------------------------


def _series(start: datetime, hours: int, value: float) -> Series:
    return Series(Point(start + timedelta(minutes=30 * index), value) for index in range(hours * 2))


def _inputs(**overrides) -> PayloadInputs:
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    defaults = {
        "action": ACTION_MPC,
        "now": now,
        "time_step_minutes": 30,
        "horizon_steps": DAY_STEPS,
        "battery": BatteryConfig(),
        "grid": GridConfig(),
        "hybrid_inverter": HybridInverterConfig(),
    }
    return PayloadInputs(**{**defaults, **overrides})


def test_mpc_sends_prediction_horizon_in_timesteps():
    payload = build_payload(_inputs()).payload
    assert payload["prediction_horizon"] == DAY_STEPS
    assert payload["optimization_time_step"] == 30
    assert "delta_forecast_daily" not in payload


def test_dayahead_sends_delta_forecast_in_days():
    payload = build_payload(_inputs(action=ACTION_DAYAHEAD)).payload
    assert payload["delta_forecast_daily"] == 1
    assert "prediction_horizon" not in payload


def test_forecasts_are_sent_as_timestamp_maps_with_explicit_offsets():
    """Never bare lists: a mapping is resampled and aligned by EMHASS itself."""
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    result = build_payload(_inputs(pv=_series(now, 24, 5000.0)))

    forecast = result.payload["pv_power_forecast"]
    assert isinstance(forecast, dict)
    key = next(iter(forecast))
    assert key == "2026-07-28T10:00:00+00:00"
    assert forecast[key] == 5000.0
    # The forecast method is deliberately not set; EMHASS switches it to "list"
    # by itself when a forecast key is present.
    assert "weather_forecast_method" not in result.payload


def test_short_forecast_produces_a_warning():
    """A short series is silently forward-filled by EMHASS, so we must warn.

    Nothing downstream errors: the plan comes back looking entirely healthy,
    built on a price that flat-lined at its last known value.
    """
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    result = build_payload(_inputs(buy_price=_series(now, 6, 1.5)))

    assert len(result.warnings) == 1
    assert "Buy price" in result.warnings[0]


def test_full_length_forecast_produces_no_warning():
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    result = build_payload(_inputs(buy_price=_series(now, 25, 1.5)))
    assert result.warnings == []


def test_battery_disabled_sends_only_the_flag():
    payload = build_payload(_inputs()).payload
    assert payload["set_use_battery"] is False
    assert "battery_nominal_energy_capacity" not in payload


def test_battery_enabled_sends_every_limit():
    battery = BatteryConfig(
        enabled=True,
        capacity_wh=25600,
        charge_power_max_w=10600,
        discharge_power_max_w=10600,
        soc_min=0.05,
        soc_max=0.95,
    )
    payload = build_payload(_inputs(battery=battery, soc_init=0.098)).payload
    assert payload["set_use_battery"] is True
    assert payload["battery_nominal_energy_capacity"] == 25600
    assert payload["battery_minimum_state_of_charge"] == 0.05
    # A fraction, matching EMHASS -- not the percentage Home Assistant reports.
    assert payload["soc_init"] == 0.098


# --- hybrid inverter -----------------------------------------------------------


def test_hybrid_inverter_disabled_sends_only_the_flag():
    """Disabled must still assert the flag -- never leave it to EMHASS's own
    persisted config.json, the same reasoning as the battery flag above."""
    payload = build_payload(_inputs()).payload
    assert payload["inverter_is_hybrid"] is False
    assert "inverter_ac_output_max" not in payload


def test_hybrid_inverter_enabled_sends_every_limit():
    hybrid = HybridInverterConfig(
        enabled=True,
        ac_output_max_w=5000,
        ac_input_max_w=6000,
        efficiency_dc_ac=0.97,
        efficiency_ac_dc=0.98,
    )
    payload = build_payload(_inputs(hybrid_inverter=hybrid)).payload
    assert payload["inverter_is_hybrid"] is True
    assert payload["inverter_ac_output_max"] == 5000
    assert payload["inverter_ac_input_max"] == 6000
    assert payload["inverter_efficiency_dc_ac"] == 0.97
    assert payload["inverter_efficiency_ac_dc"] == 0.98


def test_no_deferrable_loads_is_a_valid_configuration():
    payload = build_payload(_inputs()).payload
    assert payload["number_of_deferrable_loads"] == 0


@pytest.mark.usefixtures("stockholm_timezone")
def test_deferrable_arrays_are_parallel_to_load_order():
    """Every per-load array must line up with ``load_order`` index for index.

    ``load_order[k]`` is what tells the rest of the integration which subentry
    owns EMHASS's ``P_deferrable{k}`` column. If the arrays and that list ever
    disagree, each load silently gets another load's schedule.
    """
    loads = [
        DeferrableLoad(
            subentry_id="dishwasher",
            name="Dishwasher",
            nominal_power_w=2000,
            operating_hours=2,
            semi_continuous=True,
        ),
        DeferrableLoad(
            subentry_id="car",
            name="Car",
            nominal_power_w=11000,
            operating_hours=3,
            semi_continuous=False,
            current_state=True,
        ),
    ]
    result = build_payload(_inputs(loads=loads))
    payload = result.payload

    # build_payload preserves the order it is given; choosing that order is the
    # coordinator's job, so that it stays stable across runs.
    assert result.load_order == ["dishwasher", "car"]
    assert payload["number_of_deferrable_loads"] == 2

    car = result.load_order.index("car")
    assert payload["nominal_power_of_deferrable_loads"][car] == 11000
    assert payload["operating_hours_of_each_deferrable_load"][car] == 3
    assert payload["treat_deferrable_load_as_semi_cont"][car] is False
    assert payload["def_current_state"][car] is True

    for key in (
        "nominal_power_of_deferrable_loads",
        "minimum_power_of_deferrable_loads",
        "operating_hours_of_each_deferrable_load",
        "operating_timesteps_of_each_deferrable_load",
        "start_timesteps_of_each_deferrable_load",
        "end_timesteps_of_each_deferrable_load",
        "treat_deferrable_load_as_semi_cont",
        "set_deferrable_load_single_constant",
        "set_deferrable_startup_penalty",
        "set_deferrable_max_startups",
        "def_current_state",
        "def_current_operating_timesteps",
        "deferrable_load_max_cost",
    ):
        assert len(payload[key]) == len(result.load_order), key


# --- operating hours must land on timestep boundaries ------------------------


@pytest.mark.parametrize(
    ("hours", "step_minutes", "expected_steps"),
    [
        (2, 30, 4),
        (2, 15, 8),
        (0, 30, 0),
        # Rounds to the nearest step ...
        (2.6, 30, 5),
        (0.4, 30, 1),
        # ... except that a non-zero request never rounds down to "never run".
        (0.1, 30, 1),
        (0.01, 60, 1),
    ],
)
def test_operating_hours_become_whole_timesteps(hours, step_minutes, expected_steps):
    assert operating_timesteps(hours, step_minutes) == expected_steps


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_run_time_off_the_timestep_grid_is_quantised_and_reported():
    """EMHASS enforces `sum(p) * dt == nominal * hours` as an equality.

    A semi-continuous load can only draw 0 or exactly its nominal power, so
    2.6 h at a 30-minute step has no solution at all -- EMHASS reports an
    infeasible problem rather than rounding. Quantising here is what keeps that
    from happening, and the warning is what stops it being a silent change.
    """
    loads = [
        DeferrableLoad(
            subentry_id="car",
            name="Car",
            nominal_power_w=11000,
            operating_hours=2.6,
            semi_continuous=True,
        )
    ]
    result = build_payload(_inputs(loads=loads))

    assert result.payload["operating_hours_of_each_deferrable_load"] == [2.5]
    assert result.payload["operating_timesteps_of_each_deferrable_load"] == [5]
    assert any("2.5 h" in warning for warning in result.warnings)


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_run_time_already_on_the_grid_is_not_reported():
    loads = [
        DeferrableLoad(
            subentry_id="dishwasher", name="Dishwasher", nominal_power_w=2000, operating_hours=2
        )
    ]
    result = build_payload(_inputs(loads=loads))

    assert result.payload["operating_hours_of_each_deferrable_load"] == [2.0]
    assert result.warnings == []


@pytest.mark.usefixtures("stockholm_timezone")
def test_exact_timesteps_are_sent_only_for_mpc():
    """EMHASS honours this key only on the branch that takes a prediction
    horizon (utils.treat_runtimeparams); it is absent from associations.csv, so
    the day-ahead request has to make do with the quantised hours."""
    loads = [
        DeferrableLoad(
            subentry_id="dishwasher", name="Dishwasher", nominal_power_w=2000, operating_hours=2
        )
    ]

    mpc = build_payload(_inputs(loads=loads, action=ACTION_MPC)).payload
    dayahead = build_payload(_inputs(loads=loads, action=ACTION_DAYAHEAD)).payload

    assert mpc["operating_timesteps_of_each_deferrable_load"] == [4]
    assert "operating_timesteps_of_each_deferrable_load" not in dayahead
    assert dayahead["operating_hours_of_each_deferrable_load"] == [2.0]


# --- minimum power -----------------------------------------------------------


@pytest.mark.usefixtures("stockholm_timezone")
def test_minimum_power_is_sent_per_load():
    loads = [
        DeferrableLoad(
            subentry_id="car",
            name="Car",
            nominal_power_w=11000,
            minimum_power_w=1380,
            operating_hours=3,
            semi_continuous=False,
        )
    ]
    payload = build_payload(_inputs(loads=loads)).payload
    assert payload["minimum_power_of_deferrable_loads"] == [1380]


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_minimum_power_above_nominal_is_clamped():
    """A floor above the ceiling leaves no feasible power at all, and EMHASS
    reports that as an infeasible problem naming no load in particular."""
    loads = [
        DeferrableLoad(
            subentry_id="car",
            name="Car",
            nominal_power_w=3000,
            minimum_power_w=5000,
            operating_hours=1,
            semi_continuous=False,
        )
    ]
    result = build_payload(_inputs(loads=loads))

    assert result.payload["minimum_power_of_deferrable_loads"] == [3000]
    assert any("minimum power" in warning for warning in result.warnings)


@pytest.mark.usefixtures("stockholm_timezone")
def test_max_startups_is_sent_per_load():
    loads = [
        DeferrableLoad(
            subentry_id="heatpump",
            name="Heat pump",
            nominal_power_w=3000,
            operating_hours=4,
            max_startups=2,
        )
    ]
    payload = build_payload(_inputs(loads=loads)).payload
    assert payload["set_deferrable_max_startups"] == [2]


@pytest.mark.usefixtures("stockholm_timezone")
def test_deferrable_load_max_cost_is_disabled_but_correctly_sized():
    """Companion has no per-load max-cost feature -- but must still send this.

    EMHASS's optimisation.py indexes this list with no bounds check, unlike
    every sibling array (all `k < len(...)` guarded). Leaving it to whatever is
    persisted in EMHASS's own config is how a load count increase turns into an
    uncaught IndexError and a 500 on every optimisation, rather than the clean
    "unlimited" behaviour a missing key would give.
    """
    loads = [
        DeferrableLoad(
            subentry_id=f"load{i}", name=f"Load {i}", nominal_power_w=1000, operating_hours=1
        )
        for i in range(3)
    ]
    payload = build_payload(_inputs(loads=loads)).payload

    assert payload["deferrable_load_max_cost"] == [0.0, 0.0, 0.0]


def test_disabled_loads_are_excluded():
    loads = [
        DeferrableLoad(subentry_id="a", name="On", nominal_power_w=1, operating_hours=1),
        DeferrableLoad(
            subentry_id="b",
            name="Off",
            nominal_power_w=1,
            operating_hours=1,
            enabled=False,
        ),
    ]
    result = build_payload(_inputs(loads=loads))
    assert result.payload["number_of_deferrable_loads"] == 1
    assert result.load_order == ["a"]


def test_profile_settings_can_override_defaults():
    """A "no solar" profile must be able to switch PV modelling off."""
    payload = build_payload(_inputs(extra_settings={"set_use_pv": False})).payload
    assert payload["set_use_pv"] is False

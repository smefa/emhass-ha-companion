"""Analytical tank contract and UTC-overlap daily energy schedules."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.emhass_companion.thermal import ThermalConfig
from custom_components.emhass_companion.water_tank import WaterTankConfig

pytestmark = pytest.mark.usefixtures("stockholm_timezone")
STOCKHOLM = ZoneInfo("Europe/Stockholm")


def _config(**overrides) -> WaterTankConfig:
    return WaterTankConfig(
        **{
            "current_temperature": 42.0,
            "volume_litres": 200.0,
            "nominal_power_w": 2000.0,
            "standby_loss_w": 50.0,
            "comfort_temperature": 45.0,
            "setback_temperature": 37.0,
            "max_temperature": 52.0,
            "comfort_start": time(6),
            "comfort_end": time(22),
            "draw_start": time(7),
            "draw_end": time(9),
            "draw_energy_kwh": 4.0,
            **overrides,
        }
    )


def _local(day: str, hour: int = 0, minute: int = 0) -> datetime:
    return (
        datetime.fromisoformat(day)
        .replace(hour=hour, minute=minute, tzinfo=STOCKHOLM)
        .astimezone(UTC)
    )


def test_v0188_payload_contract():
    """Fixture from v0.18.8 optimization.py _add_thermal_battery_constraints.

    Conversion is 3600 / (density * heat_capacity * volume); not tank_volume
    or specific_heat_capacity. _resolve_draw_off_demand reads kWh/step and
    _loss_kw_to_timestep_energy multiplies thermal_loss (kW) by step hours.
    """
    payload = _config().to_emhass(_local("2026-07-28"), timedelta(minutes=30), 48)
    assert payload == {
        "volume": 0.2,
        "density": 997.0,
        "heat_capacity": 4.184,
        "thermal_loss": 0.05,
        "efficiency": 1.0,
        "sense": "heat",
        "start_temperature": 42.0,
        "min_temperatures": payload["min_temperatures"],
        "max_temperatures": [52.0] * 48,
        "draw_off_demand": [0.0] * 14 + [1.0] * 4 + [0.0] * 30,
    }
    assert len(payload["min_temperatures"]) == 48
    assert "thermal_model" not in payload


def test_200_litre_2kw_analytical_heating_rate():
    assert _config().heating_rate == pytest.approx(8.63, abs=0.01)
    assert _config(volume_litres=400).heating_rate == pytest.approx(_config().heating_rate / 2)


@pytest.mark.parametrize("minutes", [15, 30, 60])
@pytest.mark.parametrize(
    "heater_w,loss_w,draw_kwh", [(0, 0, 0), (0, 50, 0), (0, 0, 4), (2000, 50, 4)]
)
def test_analytical_energy_balance(minutes, heater_w, loss_w, draw_kwh):
    config = _config(standby_loss_w=loss_w, draw_energy_kwh=draw_kwh)
    step = timedelta(minutes=minutes)
    payload = config.to_emhass(_local("2026-07-28"), step, 1440 // minutes)
    conversion = 3600 / (payload["density"] * payload["heat_capacity"] * payload["volume"])
    temperature = payload["start_temperature"]
    for demand in payload["draw_off_demand"]:
        temperature += conversion * (
            (heater_w / 1000 * payload["efficiency"] - payload["thermal_loss"]) * minutes / 60
            - demand
        )
    expected_energy = heater_w / 1000 * 24 - loss_w / 1000 * 24 - draw_kwh
    assert temperature == pytest.approx(42 + expected_energy / (199.4 * 4.184 / 3600))
    assert sum(payload["draw_off_demand"]) == pytest.approx(draw_kwh)


def test_off_cycle_partial_horizon_uses_overlap_not_alignment_or_tiling():
    profile = _config().draw_off_demand(_local("2026-07-28", 7, 10), timedelta(minutes=30), 4)
    assert profile == pytest.approx([1, 1, 1, 2 / 3])


def test_partial_window_endpoints_resample_energy():
    config = _config(draw_start=time(7, 10), draw_end=time(8, 40), draw_energy_kwh=3)
    assert config.draw_off_demand(_local("2026-07-28", 7), timedelta(hours=1), 3) == pytest.approx(
        [5 / 3, 4 / 3, 0]
    )


def test_overnight_window_includes_previous_days_tail():
    config = _config(draw_start=time(22), draw_end=time(2))
    profile = config.draw_off_demand(_local("2026-07-28"), timedelta(hours=1), 48)
    assert profile == [1, 1] + [0] * 20 + [1] * 4 + [0] * 20 + [1] * 2
    assert sum(profile) == 8


@pytest.mark.parametrize("day,hours", [("2026-03-29", 23), ("2026-10-25", 25)])
@pytest.mark.parametrize("minutes", [15, 30, 60])
def test_full_local_day_preserves_energy_across_dst(day, hours, minutes):
    config = _config(draw_start=time(0), draw_end=time(0), draw_energy_kwh=6)
    profile = config.draw_off_demand(_local(day), timedelta(minutes=minutes), hours * 60 // minutes)
    assert profile == pytest.approx([6 / len(profile)] * len(profile))
    assert sum(profile) == pytest.approx(6)


@pytest.mark.parametrize("day,hours", [("2026-03-28", 3), ("2026-10-24", 5)])
def test_overnight_dst_window_uses_actual_elapsed_duration(day, hours):
    config = _config(draw_start=time(23), draw_end=time(3), draw_energy_kwh=6)
    profile = config.draw_off_demand(_local(day, 23), timedelta(hours=1), hours + 1)
    assert profile == pytest.approx([6 / hours] * hours + [0])


@pytest.mark.parametrize(
    "draw_start,draw_end,expected",
    [
        (time(2, 30), time(4), [0, 0, 2, 2, 0, 0, 0, 0]),
        (time(1), time(2, 30), [2, 2, 0, 0, 0, 0, 0, 0]),
    ],
)
def test_missing_endpoint_shifts_to_first_valid_time(draw_start, draw_end, expected):
    config = _config(draw_start=draw_start, draw_end=draw_end)
    profile = config.draw_off_demand(_local("2026-03-29", 1), timedelta(minutes=30), 8)
    assert profile == expected


def test_both_missing_endpoints_preserve_daily_energy_at_first_valid_instant():
    config = _config(draw_start=time(2, 10), draw_end=time(2, 40))
    assert config.draw_off_demand(_local("2026-03-29", 1), timedelta(minutes=30), 4) == [0, 0, 4, 0]


@pytest.mark.parametrize(
    "draw_start,draw_end,expected",
    [
        (time(2, 30), time(3, 30), [0, 0, 1, 1, 1, 1, 0]),
        (time(1, 30), time(2, 30), [2, 2, 0, 0, 0, 0, 0]),
    ],
)
def test_ambiguous_endpoints_use_first_fold(draw_start, draw_end, expected):
    config = _config(draw_start=draw_start, draw_end=draw_end)
    profile = config.draw_off_demand(_local("2026-10-25", 1, 30), timedelta(minutes=30), 7)
    assert profile == expected


def test_zero_demand_and_standby_remain_explicit_with_zero_current_temperature():
    payload = _config(draw_energy_kwh=0, standby_loss_w=0, current_temperature=0).to_emhass(
        _local("2026-07-28"), timedelta(hours=1), 1
    )
    assert payload["draw_off_demand"] == [0]
    assert payload["thermal_loss"] == 0
    assert payload["start_temperature"] == 0


def test_ramp_reuses_comfort_helper_with_physically_derived_rate():
    config = _config()
    start = _local("2026-07-28", 5, 30)
    actual, maximums = config.comfort_band(start, timedelta(minutes=30), 4)
    reference = ThermalConfig(
        heating_rate=config.heating_rate,
        comfort_temperature=45,
        setback_temperature=37,
        max_temperature=52,
        comfort_start=time(6),
        comfort_end=time(22),
    )
    expected, expected_maximums = reference.comfort_band(start, timedelta(minutes=30), 4)
    assert actual == pytest.approx(expected)
    assert maximums == expected_maximums
    assert actual[0] == pytest.approx(45 - config.heating_rate / 2)


@pytest.mark.parametrize(
    ("day", "start_hour", "start_minute", "comfort", "hours_until"),
    [
        ("2026-03-29", 0, 45, time(3, 30), 0.75),
        ("2026-10-25", 0, 45, time(3, 30), 1.75),
        ("2026-03-29", 0, 30, time(2, 30), 0.5),
        ("2026-10-25", 0, 0, time(2, 30), 0.5),
    ],
)
def test_tank_comfort_ramp_uses_elapsed_time_across_dst(
    day, start_hour, start_minute, comfort, hours_until
):
    config = _config(comfort_start=comfort, comfort_end=time(5))
    instant = datetime.fromisoformat(day).replace(hour=start_hour, minute=start_minute, tzinfo=UTC)
    minimums, _ = config.comfort_band(instant, timedelta(minutes=15), 1)
    assert minimums[0] == pytest.approx(
        max(
            config.setback_temperature,
            config.comfort_temperature - config.heating_rate * hours_until,
        )
    )


def test_ambiguous_comfort_window_stays_active_through_second_fold():
    config = _config(comfort_start=time(2, 30), comfort_end=time(4))
    instant = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
    assert config.comfort_band(instant, timedelta(minutes=15), 1)[0] == [45]


@pytest.mark.parametrize("day", ["2026-03-29", "2026-10-25"])
def test_local_aware_and_utc_grids_match_across_dst(day):
    config = _config(
        comfort_start=time(3), comfort_end=time(4), draw_start=time(1), draw_end=time(4)
    )
    start = _local(day)
    assert config.to_emhass(start, timedelta(minutes=30), 12) == config.to_emhass(
        start.astimezone(STOCKHOLM), timedelta(minutes=30), 12
    )
    minimums, _ = config.comfort_band(start, timedelta(minutes=30), 12)
    for index in range(12):
        instant = start + index * timedelta(minutes=30)
        if instant.astimezone(STOCKHOLM).hour == 3:
            assert minimums[index] == 45


@pytest.mark.parametrize(
    "field",
    [
        "current_temperature",
        "volume_litres",
        "nominal_power_w",
        "standby_loss_w",
        "comfort_temperature",
        "setback_temperature",
        "max_temperature",
        "draw_energy_kwh",
    ],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), None, True, "42"])
def test_nonfinite_or_nonnumeric_config_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        _config(**{field: value})


@pytest.mark.parametrize(
    "overrides",
    [
        {"volume_litres": 0},
        {"volume_litres": -1},
        {"nominal_power_w": 0},
        {"nominal_power_w": -1},
        {"standby_loss_w": -1},
        {"draw_energy_kwh": -1},
        {"current_temperature": -1},
        {"current_temperature": 101},
        {"setback_temperature": 4},
        {"max_temperature": 96},
        {"setback_temperature": 46},
        {"comfort_temperature": 53},
    ],
)
def test_invalid_physical_bounds_rejected(overrides):
    with pytest.raises(ValueError):
        _config(**overrides)


@pytest.mark.parametrize("field", ["comfort_start", "comfort_end", "draw_start", "draw_end"])
@pytest.mark.parametrize("value", [None, "07:00", time(7, tzinfo=UTC)])
def test_invalid_local_window_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        _config(**{field: value})


@pytest.mark.parametrize(
    "overrides",
    [
        {"grid_start": datetime(2026, 7, 28)},
        {"step": timedelta(0)},
        {"step": timedelta(minutes=-1)},
        {"horizon_steps": 0},
        {"horizon_steps": -1},
        {"horizon_steps": 1.5},
        {"horizon_steps": True},
    ],
)
def test_invalid_grid_rejected_even_for_standby_only(overrides):
    arguments = {
        "grid_start": _local("2026-07-28"),
        "step": timedelta(hours=1),
        "horizon_steps": 24,
    }
    with pytest.raises(ValueError):
        _config(draw_energy_kwh=0).to_emhass(**{**arguments, **overrides})


def test_mutated_live_temperature_is_validated_before_projection():
    config = _config()
    config.current_temperature = float("nan")
    with pytest.raises(ValueError, match="current_temperature"):
        config.to_emhass(_local("2026-07-28"), timedelta(hours=1), 24)


@pytest.mark.parametrize("day", ["2026-03-28", "2026-10-24"])
def test_off_cycle_mpc_slices_equal_one_continuous_dst_horizon(day):
    config = _config(draw_start=time(23, 10), draw_end=time(3, 40))
    start = _local(day, 22, 7)
    step = timedelta(minutes=15)
    whole = config.draw_off_demand(start, step, 192)
    pieces = config.draw_off_demand(start, step, 19) + config.draw_off_demand(
        start + 19 * step, step, 173
    )
    assert pieces == pytest.approx(whole)
    assert sum(whole) == pytest.approx(8)


def test_missing_endpoint_with_seconds_resolves_to_exact_gap_boundary():
    config = _config(draw_start=time(2, 30, 17, 123456), draw_end=time(3, 1))
    profile = config.draw_off_demand(_local("2026-03-29", 3), timedelta(seconds=30), 3)
    assert profile == [2, 2, 0]


def test_equal_nonmidnight_endpoints_mean_full_local_day():
    config = _config(draw_start=time(6), draw_end=time(6), draw_energy_kwh=24)
    assert config.draw_off_demand(_local("2026-07-28"), timedelta(hours=1), 48) == [1] * 48


def test_energy_at_collapsed_endpoint_is_not_duplicated_at_horizon_boundary():
    config = _config(draw_start=time(2, 10), draw_end=time(2, 40))
    before = config.draw_off_demand(_local("2026-03-29", 1), timedelta(minutes=30), 2)
    after = config.draw_off_demand(_local("2026-03-29", 3), timedelta(minutes=30), 2)
    assert before == [0, 0]
    assert after == [4, 0]


def _best_case(config: WaterTankConfig, step: timedelta, steps: int) -> list[float]:
    """Independent flat-out trajectory: kWh in, minus loss and draw, over C per kWh."""
    capacity = config.volume_litres / 1000 * 997.0 * 4.184 / 3600
    draw = config.draw_off_demand(_local("2026-07-28"), step, steps)
    hours = step / timedelta(hours=1)
    temperatures = [config.current_temperature]
    for index in range(steps - 1):
        gained = (config.nominal_power_w - config.standby_loss_w) / 1000 * hours - draw[index]
        temperatures.append(temperatures[-1] + gained / capacity)
    return temperatures


def test_floors_the_heater_cannot_reach_are_lowered_to_just_under_what_it_can():
    config = _config(current_temperature=30.0, draw_energy_kwh=0.0)
    step, steps = timedelta(minutes=30), 48
    asked, _ = config.comfort_band(_local("2026-07-28"), step, steps)
    best = _best_case(config, step, steps)

    sent = config.to_emhass(_local("2026-07-28"), step, steps)["min_temperatures"]

    assert any(a > b for a, b in zip(asked, best, strict=True))  # the premise
    for index in range(1, steps):
        assert sent[index] <= asked[index]
        assert sent[index] <= best[index] + 1e-6  # reachable
        if asked[index] > best[index] + 1e-6:
            assert sent[index] == pytest.approx(best[index] - 0.02)
        else:
            assert sent[index] == asked[index]  # untouched when reachable
    assert sent[0] == asked[0]


def test_a_reachable_schedule_is_sent_unchanged_and_reports_nothing():
    config = _config(current_temperature=50.0)
    step = timedelta(minutes=30)
    asked, _ = config.comfort_band(_local("2026-07-28"), step, 48)

    assert config.to_emhass(_local("2026-07-28"), step, 48)["min_temperatures"] == asked
    assert config.first_unreachable_step(_local("2026-07-28"), step, 48) is None


def test_first_unreachable_step_names_the_first_missed_floor():
    config = _config(current_temperature=30.0, draw_energy_kwh=0.0)
    step = timedelta(minutes=30)
    asked, _ = config.comfort_band(_local("2026-07-28"), step, 48)
    best = _best_case(config, step, 48)
    expected = next(i for i in range(1, 48) if asked[i] > best[i] + 0.01)

    shortfall = config.first_unreachable_step(_local("2026-07-28"), step, 48)

    assert shortfall is not None
    assert shortfall.required == pytest.approx(asked[expected])
    assert shortfall.achievable == pytest.approx(best[expected])
    assert shortfall.current == 30.0
    assert shortfall.power_w == 2000.0
    assert shortfall.moment == _local("2026-07-28").astimezone(STOCKHOLM) + step * expected


def test_a_lowered_request_no_longer_trips_the_infeasibility_check():
    from custom_components.emhass_companion.infeasibility import Report, check_thermal_unreachable

    config = _config(current_temperature=30.0)
    step = timedelta(minutes=30)
    payload = {
        "optimization_time_step": 30,
        "prediction_horizon": 48,
        "pv_power_forecast": [0] * 48,
        "nominal_power_of_deferrable_loads": [2000],
        "def_load_config": [{"thermal_battery": config.to_emhass(_local("2026-07-28"), step, 48)}],
    }
    report = Report()
    check_thermal_unreachable(payload, report)

    assert [f for f in report.findings if f.code == "thermal_unreachable"] == []


def test_a_floor_just_above_the_trajectory_is_lowered_too():
    # EMHASS holds the floor hard, so even a floor 5 mK out of reach is infeasible.
    step, steps = timedelta(minutes=30), 48
    probe = _config(current_temperature=0.0, draw_energy_kwh=0.0)
    asked, _ = probe.comfort_band(_local("2026-07-28"), step, steps)
    gain = _best_case(probe, step, steps)
    # A start temperature that leaves the floor at one timestep 5 mK out of reach.
    near = next(i for i in range(1, steps) if 20 < asked[i] - gain[i] - 0.005 < 90)
    config = _config(current_temperature=asked[near] - gain[near] - 0.005, draw_energy_kwh=0.0)
    best = _best_case(config, step, steps)

    sent = config.to_emhass(_local("2026-07-28"), step, steps)["min_temperatures"]

    assert 0 < asked[near] - best[near] < 0.01  # the premise
    assert sent[near] == pytest.approx(best[near] - 0.02)


def test_heater_cop_scales_the_payload_efficiency_and_the_heating_rate():
    base = _config()
    heat_pump = _config(heater_cop=3.0)
    assert heat_pump.heating_rate == pytest.approx(base.heating_rate * 3)
    payload = heat_pump.to_emhass(_local("2026-07-28"), timedelta(minutes=30), 48)
    assert payload["efficiency"] == 3.0
    # Rated power stays the electrical draw; only the heat it delivers changes.
    assert _config(heater_cop=3.0).nominal_power_w == base.nominal_power_w


def test_heater_cop_raises_what_a_cold_tank_can_reach():
    step, steps = timedelta(minutes=30), 48
    cold = {"current_temperature": 30.0, "draw_energy_kwh": 0.0}
    immersion = _config(**cold)
    heat_pump = _config(heater_cop=3.0, **cold)
    start = _local("2026-07-28")
    assert immersion.first_unreachable_step(start, step, steps) is not None
    assert heat_pump.first_unreachable_step(start, step, steps) is None
    assert min(heat_pump.to_emhass(start, step, steps)["min_temperatures"][1:]) > 30.0


@pytest.mark.parametrize("value", [0, -1, 10.01, float("nan"), float("inf"), None, True, "3"])
def test_invalid_heater_cop_rejected(value):
    with pytest.raises(ValueError, match="heater_cop"):
        _config(heater_cop=value)


@pytest.mark.parametrize("value", [-0.1, 1000.1, float("nan"), float("inf"), None, True, "5"])
def test_invalid_comfort_penalty_rejected(value):
    with pytest.raises(ValueError, match="comfort_penalty"):
        _config(comfort_penalty=value)


def test_hard_comfort_is_the_default_and_sends_no_target_keys():
    payload = _config().to_emhass(_local("2026-07-28"), timedelta(minutes=30), 48)
    assert not {"desired_temperatures", "overshoot_temperature", "penalty_factor"} & payload.keys()


def test_comfort_penalty_turns_the_schedule_into_a_priced_target():
    config = _config(comfort_penalty=10.0)
    step, steps = timedelta(minutes=30), 48
    hard = _config().to_emhass(_local("2026-07-28"), step, steps)
    soft = config.to_emhass(_local("2026-07-28"), step, steps)

    # Only the setback is a hard limit; the comfort schedule is what gets priced.
    assert soft["min_temperatures"] == [37.0] * steps
    assert soft["desired_temperatures"] == hard["min_temperatures"]
    assert soft["penalty_factor"] == 10.0
    assert soft["overshoot_temperature"] == 52.0
    assert soft["max_temperatures"] == hard["max_temperatures"]
    assert max(soft["desired_temperatures"]) == 45.0


def test_a_priced_target_the_heater_cannot_reach_is_not_lowered():
    # Unreachable comfort only costs penalty, so the target is sent as asked.
    config = _config(comfort_penalty=10.0, current_temperature=30.0, draw_energy_kwh=0.0)
    step, steps = timedelta(minutes=30), 48
    payload = config.to_emhass(_local("2026-07-28"), step, steps)
    asked, _ = config.comfort_band(_local("2026-07-28"), step, steps)
    assert payload["desired_temperatures"] == asked


def test_an_unreachable_priced_target_is_not_reported_as_a_shortfall():
    # Above the setback but too weak a heater to reach the 45 C target in time.
    weak = {"current_temperature": 38.0, "draw_energy_kwh": 0.0, "nominal_power_w": 200.0}
    start, step, steps = _local("2026-07-28"), timedelta(minutes=30), 48
    assert _config(**weak).first_unreachable_step(start, step, steps) is not None
    assert _config(comfort_penalty=10.0, **weak).first_unreachable_step(start, step, steps) is None


def test_a_setback_the_heater_cannot_reach_is_still_lowered_with_a_penalty():
    config = _config(comfort_penalty=10.0, current_temperature=10.0, draw_energy_kwh=0.0)
    step, steps = timedelta(minutes=30), 4
    payload = config.to_emhass(_local("2026-07-28"), step, steps)
    best = _best_case(config, step, steps)
    for index in range(1, steps):
        assert payload["min_temperatures"][index] <= best[index] + 1e-6


def test_shortfall_time_is_elapsed_not_wall_clock_across_the_autumn_change():
    # 02:45 CEST plus one 15-minute step is 02:00 CET, not 03:00 on the wall clock.
    config = _config(current_temperature=30.0, draw_energy_kwh=0.0)
    start = datetime(2026, 10, 25, 0, 45, tzinfo=UTC)  # 02:45 CEST
    shortfall = config.first_unreachable_step(start, timedelta(minutes=15), 8)
    assert shortfall is not None  # a 30 C tank misses the 37 C setback at once
    assert shortfall.moment.astimezone(UTC) == start + timedelta(minutes=15)


def _request(tank: dict, power_w: float = 2000.0, steps: int = 48) -> dict:
    return {
        "optimization_time_step": 30,
        "prediction_horizon": steps,
        "pv_power_forecast": [0] * steps,
        "nominal_power_of_deferrable_loads": [power_w],
        "def_load_config": [{"thermal_battery": tank}],
    }


def _unreachable(payload: dict) -> list:
    from custom_components.emhass_companion.infeasibility import Report, check_thermal_unreachable

    report = Report()
    check_thermal_unreachable(payload, report)
    return [f for f in report.findings if f.code == "thermal_unreachable"]


# A cold tank, a weak heater, and every combination of the two new settings.
@pytest.mark.parametrize("cop", [1.0, 3.0])
@pytest.mark.parametrize("penalty", [0.0, 10.0])
@pytest.mark.parametrize("start", [10.0, 30.0, 38.0, 50.0])
@pytest.mark.parametrize("power_w", [200.0, 2000.0])
def test_every_request_companion_builds_passes_its_own_infeasibility_check(
    cop, penalty, start, power_w
):
    config = _config(
        current_temperature=start,
        nominal_power_w=power_w,
        heater_cop=cop,
        comfort_penalty=penalty,
        draw_energy_kwh=0.0,
    )
    tank = config.to_emhass(_local("2026-07-28"), timedelta(minutes=30), 48)
    assert _unreachable(_request(tank, power_w)) == []


def test_the_check_models_the_cop_it_is_sent():
    # 38 C start, 200 W: the first step gains 0.3 C at COP 1 but 1.2 C at COP 3.
    step, steps = timedelta(minutes=30), 48
    config = _config(current_temperature=38.0, nominal_power_w=200.0, draw_energy_kwh=0.0)
    tank = config.to_emhass(_local("2026-07-28"), step, steps)
    tank["min_temperatures"] = [38.0] + [39.0] * (steps - 1)
    assert _unreachable(_request(tank, 200.0))
    tank["efficiency"] = 3.0
    assert _unreachable(_request(tank, 200.0)) == []


def test_a_priced_target_is_ignored_by_the_check_but_the_setback_is_not():
    from custom_components.emhass_companion.infeasibility import best_case_temperatures

    step, steps = timedelta(minutes=30), 48
    weak = {"nominal_power_w": 200.0, "draw_energy_kwh": 0.0, "comfort_penalty": 10.0}
    # Setback reachable, comfort target not: EMHASS only prices the miss.
    soft = _config(current_temperature=38.0, **weak).to_emhass(_local("2026-07-28"), step, steps)
    best = best_case_temperatures(soft, 200.0, 30, steps)
    assert any(d > b + 0.01 for d, b in zip(soft["desired_temperatures"], best, strict=True))
    assert _unreachable(_request(soft, 200.0)) == []
    # A setback it cannot reach is still a hard floor, so a raw request is flagged.
    raw = {**soft, "min_temperatures": [38.0] + [45.0] * (steps - 1)}
    assert _unreachable(_request(raw, 200.0))

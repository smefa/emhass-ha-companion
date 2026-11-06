"""Multiple daily tank periods preserve energy and availability on UTC grids."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.emhass_companion.water_tank import (
    HotWaterPeriod,
    WaterTankConfig,
    hot_water_periods_from_data,
)

pytestmark = pytest.mark.usefixtures("stockholm_timezone")
STOCKHOLM = ZoneInfo("Europe/Stockholm")


def _local(day, hour=0):
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=STOCKHOLM).astimezone(UTC)


def _tank(periods):
    return WaterTankConfig(
        current_temperature=42,
        volume_litres=200,
        nominal_power_w=2000,
        standby_loss_w=50,
        comfort_temperature=45,
        setback_temperature=37,
        max_temperature=52,
        comfort_start=time(0),
        comfort_end=time(0),
        draw_start=None,
        draw_end=None,
        draw_energy_kwh=0,
        periods=periods,
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_period_omits_draw_and_temperature_window(enabled):
    tank = _tank((HotWaterPeriod(time(6), time(8), 3, enabled=enabled),))
    payload = tank.to_emhass(_local("2026-11-03"), timedelta(minutes=30), 48)
    assert sum(payload["draw_off_demand"]) == pytest.approx(3 if enabled else 0)
    if enabled:
        assert payload["min_temperatures"][12] == 45
    else:
        assert payload["min_temperatures"] == [37] * 48


def test_disabled_overlapping_period_does_not_add_demand_or_extend_availability():
    active = HotWaterPeriod(time(6), time(8), 3)
    disabled = HotWaterPeriod(time(7), time(12), 10, enabled=False)
    start = _local("2026-11-03")
    step = timedelta(minutes=15)
    assert _tank((active, disabled)).to_emhass(start, step, 96) == _tank((active,)).to_emhass(
        start, step, 96
    )


@pytest.mark.parametrize("minutes", [15, 30, 60])
def test_morning_and_evening_have_independent_energy_and_shared_targets(minutes):
    tank = _tank(
        (
            HotWaterPeriod(time(6), time(8), 3, "Morning"),
            HotWaterPeriod(time(18), time(20), 2, "Evening"),
        )
    )
    step = timedelta(minutes=minutes)
    count = 24 * 60 // minutes
    payload = tank.to_emhass(_local("2026-11-03"), step, count)
    demand = payload["draw_off_demand"]
    per_hour = 60 // minutes
    assert sum(demand) == pytest.approx(5)
    assert sum(demand[6 * per_hour : 8 * per_hour]) == pytest.approx(3)
    assert sum(demand[18 * per_hour : 20 * per_hour]) == pytest.approx(2)
    minimums = payload["min_temperatures"]
    for hour in (6, 7, 18, 19):
        assert minimums[hour * per_hour] == 45
    for hour in (0, 9, 12, 21):
        assert minimums[hour * per_hour] == 37
    if minutes < 60:
        assert 37 < minimums[17 * per_hour + 30 // minutes] < 45
    assert payload["max_temperatures"] == [52] * count


@pytest.mark.parametrize("day", ["2026-03-29", "2026-10-25"])
def test_multiple_periods_conserve_daily_energy_through_dst(day):
    start = _local(day)
    end = (
        (start.astimezone(STOCKHOLM).replace(tzinfo=None) + timedelta(days=1))
        .replace(tzinfo=STOCKHOLM)
        .astimezone(UTC)
    )
    tank = _tank((HotWaterPeriod(time(1), time(4), 3), HotWaterPeriod(time(18), time(20), 2)))
    step = timedelta(minutes=15)
    count = (end - start) // step
    demand = tank.draw_off_demand(start, step, count)
    assert sum(demand) == pytest.approx(5)
    assert tank.draw_off_demand(start, step, 13) + tank.draw_off_demand(
        start + 13 * step, step, count - 13
    ) == pytest.approx(demand)


def test_overnight_and_overlapping_periods_add_withdrawals_not_temperatures():
    tank = _tank((HotWaterPeriod(time(23), time(1), 2), HotWaterPeriod(time(0), time(2), 4)))
    payload = tank.to_emhass(_local("2026-11-03"), timedelta(hours=1), 24)
    assert sum(payload["draw_off_demand"]) == pytest.approx(6)
    assert payload["draw_off_demand"][:3] == pytest.approx([3, 2, 0])
    assert payload["draw_off_demand"][23] == pytest.approx(1)
    # The schedule itself; to_emhass lowers floors this small heater cannot reach.
    minimums, _ = tank.comfort_band(_local("2026-11-03"), timedelta(hours=1), 24)
    assert minimums[:2] == [45, 45]
    assert minimums[23] == 45


def test_zero_draw_still_requires_hot_water_during_the_period():
    tank = _tank((HotWaterPeriod(time(6), time(8), 0),))
    payload = tank.to_emhass(_local("2026-11-03"), timedelta(hours=1), 24)
    assert payload["draw_off_demand"] == [0] * 24
    assert payload["min_temperatures"][6:8] == [45, 45]
    assert payload["min_temperatures"][12] == 37


def test_full_day_period_and_partial_horizon():
    tank = _tank((HotWaterPeriod(time(0), time(0), 24), HotWaterPeriod(time(6), time(8), 4)))
    payload = tank.to_emhass(_local("2026-11-03", 7), timedelta(minutes=30), 4)
    assert payload["draw_off_demand"] == pytest.approx([1.5, 1.5, 0.5, 0.5])
    minimums, _ = tank.comfort_band(_local("2026-11-03", 7), timedelta(minutes=30), 4)
    assert minimums == [45] * 4


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        [],
        [{}],
        [False],
        [{"draw_start": "bad", "draw_end": "08:00", "draw_energy_kwh": 1}],
        [{"draw_start": "06:00+01:00", "draw_end": "08:00", "draw_energy_kwh": 1}],
        [{"draw_start": "06:00", "draw_end": "08:00", "draw_energy_kwh": 1}] * 9,
    ],
)
def test_malformed_stored_periods_fail_closed(data):
    with pytest.raises(ValueError):
        hot_water_periods_from_data(data)


@pytest.mark.parametrize("energy", [True, "3", -1, float("nan"), float("inf")])
def test_invalid_energy_rejected(energy):
    with pytest.raises(ValueError):
        hot_water_periods_from_data(
            [{"draw_start": "06:00", "draw_end": "08:00", "draw_energy_kwh": energy}]
        )


def test_valid_stored_periods_parse_to_immutable_values():
    periods = hot_water_periods_from_data(
        [
            {
                "name": "Morning",
                "draw_start": "06:00:00",
                "draw_end": "08:00:00",
                "draw_energy_kwh": 3,
            }
        ]
    )
    assert periods == (HotWaterPeriod(time(6), time(8), 3, "Morning"),)
    with pytest.raises(ValueError, match="periods"):
        _tank(())

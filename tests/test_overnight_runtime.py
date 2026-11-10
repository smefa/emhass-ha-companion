"""Scalar daily runtime belongs to an overnight window, not a calendar date."""

from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.util import dt as dt_util
import pytest

from custom_components.emhass_companion.const import (
    ACTION_MPC,
    RECURRENCE_ON_DEMAND,
    RECURRENCE_SURPLUS,
)
from custom_components.emhass_companion.deferrable import DeferrableRuntime
from custom_components.emhass_companion.entity import EmhassLoadEntity
from custom_components.emhass_companion.models import (
    BatteryConfig,
    GridConfig,
    HybridInverterConfig,
)
from custom_components.emhass_companion.payload import PayloadInputs, build_payload
from custom_components.emhass_companion.sensor import LoadRuntimeTodaySensor

pytestmark = pytest.mark.usefixtures("stockholm_timezone")


def moment(day, hour, minute=0, month=7):
    return dt_util.as_utc(
        datetime(2026, month, day, hour, minute, tzinfo=dt_util.DEFAULT_TIME_ZONE)
    )


def load_for_night(**overrides):
    return DeferrableRuntime(
        **{
            "subentry_id": "night",
            "name": "Night load",
            "nominal_power_w": 2000,
            "operating_hours": 2,
            "use_time_window": True,
            "earliest_start": time(22),
            "latest_end": time(6),
            **overrides,
        }
    )


def payload(load, now, horizon_steps=192):
    return build_payload(
        PayloadInputs(
            action=ACTION_MPC,
            now=now,
            time_step_minutes=15,
            horizon_steps=horizon_steps,
            battery=BatteryConfig(),
            grid=GridConfig(),
            hybrid_inverter=HybridInverterConfig(),
            backend_version="0.18.8",
            loads=[load.to_load(now, 15)],
        )
    ).payload


def test_midnight_preserves_seven_steps_and_next_evening_owes_full_run():
    load = load_for_night()
    load.observe_power(2000, moment(28, 22, 15))
    midnight = moment(29, 0)
    load.reset_day(midnight)
    load.observe_power(0, midnight)
    result = payload(load, midnight)
    assert load.elapsed_today(midnight) == timedelta(minutes=105)
    assert result["def_current_operating_timesteps"][0] == 7
    assert result["operating_hours_of_each_deferrable_load"][0] == 2
    assert 8 - result["def_current_operating_timesteps"][0] == 1
    assert load.elapsed_today(moment(29, 6)) == timedelta()
    result = payload(load, moment(29, 22))
    assert result["def_current_operating_timesteps"][0] == 0
    assert result["operating_hours_of_each_deferrable_load"][0] == 2


@pytest.mark.parametrize("horizon_steps", [96, 192])
def test_rounded_horizon_after_close_does_not_credit_last_night(horizon_steps):
    load = load_for_night()
    load.observe_power(2000, moment(28, 22, 15))
    load.observe_power(0, moment(29, 0))
    # Row zero rounds to 06:00, so the first remaining window is tonight.
    now = moment(29, 5, 59)
    assert load.completed_timesteps(now, 15) == 7
    assert payload(load, now, horizon_steps)["def_current_operating_timesteps"][0] == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"earliest_start": time(8), "latest_end": time(20)},
        {"use_time_window": False},
        {"recurrence": RECURRENCE_ON_DEMAND},
        {"recurrence": RECURRENCE_SURPLUS},
        {"load_type": "thermal"},
    ],
)
def test_other_daily_counters_still_reset_at_midnight(overrides):
    load = load_for_night(**overrides)
    load.observe_power(2000, moment(28, 22, 15))
    load.reset_day(moment(29, 0))
    assert load.elapsed_today(moment(29, 0)) == timedelta()


@pytest.mark.parametrize(("month", "day", "hours"), [(3, 28, 7), (10, 24, 9)])
def test_dst_nights_count_elapsed_time_and_split_at_local_close(month, day, hours):
    load = load_for_night()
    load.observe_power(2000, moment(day, 22, month=month))
    load.reset_day(moment(day + 1, 0, month=month))
    assert load.elapsed_today(moment(day + 1, 5, 45, month)) == timedelta(hours=hours, minutes=-15)
    # No event at close: the first later reading must still split at 06:00.
    load.observe_power(0, moment(day + 1, 6, 15, month))
    assert load.elapsed_today(moment(day + 1, 6, 15, month)) == timedelta(minutes=15)


@pytest.mark.parametrize(("restart_hour", "expected"), [(1, 1.75), (7, 0)])
async def test_restore_uses_window_day(monkeypatch, restart_hour, expected):
    load = load_for_night()
    sensor = object.__new__(LoadRuntimeTodaySensor)
    sensor.load = load
    monkeypatch.setattr(EmhassLoadEntity, "async_added_to_hass", AsyncMock())
    monkeypatch.setattr(
        sensor,
        "async_get_last_sensor_data",
        AsyncMock(return_value=SimpleNamespace(native_value=1.75)),
    )
    monkeypatch.setattr(
        sensor,
        "async_get_last_state",
        AsyncMock(return_value=SimpleNamespace(last_updated=moment(28, 23, 59))),
    )
    monkeypatch.setattr(dt_util, "utcnow", lambda: moment(29, restart_hour))
    await sensor.async_added_to_hass()
    load.apply_restored_runtime(moment(29, restart_hour))
    assert load.elapsed_today(moment(29, restart_hour)) == timedelta(hours=expected)


def test_restore_is_judged_against_the_window_restored_later():
    """The sensor restores before the time entities, so the decision waits.

    Seeded 22:00-06:00, moved to 22:00-08:00 through the time entity: at 07:00
    last night's run still belongs to this window and must be kept.
    """
    load = load_for_night()
    load.restored_runtime = (timedelta(hours=1.75), moment(28, 23, 59))
    load.latest_end = time(8)  # the time entity's restore
    load.apply_restored_runtime(moment(29, 7))
    assert load.restored_runtime is None
    assert load.elapsed_today(moment(29, 7)) == timedelta(hours=1.75)


def test_pending_request_survives_window_close():
    load = load_for_night()
    load.request(moment(28, 22))
    load.observe_power(2000, moment(28, 22))
    load.reset_day(moment(29, 0))
    load.observe_power(0, moment(29, 6))
    assert load.requested
    assert load.elapsed_since_request(moment(29, 6)) == timedelta(hours=8)


def test_delayed_plan_readings_cannot_recredit_previous_window():
    load = load_for_night()
    load.observe_power(2000, moment(28, 22, 15))
    load.observe_power(0, moment(29, 0))
    # A sensor read rolls the counter before the next solve replays its plan.
    assert load.elapsed_today(moment(29, 6)) == timedelta()
    load.observe_power(2000, moment(29, 5, 45))
    assert load.elapsed_today(moment(29, 6, 15)) == timedelta(minutes=15)
    load.observe_power(0, moment(29, 6, 15))
    assert load.elapsed_today(moment(29, 6, 30)) == timedelta(minutes=15)


def test_close_inside_spring_forward_gap_splits_at_first_real_instant():
    # 02:30 does not exist on 2026-03-29 in Stockholm; the day turns at 03:00.
    load = load_for_night(latest_end=time(2, 30))
    load.observe_power(2000, moment(28, 22, month=3))
    load.reset_day(moment(29, 0, month=3))
    load.observe_power(0, moment(29, 3, 15, month=3))
    assert load.elapsed_today(moment(29, 3, 15, month=3)) == timedelta(minutes=15)


def test_midnight_catches_up_a_missed_close_without_losing_the_day():
    load = load_for_night()
    load.observe_power(2000, moment(28, 22))
    load.reset_day(moment(29, 0))
    # No readings or runtime reads after this until the next midnight.
    load.reset_day(moment(30, 0))
    # 06:00 to midnight belongs to the window day that started at 06:00.
    assert load.elapsed_today(moment(30, 0)) == timedelta(hours=18)


def test_repeated_autumn_hour_after_the_close_is_the_new_day():
    # 2026-10-25: 02:00-03:00 happens twice. A 02:30 close is the first 02:30
    # (00:30 UTC); 02:15 in the repeated hour (01:15 UTC) is after it.
    load = load_for_night(latest_end=time(2, 30))
    first_hour = datetime(2026, 10, 25, 0, 15, tzinfo=UTC)
    repeated_hour = datetime(2026, 10, 25, 1, 15, tzinfo=UTC)
    assert load.runtime_date(first_hour) == date(2026, 10, 24)
    assert load.runtime_date(repeated_hour) == date(2026, 10, 25)

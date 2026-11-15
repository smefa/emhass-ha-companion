"""Weekly masks use local wall time and conservative slot ceilings."""

from datetime import UTC, datetime, time, timedelta

import pytest

from custom_components.emhass_companion.load_availability import WeeklyAvailability, power_limit
from custom_components.emhass_companion.models import DeferrableLoad
from custom_components.emhass_companion.payload import PayloadError, build_payload
from tests.test_payload import _inputs


@pytest.mark.parametrize("value", [-1, "bad", float("nan"), float("inf"), True, None])
def test_invalid_power_is_rejected(value):
    with pytest.raises(ValueError, match="power_limit_w"):
        power_limit({"power_limit_w": value}, 6000)


def test_ceiling_default_and_clamp():
    assert power_limit({}, 6000) == 6000
    assert power_limit({"power_limit_w": 10000}, 6000) == 6000
    assert power_limit({"power_limit_w": "3000"}, 6000) == 3000


@pytest.mark.usefixtures("stockholm_timezone")
def test_partial_slot_and_exclusive_end():
    schedule = WeeklyAvailability.parse(
        {
            "tuesday": [
                {"from": "12:00", "to": "12:20", "data": {"power_limit_w": 3000}},
                {"from": "12:20", "to": "13:00"},
            ]
        },
        6000,
    )
    start = datetime(2026, 7, 28, 10, tzinfo=UTC)
    assert schedule.step_ceiling(start, timedelta(minutes=30)) == 3000
    assert schedule.ceiling(start + timedelta(hours=1)) == 0
    assert schedule.step_ceiling(start + timedelta(minutes=45), timedelta(minutes=30)) == 0
    assert schedule.ceiling(start + timedelta(days=1)) == 0


@pytest.mark.usefixtures("stockholm_timezone")
def test_midnight_time_max_from_ha_service():
    schedule = WeeklyAvailability.parse(
        {
            "tuesday": [{"from": time(22), "to": time.max}],
            "wednesday": [{"from": time(), "to": time(6)}],
        },
        6000,
    )
    start = datetime(2026, 7, 28, 21, 45, tzinfo=UTC)
    assert schedule.step_ceiling(start, timedelta(minutes=30)) == 6000


@pytest.mark.usefixtures("stockholm_timezone")
@pytest.mark.parametrize(
    "start", [datetime(2026, 10, 25, 0, 30, tzinfo=UTC), datetime(2026, 10, 25, 1, 30, tzinfo=UTC)]
)
def test_both_dst_folds_follow_same_wall_clock(start):
    schedule = WeeklyAvailability.parse({"sunday": [{"from": "02:00", "to": "03:00"}]}, 6000)
    assert schedule.step_ceiling(start, timedelta(minutes=30)) == 6000
    assert schedule.step_ceiling(start, timedelta(minutes=45)) == (6000 if start.hour == 0 else 0)


@pytest.mark.usefixtures("stockholm_timezone")
def test_spring_dst_gap_does_not_create_available_time():
    schedule = WeeklyAvailability.parse({"sunday": [{"from": "02:00", "to": "03:00"}]}, 6000)
    assert (
        schedule.step_ceiling(datetime(2026, 3, 29, 0, 45, tzinfo=UTC), timedelta(minutes=30)) == 0
    )


def _scheduled_inputs(**kwargs):
    load = DeferrableLoad(
        "car", "Car", nominal_power_w=6000, operating_hours=1, semi_continuous=False
    )
    schedule = WeeklyAvailability.parse(
        {
            "tuesday": [
                {"from": "10:00", "to": "12:00", "data": {"power_limit_w": 3000}},
                {"from": "13:00", "to": "16:00"},
            ]
        },
        6000,
    )
    return _inputs(
        loads=[load],
        backend_version="0.18.9",
        load_availability={"car": schedule},
        load_power_controls={"car"},
        **kwargs,
    )


def test_payload_masks_and_stable_load_order():
    inputs = _scheduled_inputs()
    inputs.loads.append(DeferrableLoad("other", "Other", 1000, 1))
    payload = build_payload(inputs).payload
    mask, other = payload["def_availability"]
    assert mask[:14] == [0.5] * 4 + [0] * 2 + [1] * 6 + [0] * 2
    assert other is None
    assert payload["number_of_deferrable_loads"] == 2


@pytest.mark.parametrize(
    "version,action", [("0.18.8", "naive-mpc-optim"), ("0.18.9", "dayahead-optim")]
)
def test_unsupported_requests_are_not_sent(version, action):
    inputs = _scheduled_inputs()
    inputs.backend_version, inputs.action = version, action
    with pytest.raises(PayloadError, match=r"0\.18\.9"):
        build_payload(inputs)


def test_fixed_power_and_no_control_cannot_be_derated():
    inputs = _scheduled_inputs()
    inputs.load_power_controls.clear()
    assert build_payload(inputs).payload["def_availability"][0][:4] == [0] * 4
    inputs.load_power_controls.add("car")
    inputs.loads[0].semi_continuous = True
    assert build_payload(inputs).payload["def_availability"][0][:4] == [0] * 4


def test_run_now_bypasses_closed_time_but_keeps_power_ceiling():
    inputs = _scheduled_inputs(load_schedule_overrides={"car"})
    mask = build_payload(inputs).payload["def_availability"][0]
    assert mask[:8] == [0.5] * 4 + [1] * 4


def test_unfittable_sequence_is_parked_without_dropping_request():
    inputs = _scheduled_inputs()
    inputs.loads[0].power_sequence = (6000, 4000)
    inputs.loads[0].operating_hours = 4
    result = build_payload(inputs)
    assert result.payload["operating_timesteps_of_each_deferrable_load"] == [0]
    assert any("request retained" in warning for warning in result.warnings)
    assert inputs.loads[0].wants_to_run


def test_running_sequence_finishes_outside_allowed_times():
    inputs = _scheduled_inputs()
    inputs.loads[0].cycle_anchor = inputs.now
    inputs.loads[0].cycle_values = (6000, 4000)
    assert build_payload(inputs).payload["def_availability"] == [None]


@pytest.mark.usefixtures("stockholm_timezone")
def test_dst_rewind_can_cross_into_closed_time_without_block_boundary():
    schedule = WeeklyAvailability.parse({"sunday": [{"from": "02:30", "to": "03:00"}]}, 6000)
    assert (
        schedule.step_ceiling(datetime(2026, 10, 25, 0, 45, tzinfo=UTC), timedelta(minutes=30)) == 0
    )


def test_number_bounds_and_step_are_reflected_in_plan():
    from custom_components.emhass_companion.load_availability import PowerControl

    inputs = _scheduled_inputs()
    inputs.load_power_limits["car"] = PowerControl(1400, 6000, 300, 1)
    assert build_payload(inputs).payload["def_availability"][0][0] == pytest.approx(2900 / 6000)


@pytest.mark.usefixtures("stockholm_timezone")
def test_masks_cover_extra_daily_rows_and_keep_nearest_alignment():
    inputs = _scheduled_inputs()
    inputs.loads[0].repeats_by_calendar_day = True
    inputs.horizon_steps = 96
    inputs.now = inputs.now.replace(minute=20)
    schedule = WeeklyAvailability.parse(
        {
            "tuesday": [{"from": "13:00", "to": "16:00"}],
            "wednesday": [{"from": "12:00", "to": "15:00"}],
        },
        6000,
    )
    inputs.load_availability["car"] = schedule
    payload = build_payload(inputs).payload
    masks = payload["def_availability"]
    assert len(masks) == payload["number_of_deferrable_loads"] == 2
    assert len(masks[0]) == len(masks[1]) == 96
    assert masks[0] == masks[1]
    assert masks[0][0] == masks[0][7] == 0
    assert masks[0][1] == 1  # nearest row zero is 12:30 local; 13:00 is index 1


def test_unreadable_load_is_parked_and_the_rest_still_planned():
    inputs = _scheduled_inputs(load_unavailable={"other": "power control is unavailable"})
    inputs.loads.append(DeferrableLoad("other", "Other", 1000, 1))
    result = build_payload(inputs)
    assert result.payload["operating_hours_of_each_deferrable_load"] == [1, 0.0]
    assert result.payload["def_availability"][1] is None
    assert any("Other: power control is unavailable" in text for text in result.warnings)

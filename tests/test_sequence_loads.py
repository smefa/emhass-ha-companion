"""Sequence programs keep one clock across payloads, gate changes and restarts."""

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace

from homeassistant.exceptions import ServiceValidationError
import pytest

from custom_components.emhass_companion.const import ACTION_DAYAHEAD, ACTION_MPC, LOAD_MODE_AUTO
from custom_components.emhass_companion.deferrable import (
    DeferrableRegistry,
    DeferrableRuntime,
    expand_power,
    parse_power,
)
from custom_components.emhass_companion.executor import Executor
from custom_components.emhass_companion.models import (
    BatteryConfig,
    GridConfig,
    HybridInverterConfig,
    Plan,
    PlanRow,
)
from custom_components.emhass_companion.payload import (
    PayloadInputs,
    _deferrable_settings,
    _extra_window_rows,
)
from custom_components.emhass_companion.switch import _set_enabled

NOW = datetime(2026, 7, 28, 10, tzinfo=UTC)
STEP = timedelta(minutes=15)


def load(**kwargs):
    return DeferrableRuntime(
        **{
            **dict(
                subentry_id="washer",
                name="Washer",
                nominal_power_w=2000,
                power_sequence=(150, 2000, 0),
                operating_hours=1,
            ),
            **kwargs,
        }
    )


def inputs(runtime, **kwargs):
    return PayloadInputs(
        **{
            **dict(
                action=ACTION_MPC,
                now=NOW,
                time_step_minutes=15,
                horizon_steps=96,
                battery=BatteryConfig(),
                grid=GridConfig(),
                hybrid_inverter=HybridInverterConfig(),
                backend_version="0.18.8",
                loads=[runtime.to_load(NOW, 15)],
            ),
            **kwargs,
        }
    )


def row(runtime, **kwargs):
    return _deferrable_settings(inputs(runtime, **kwargs), STEP)[0]


@pytest.mark.parametrize(
    "text", ["", "0,10", "9,20", "10,", "10,,20", "-10,20", "1e3", "10,100001", "10," * 128]
)
def test_invalid_power(text):
    with pytest.raises(ServiceValidationError):
        parse_power(text)


def test_parse_expand_and_runtime_validation():
    assert parse_power(" 500, 2000, 1000 ") == (500, 2000, 1000)
    assert parse_power(500) == (500,)
    assert expand_power((500, 2000, 1000), 1.5, 15) == (500, 2000, 1000, 1000, 1000, 1000)
    assert expand_power((500, 2000), 0, 15) == ()
    with pytest.raises(ServiceValidationError, match="needs at least"):
        expand_power((500, 2000, 1000), 0.5, 15)


@pytest.mark.parametrize(
    "state", ["disabled", "not_requested", "done", "zero", "invalid", "old_backend"]
)
def test_non_owing_is_scalar_park(state):
    runtime = load()
    overrides = {}
    if state == "disabled":
        runtime.enabled = False
    if state == "not_requested":
        runtime.recurrence = "on_demand"
    if state == "done":
        runtime.last_completed_day = NOW.date()
    if state == "zero":
        runtime.operating_hours = 0
    if state == "invalid":
        runtime.operating_hours = 0.25
    if state == "old_backend":
        overrides["backend_version"] = "0.18.7"
    payload = row(runtime, **overrides)
    assert payload["nominal_power_of_deferrable_loads"] == [2000]
    assert payload["operating_hours_of_each_deferrable_load"] == [0]


def test_owing_expands_and_neutralises_ignored_constraints():
    runtime = load(single_constant=True, minimum_power_w=100, max_startups=2)
    payload = row(runtime)
    assert payload["nominal_power_of_deferrable_loads"] == [[150, 2000, 0, 0]]
    assert payload["set_deferrable_load_single_constant"] == [False]
    assert payload["minimum_power_of_deferrable_loads"] == [0]
    assert payload["set_deferrable_max_startups"] == [0]


def test_cycle_snapshot_nearest_row_zero_open_window_and_dayahead():
    runtime = load(
        use_time_window=True, earliest_start=time(22), latest_end=time(23), recurrence="on_demand"
    )
    runtime.force_run(NOW)
    assert runtime.mode == LOAD_MODE_AUTO and runtime.sequence_start_pending
    runtime.observe_command(True, NOW + timedelta(minutes=14))
    cycle = runtime.sequence_cycle
    assert cycle.anchor == NOW + STEP
    runtime.operating_hours = 2
    runtime.power_sequence = (500, 1000)
    payload = row(runtime, now=NOW + timedelta(minutes=38), row_zero_mode="nearest")
    assert payload["def_current_operating_timesteps"] == [2]
    assert payload["def_current_state"] == [True]
    assert payload["start_timesteps_of_each_deferrable_load"] == [0]
    assert payload["end_timesteps_of_each_deferrable_load"] == [0]
    assert payload["nominal_power_of_deferrable_loads"] == [[150, 2000, 0, 0]]
    parked = row(runtime, action=ACTION_DAYAHEAD)
    assert parked["operating_hours_of_each_deferrable_load"] == [0]
    assert isinstance(parked["nominal_power_of_deferrable_loads"][0], (int, float))


def test_zero_soak_gate_off_and_repeat_run_now_do_not_end_or_restart_cycle():
    runtime = load(power_sensor="sensor.watts", recurrence="on_demand")
    runtime.request(NOW)
    runtime.force_run(NOW)
    runtime.observe_command(False, NOW)
    assert runtime.sequence_cycle is None
    runtime.observe_command(True, NOW)
    cycle = runtime.sequence_cycle
    runtime.observe_power(2000, NOW)
    runtime.observe_power(0, NOW + STEP)
    runtime.force_run(NOW + STEP)
    runtime.observe_command(False, NOW + STEP)
    assert not runtime.check_auto_disarm(NOW + 3 * STEP, 15)
    assert runtime.sequence_cycle is cycle
    assert not runtime.mid_program(NOW + STEP, 15)
    assert not runtime.in_completion_hold(NOW + STEP, 15)
    assert runtime.check_auto_disarm(NOW + 4 * STEP, 15)
    assert runtime.last_completion_reason == "completed"
    assert not runtime.requested


def test_request_midcycle_absorbed_and_user_cancel_restarts_at_zero():
    runtime = load(recurrence="on_demand")
    runtime.force_run(NOW)
    runtime.observe_command(True, NOW)
    cycle = runtime.sequence_cycle
    runtime.request(NOW + STEP)
    assert runtime.sequence_cycle is cycle and cycle.serves_request
    runtime.check_auto_disarm(NOW + 4 * STEP, 15)
    assert not runtime.requested
    runtime.force_run(NOW + 5 * STEP)
    runtime.observe_command(True, NOW + 5 * STEP)
    _set_enabled(runtime, False)
    assert runtime.sequence_cycle is None and not runtime.sequence_start_pending
    runtime.enabled = True
    runtime.force_run(NOW + 6 * STEP)
    runtime.observe_command(True, NOW + 6 * STEP)
    assert runtime.sequence_cycle.elapsed(NOW + 6 * STEP, 15) == 0


def test_daily_credit_and_explicit_run_now_after_completion():
    runtime = load()
    runtime.observe_command(True, NOW)
    runtime.check_auto_disarm(NOW + 4 * STEP, 15)
    assert runtime.last_completed_day == NOW.date()
    runtime.observe_command(True, NOW + 5 * STEP)
    assert runtime.sequence_cycle is None
    runtime.force_run(NOW + 5 * STEP)
    runtime.observe_command(True, NOW + 5 * STEP)
    assert runtime.sequence_cycle.source == "run_now"


@pytest.mark.parametrize(
    "metered,seen,reason",
    [(False, False, "completed"), (True, False, "never_started"), (True, True, "cut_short")],
)
def test_completion_reason_and_one_step_extension(metered, seen, reason):
    runtime = load(power_sensor="sensor.w" if metered else None)
    runtime.observe_command(True, NOW)
    if seen:
        runtime.observe_power(2000, NOW)
    assert runtime.check_auto_disarm(NOW + 4 * STEP, 15) is not seen
    if seen:
        assert runtime.check_auto_disarm(NOW + 5 * STEP, 15)
    assert runtime.last_completion_reason == reason


@pytest.mark.parametrize("start,end", [(time(11), time(11, 30)), (time(9, 45), time(10, 15))])
def test_narrow_standing_window_parks(start, end):
    runtime = load(use_time_window=True, earliest_start=start, latest_end=end)
    settings, _, warnings, _ = _deferrable_settings(inputs(runtime), STEP)
    assert settings["operating_hours_of_each_deferrable_load"] == [0]
    assert any("sequence parked" in warning for warning in warnings)


def test_window_near_horizon_parks():
    runtime = load(use_time_window=True, earliest_start=time(11), latest_end=time(13))
    assert row(runtime, horizon_steps=6)["operating_hours_of_each_deferrable_load"] == [0]


@pytest.mark.parametrize("use_plan", [True, False])
def test_executor_holds_run_now_without_request_on_untrusted_ticks(use_plan, monkeypatch):
    monkeypatch.setattr("custom_components.emhass_companion.executor.dt_util.utcnow", lambda: NOW)
    runtime = load(recurrence="on_demand")
    runtime.force_run(NOW)
    fake = SimpleNamespace(
        coordinator=SimpleNamespace(
            loads=SimpleNamespace(all=lambda: [runtime]),
            config=SimpleNamespace(time_step_minutes=15),
        ),
        _scheduled=lambda _: False,
    )
    assert Executor._decide_loads(fake, use_plan=use_plan) == {"washer": True}
    runtime.observe_command(True, NOW)
    assert Executor._decide_loads(fake, use_plan=use_plan) == {"washer": True}


def test_executor_completes_an_expired_cycle_without_a_solve(monkeypatch):
    """Completion must not depend on the coordinator's disarm pass, which a
    failing optimisation never reaches; otherwise the hold keeps the load on."""
    runtime = load(recurrence="on_demand")
    runtime.force_run(NOW)
    runtime.observe_command(True, NOW)
    assert runtime.sequence_cycle_active
    later = NOW + timedelta(hours=3)
    monkeypatch.setattr("custom_components.emhass_companion.executor.dt_util.utcnow", lambda: later)
    fake = SimpleNamespace(
        coordinator=SimpleNamespace(
            loads=SimpleNamespace(all=lambda: [runtime]),
            config=SimpleNamespace(time_step_minutes=15),
        ),
        _scheduled=lambda _: False,
    )
    assert Executor._decide_loads(fake, use_plan=True) == {"washer": False}
    assert not runtime.sequence_cycle_active


def test_low_first_stage_starts_at_ten_watts(monkeypatch):
    runtime = load()
    monkeypatch.setattr("custom_components.emhass_companion.executor.dt_util.utcnow", lambda: NOW)
    fake = SimpleNamespace(
        coordinator=SimpleNamespace(
            data=SimpleNamespace(
                plan=Plan(
                    generated_at=NOW,
                    schema_version="1",
                    rows=[PlanRow(timestamp=NOW, deferrables=[150])],
                ),
                deferrable_index=lambda _: 0,
            )
        )
    )
    assert Executor._scheduled(fake, runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("after", [15, 60, 90])
async def test_restore_cycle_or_credit(hass, after):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.emhass_companion.const import DOMAIN

    entry = MockConfigEntry(domain=DOMAIN)
    registry = DeferrableRegistry(hass, entry)
    runtime = load()
    registry._loads = {"washer": runtime}
    await registry.async_load_sequences(15)
    runtime.observe_command(True, NOW)
    if after == 90:
        runtime.check_auto_disarm(NOW + 4 * STEP, 15)
    await registry._sequence_store.async_save(
        {
            "washer": {
                "cycle": runtime.sequence_cycle.as_dict() if runtime.sequence_cycle else None,
                "start_pending": False,
                "last_completed_day": runtime.last_completed_day.isoformat()
                if runtime.last_completed_day
                else None,
            }
        }
    )
    restored = DeferrableRegistry(hass, entry)
    restored._loads = {"washer": load()}
    await restored.async_load_sequences(15)
    restored.check_auto_disarm(NOW + timedelta(minutes=after), 15)
    saved = restored.get("washer")
    if after == 15:
        assert saved.sequence_cycle.elapsed(NOW + STEP, 15) == 1
    else:
        assert saved.sequence_cycle is None and saved.last_completed_day == NOW.date()


def test_config_clean_scalar_shape_and_sequence_split():
    from custom_components.emhass_companion.config_flow import _clean_deferrable

    assert _clean_deferrable({"nominal_power_w": "500"}) == {"nominal_power_w": 500.0}
    assert _clean_deferrable({"nominal_power_w": "500, 2000"}) == {
        "nominal_power_w": 2000.0,
        "power_sequence": [500.0, 2000.0],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("power,expected", [("500", None), ("150, 2000, 0", [150, 2000, 0])])
async def test_flow_setup_and_reconfigure_split(hass, monkeypatch, power, expected):
    from custom_components.emhass_companion.config_flow import DeferrableLoadSubentryFlow

    flow = DeferrableLoadSubentryFlow()
    flow.hass = hass
    runtime = load()
    subentry = SimpleNamespace(
        subentry_id="washer",
        data={
            "name": "Washer",
            "nominal_power_w": 2000,
            "power_sequence": [150, 2000, 0],
            "operating_hours": 1,
        },
    )
    entry = SimpleNamespace(
        entry_id="test",
        options={},
        runtime_data=SimpleNamespace(loads=SimpleNamespace(get=lambda _: runtime)),
    )
    monkeypatch.setattr(flow, "_get_entry", lambda: entry)
    monkeypatch.setattr(flow, "_get_reconfigure_subentry", lambda: subentry)
    monkeypatch.setattr(flow, "async_update_and_abort", lambda *a, **kw: kw)
    monkeypatch.setattr(flow, "async_show_form", lambda **kw: kw)
    monkeypatch.setattr(flow, "async_create_entry", lambda **kw: kw)
    flow._kind = {"name": "Washer", "recurrence": "daily"}
    created = await flow.async_step_settings({"nominal_power_w": power, "operating_hours": 1})
    assert created["data"].get("power_sequence") == expected
    updated = await flow.async_step_reconfigure({"name": "Washer", "nominal_power_w": power})
    assert updated["data"].get("power_sequence") == expected
    # Not written to the live runtime: the reload seeds it from the subentry,
    # and the stamp keeps the number's restore from undoing it.
    assert ("nominal_power_edited_at" in updated["data"]) is (expected is None)


@pytest.mark.asyncio
async def test_reconfigure_shows_runtime_scalar_and_preserves_unchanged_number(hass, monkeypatch):
    from custom_components.emhass_companion.config_flow import DeferrableLoadSubentryFlow

    flow = DeferrableLoadSubentryFlow()
    flow.hass = hass
    runtime = load(power_sequence=(), nominal_power_w=750)
    subentry = SimpleNamespace(
        subentry_id="washer", data={"name": "Washer", "nominal_power_w": 2000}
    )
    entry = SimpleNamespace(
        entry_id="test",
        options={},
        runtime_data=SimpleNamespace(loads=SimpleNamespace(get=lambda _: runtime)),
    )
    monkeypatch.setattr(flow, "_get_entry", lambda: entry)
    monkeypatch.setattr(flow, "_get_reconfigure_subentry", lambda: subentry)
    monkeypatch.setattr(flow, "async_show_form", lambda **kw: kw)
    monkeypatch.setattr(flow, "async_update_and_abort", lambda *a, **kw: kw)
    form = await flow.async_step_reconfigure()
    power_key = next(key for key in form["data_schema"].schema if str(key) == "nominal_power_w")
    assert power_key.default() == "750"
    await flow.async_step_reconfigure({"name": "Washer", "nominal_power_w": "750"})
    assert runtime.nominal_power_w == 750


@pytest.mark.asyncio
async def test_flow_rejects_too_many_stages_and_surplus(hass, monkeypatch):
    from custom_components.emhass_companion.config_flow import (
        DeferrableLoadSubentryFlow,
        ThermalLoadSubentryFlow,
    )

    flow = DeferrableLoadSubentryFlow()
    monkeypatch.setattr(flow, "_get_entry", lambda: SimpleNamespace(options={}))
    assert flow._form_errors({"nominal_power_w": "150, 2000, 0", "operating_hours": 0.25}) == {
        "nominal_power_w": "invalid_power_sequence"
    }
    assert flow._form_errors({"nominal_power_w": "150, 2000", "recurrence": "surplus"}) == {
        "nominal_power_w": "sequence_not_supported"
    }
    thermal = ThermalLoadSubentryFlow()
    assert thermal._form_errors({"nominal_power_w": "150, 2000"}) == {
        "nominal_power_w": "sequence_not_supported"
    }


@pytest.mark.asyncio
async def test_recurrence_cancel_and_surplus_guard():
    from unittest.mock import AsyncMock

    from custom_components.emhass_companion.select import LoadRecurrenceSelect

    runtime = load()
    runtime.observe_command(True, NOW)
    fake = SimpleNamespace(
        load=runtime, coordinator=SimpleNamespace(async_request_settings_refresh=AsyncMock())
    )
    with pytest.raises(ServiceValidationError, match="surplus"):
        await LoadRecurrenceSelect.async_select_option(fake, "surplus")
    assert runtime.sequence_cycle is not None
    await LoadRecurrenceSelect.async_select_option(fake, "on_demand")
    assert runtime.sequence_cycle is None


def test_running_sequence_counts_as_fixed_import_draw():
    from custom_components.emhass_companion.payload import _fit_pins_to_import_limit

    payload = {
        "maximum_power_from_grid": 2500,
        "nominal_power_of_deferrable_loads": [[150, 2000, 0], 1000],
        "def_current_state": [True, True],
        "def_current_operating_timesteps": [1, 0],
        "def_current_power": [0, 1000],
        "treat_deferrable_load_as_semi_cont": [True, True],
    }
    warnings = _fit_pins_to_import_limit(payload, ["Washer", "Car"], 0, NOW)
    assert warnings
    assert "def_current_power" not in payload


def test_attributes_keep_zero_stage_inside_one_cycle(monkeypatch):
    from custom_components.emhass_companion.binary_sensor import LoadShouldRunBinarySensor
    from custom_components.emhass_companion.models import Point, Series
    from custom_components.emhass_companion.sensor import LoadScheduledPowerSensor

    runtime = load()
    runtime.observe_command(True, NOW)
    monkeypatch.setattr(
        "custom_components.emhass_companion.binary_sensor.dt_util.utcnow", lambda: NOW + 2 * STEP
    )
    fake = SimpleNamespace(
        load=runtime,
        coordinator=SimpleNamespace(config=SimpleNamespace(time_step_minutes=15)),
        _scheduled_power=lambda: 0,
        _reason="sequence",
        deferrable_number=0,
    )
    attrs = LoadShouldRunBinarySensor.extra_state_attributes.fget(fake)
    assert attrs["power_sequence"] == [150, 2000, 0]
    assert attrs["sequence_step"] == 3 and attrs["sequence_steps"] == 4
    fake = SimpleNamespace(
        load=runtime,
        coordinator=SimpleNamespace(config=SimpleNamespace(time_step_minutes=15), data=None),
        _series=lambda: Series(
            [
                Point(NOW, 150),
                Point(NOW + STEP, 2000),
                Point(NOW + 2 * STEP, 0),
                Point(NOW + 3 * STEP, 1000),
            ]
        ),
    )
    assert LoadScheduledPowerSensor._cycles(fake) == [
        {"start": NOW.isoformat(), "end": (NOW + 4 * STEP).isoformat()}
    ]


@pytest.mark.asyncio
async def test_invalid_setup_submission_returns_error_form(hass, monkeypatch):
    from custom_components.emhass_companion.config_flow import DeferrableLoadSubentryFlow

    flow = DeferrableLoadSubentryFlow()
    flow.hass = hass
    flow._kind = {"name": "Washer", "recurrence": "daily"}
    monkeypatch.setattr(flow, "_get_entry", lambda: SimpleNamespace(options={}))
    monkeypatch.setattr(flow, "async_show_form", lambda **kw: kw)
    result = await flow.async_step_settings(
        {"nominal_power_w": "150, 2000, 0", "operating_hours": 0.25}
    )
    assert result["errors"] == {"nominal_power_w": "invalid_power_sequence"}


@pytest.mark.asyncio
async def test_persist_pending_request_and_completion_prevents_second_cycle(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    registry = DeferrableRegistry(hass, MockConfigEntry(domain="emhass_companion"))
    original = load(recurrence="on_demand")
    registry._loads = {"washer": original}
    await registry.async_load_sequences(15)
    original.force_run(NOW)
    await registry.async_save_sequences()
    pending = DeferrableRegistry(hass, registry.entry)
    pending._loads = {"washer": load(recurrence="on_demand")}
    await pending.async_load_sequences(15)
    restored = pending.get("washer")
    assert restored.sequence_start_pending and not restored.requested
    restored.observe_command(False, NOW)
    assert restored.sequence_cycle is None
    restored.observe_command(True, NOW)
    restored.request(NOW + STEP)
    await pending.async_save_sequences()
    resumed = DeferrableRegistry(hass, registry.entry)
    resumed._loads = {"washer": load(recurrence="on_demand")}
    await resumed.async_load_sequences(15)
    running = resumed.get("washer")
    assert running.requested and running.sequence_cycle.serves_request
    resumed.check_auto_disarm(NOW + 4 * STEP, 15)
    assert not running.requested
    await resumed.async_save_sequences()
    finished = DeferrableRegistry(hass, registry.entry)
    finished._loads = {"washer": load(recurrence="on_demand")}
    await finished.async_load_sequences(15)
    done = finished.get("washer")
    done.observe_command(True, NOW + 5 * STEP)
    assert not done.requested and done.sequence_cycle is None


def test_requested_off_cancels_pending_and_running():
    from custom_components.emhass_companion.switch import _set_requested

    runtime = load(recurrence="on_demand")
    runtime.force_run(NOW)
    _set_requested(runtime, False)
    assert not runtime.sequence_start_pending
    runtime.force_run(NOW)
    runtime.observe_command(True, NOW)
    _set_requested(runtime, False)
    assert runtime.sequence_cycle is None


def test_plan_start_threshold_matches_the_parser_minimum():
    """The parser accepts a 10 W first stage, so a 10 W plan row must start it."""
    runtime = load(power_sequence=(10, 0))
    assert runtime.plan_runs_at(10)
    assert not runtime.plan_runs_at(9.9)
    scalar = load(power_sequence=())
    assert not scalar.plan_runs_at(200)  # 10 % of 2000 W, exclusive as before
    assert scalar.plan_runs_at(201)


def test_cycles_attribute_counts_a_ten_watt_first_step_as_a_start():
    """Same inclusive 10 W threshold as the executor's planned start."""
    from custom_components.emhass_companion.models import Point, Series
    from custom_components.emhass_companion.sensor import LoadScheduledPowerSensor

    runtime = load(power_sequence=(10, 2000), operating_hours=0.5)
    fake = SimpleNamespace(
        load=runtime,
        coordinator=SimpleNamespace(config=SimpleNamespace(time_step_minutes=15), data=None),
        plan_index=None,
        _series=lambda: Series([Point(NOW, 0), Point(NOW + STEP, 10), Point(NOW + 2 * STEP, 2000)]),
    )
    assert LoadScheduledPowerSensor._cycles(fake) == [
        {"start": (NOW + STEP).isoformat(), "end": (NOW + 3 * STEP).isoformat()}
    ]


def test_run_now_with_zero_runtime_drops_the_pending_hold(monkeypatch):
    """A pending start that can never begin must not hold the load on."""
    runtime = load(recurrence="on_demand")
    runtime.force_run(NOW)
    assert runtime.sequence_start_pending
    runtime.operating_hours = 0
    monkeypatch.setattr("custom_components.emhass_companion.executor.dt_util.utcnow", lambda: NOW)
    fake = SimpleNamespace(
        coordinator=SimpleNamespace(
            loads=SimpleNamespace(all=lambda: [runtime]),
            config=SimpleNamespace(time_step_minutes=15),
        ),
        _scheduled=lambda _: False,
    )
    assert Executor._decide_loads(fake, use_plan=True) == {"washer": False}
    assert not runtime.sequence_start_pending
    assert runtime.sequence_cycle is None


def test_a_start_with_zero_runtime_clears_pending():
    runtime = load(recurrence="on_demand")
    runtime.force_run(NOW)
    runtime.operating_hours = 0
    runtime.observe_command(True, NOW)
    assert not runtime.sequence_start_pending
    assert runtime.sequence_cycle is None


def test_an_overnight_cycle_counts_for_the_evening_it_started():
    runtime = load(use_time_window=True, earliest_start=time(22), latest_end=time(6))
    evening = datetime(2026, 7, 28, 23, tzinfo=UTC)
    runtime.observe_command(True, evening)
    runtime.check_auto_disarm(evening + timedelta(hours=2, minutes=30), 15)
    assert runtime.sequence_cycle is None
    assert runtime.last_completed_day == evening.date()
    assert not runtime.sequence_owes(datetime(2026, 7, 29, 1, 30, tzinfo=UTC))
    # By noon the evening's window is the next day's run, so it owes again.
    assert runtime.sequence_owes(datetime(2026, 7, 29, 12, tzinfo=UTC))
    assert runtime.sequence_owes(datetime(2026, 7, 29, 22, 30, tzinfo=UTC))


def test_a_daytime_window_keeps_the_calendar_day():
    runtime = load(use_time_window=True, earliest_start=time(6), latest_end=time(22))
    assert (
        runtime.sequence_day(datetime(2026, 7, 29, 1, tzinfo=UTC)) == datetime(2026, 7, 29).date()
    )


@pytest.mark.asyncio
async def test_pending_start_is_not_restored_onto_a_single_value_load(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    registry = DeferrableRegistry(hass, MockConfigEntry(domain="emhass_companion"))
    registry._loads = {"washer": load(recurrence="on_demand")}
    await registry.async_load_sequences(15)
    registry.get("washer").force_run(NOW)
    await registry.async_save_sequences()
    reconfigured = DeferrableRegistry(hass, registry.entry)
    reconfigured._loads = {"washer": load(recurrence="on_demand", power_sequence=())}
    await reconfigured.async_load_sequences(15)
    assert not reconfigured.get("washer").sequence_start_pending


def test_a_short_rest_of_today_does_not_drop_tomorrows_window():
    """At 11:00 a 06:00-12:00 window has 4 steps left for an 8-step block:
    today's row parks, tomorrow's window must still be asked for."""
    now = datetime(2026, 7, 28, 11, tzinfo=UTC)
    runtime = load(
        power_sequence=(500, 2000),
        operating_hours=2,
        use_time_window=True,
        earliest_start=time(6),
        latest_end=time(12),
    )
    payload_inputs = inputs(runtime, now=now, horizon_steps=144, loads=[runtime.to_load(now, 15)])
    extras = _extra_window_rows(payload_inputs.loads, payload_inputs, STEP)
    assert extras
    _, _, _, row = extras[0]
    assert row.hours == 2
    assert row.nominal_power_w == [500, 2000] + [2000] * 6

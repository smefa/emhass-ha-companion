"""EMHASS ``prior_heat``: heat already in flight under ``thermal_inertia``.

The case behind it: a room with a 30-minute response delay on 15-minute steps
had been heating for almost a step when an MPC run started 0.01 K above its
floor. Without prior_heat EMHASS modelled the delay as unheated, the first
floor it holds fell below the minimum, and the whole run was infeasible.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace

from homeassistant.core import State
import pytest

from custom_components.emhass_companion import coordinator as coordinator_module
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRuntime
from custom_components.emhass_companion.models import ROW_ZERO_NEAREST, DeferrableLoad
from custom_components.emhass_companion.thermal import ThermalConfig, step_average_power

STEP = timedelta(minutes=15)
ROW_ZERO = datetime(2026, 11, 26, 6, 0, tzinfo=UTC)


def _room(**overrides) -> ThermalConfig:
    defaults = {
        "heating_rate": 1.0,
        "cooling_constant": 0.05,
        "thermal_inertia": 0.5,
        "comfort_temperature": 19.5,
        "setback_temperature": 19.5,
        "max_temperature": 23.0,
        "comfort_start": time(0, 0),
        "comfort_end": time(0, 0),
        "current_temperature": 19.51,
        "nominal_power_w": 2000.0,
    }
    return ThermalConfig(**{**defaults, **overrides})


# -- lag ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("inertia", "step_minutes", "lag"),
    [(0.0, 15, 0), (0.5, 15, 2), (0.75, 30, 1), (0.2, 15, 0), (1.0, 60, 1)],
)
def test_lag_steps_round_down_like_emhass(inertia, step_minutes, lag):
    assert _room(thermal_inertia=inertia).lag_steps(timedelta(minutes=step_minutes)) == lag


# -- averaging ---------------------------------------------------------------


def test_step_average_is_time_weighted_and_oldest_first():
    starts = [ROW_ZERO - 2 * STEP, ROW_ZERO - STEP]
    readings = [
        (ROW_ZERO - 2 * STEP, 0.0),  # include_start_time_state
        (ROW_ZERO - STEP + timedelta(seconds=30), 1900.0),
        (ROW_ZERO - timedelta(minutes=5), 1800.0),
    ]
    first, second = step_average_power(readings, starts, STEP)
    assert first == 0.0
    expected = (1900 * (9.5 * 60) + 1800 * (5 * 60)) / (15 * 60)
    assert second == pytest.approx(expected)


def test_unavailable_negative_and_missing_history_count_as_zero():
    starts = [ROW_ZERO - STEP]
    readings = [
        (ROW_ZERO - STEP + timedelta(minutes=5), None),
        (ROW_ZERO - STEP + timedelta(minutes=10), -50.0),
    ]
    assert step_average_power(readings, starts, STEP) == (0.0,)
    assert step_average_power([], starts, STEP) == (0.0,)


def test_last_reading_holds_to_row_zero_on_a_run_between_boundaries():
    # Nearest rounding can put row zero a few minutes after now; the pump is
    # still running until it reports otherwise.
    starts = [ROW_ZERO - STEP]
    readings = [(ROW_ZERO - STEP - timedelta(hours=1), 1500.0)]
    assert step_average_power(readings, starts, STEP) == (1500.0,)


# -- payload -----------------------------------------------------------------


def test_prior_heat_is_sent_oldest_first_when_measured():
    entry = _room(prior_heat=(0.0, 1844.04)).to_emhass(ROW_ZERO, STEP, 8)
    assert entry["prior_heat"] == [0.0, 1844.0]
    assert entry["thermal_inertia"] == 0.5


def test_prior_heat_is_omitted_when_unmeasured_or_without_a_lag():
    assert "prior_heat" not in _room().to_emhass(ROW_ZERO, STEP, 8)
    assert "prior_heat" not in _room(thermal_inertia=0.0, prior_heat=(100.0,)).to_emhass(
        ROW_ZERO, STEP, 8
    )


# -- local reachability --------------------------------------------------------


def test_reachability_skips_the_unbounded_lag_window():
    # A floor jump at step 1 is reachable with no lag but EMHASS only holds
    # floors from L + 1 on, and by then planned heat arrives too.
    room = _room(thermal_inertia=0.0, heating_rate=1.0, current_temperature=19.0)
    assert room.first_unreachable_step(19.0, ROW_ZERO, STEP, 8) is not None
    lagged = _room(heating_rate=1.0, current_temperature=19.0, prior_heat=(2000.0, 2000.0))
    # Step 3 holds 1 planned + 2 in-flight steps: 19.0 + 0.75 >= 19.5.
    assert lagged.first_unreachable_step(19.0, ROW_ZERO, STEP, 8) is None


def test_measured_cold_lag_window_is_reported_and_clamped():
    # Nothing in flight: only one planned step reaches step 3.
    room = _room(heating_rate=1.0, current_temperature=19.0, prior_heat=(0.0, 0.0))
    found = room.first_unreachable_step(19.0, ROW_ZERO, STEP, 8)
    assert found is not None
    _, required, achievable = found
    assert (required, achievable) == (19.5, pytest.approx(19.25))
    clamped = ThermalConfig(**{**_fields(room), "clamp_floors": True}).to_emhass(ROW_ZERO, STEP, 8)
    assert clamped["min_temperatures"][3] == pytest.approx(19.25)


def test_unmeasured_flight_is_flat_out_for_warnings_but_zero_for_floors():
    # The warning keeps a physical upper bound; the floors EMHASS receives
    # follow what it solves with when no prior_heat is sent: nothing in flight.
    room = _room(heating_rate=1.0, current_temperature=19.0)
    assert room._achievable(19.0, STEP, 3, unmeasured_flat_out=True) == pytest.approx(19.75)
    assert room.first_unreachable_step(19.0, ROW_ZERO, STEP, 8) is None
    clamped = ThermalConfig(**{**_fields(room), "clamp_floors": True}).to_emhass(ROW_ZERO, STEP, 8)
    assert clamped["min_temperatures"][3] == pytest.approx(19.25)
    assert "prior_heat" not in clamped


def _fields(config: ThermalConfig) -> dict:
    return {name: getattr(config, name) for name in ThermalConfig.__slots__}


# -- coordinator ---------------------------------------------------------------


def _load(subentry_id: str, thermal, wants_to_run: bool = True) -> DeferrableLoad:
    return DeferrableLoad(
        subentry_id=subentry_id,
        name=subentry_id,
        nominal_power_w=2000.0,
        operating_hours=0.0,
        thermal=thermal,
        wants_to_run=wants_to_run,
    )


def _runtime(power_sensor: str | None, subentry_id: str = "room") -> DeferrableRuntime:
    runtime = DeferrableRuntime(subentry_id=subentry_id, name=subentry_id, nominal_power_w=2000.0)
    runtime.power_sensor = power_sensor
    return runtime


async def test_coordinator_reads_power_history_into_prior_heat(monkeypatch):
    now = ROW_ZERO - timedelta(minutes=1)
    calls = []

    def changes(hass, start, end, entity_id, *args):
        calls.append((start, end, entity_id))
        return {
            entity_id: [
                State(entity_id, "0", last_changed=ROW_ZERO - 2 * STEP),
                State(
                    entity_id,
                    "1.9",
                    {"unit_of_measurement": "kW"},
                    last_changed=ROW_ZERO - STEP + timedelta(seconds=50),
                ),
            ]
        }

    class Instance:
        async def async_add_executor_job(self, job):
            return job()

    monkeypatch.setattr(coordinator_module, "get_instance", lambda hass: Instance())
    monkeypatch.setattr(coordinator_module.history, "state_changes_during_period", changes)
    fake = SimpleNamespace(
        hass=None,
        config=SimpleNamespace(time_step_minutes=15),
        _row_zero_mode=ROW_ZERO_NEAREST,
        loads=SimpleNamespace(all=lambda: [_runtime("sensor.room_heat_power")]),
    )
    fake._recorded_step_power = lambda *a: EmhassCoordinator._recorded_step_power(fake, *a)

    [load] = await EmhassCoordinator._with_prior_heat(fake, [_load("room", _room())], now)

    assert calls == [(ROW_ZERO - 2 * STEP, now, "sensor.room_heat_power")]
    assert load.thermal.prior_heat == (0.0, pytest.approx(1900 * 850 / 900))


async def test_coordinator_leaves_loads_without_a_sensor_or_lag_alone(monkeypatch):
    def fail(*args):
        raise AssertionError("no history read expected")

    monkeypatch.setattr(coordinator_module, "get_instance", fail)
    rooms = [
        _load("room", _room()),
        _load("flat", _room(thermal_inertia=0.0)),
        _load("parked", _room(), wants_to_run=False),
    ]
    fake = SimpleNamespace(
        config=SimpleNamespace(time_step_minutes=15),
        _row_zero_mode=ROW_ZERO_NEAREST,
        loads=SimpleNamespace(
            all=lambda: [
                _runtime(None),
                _runtime("sensor.flat_power", "flat"),
                _runtime("sensor.parked_power", "parked"),
            ]
        ),
    )
    result = await EmhassCoordinator._with_prior_heat(fake, rooms, ROW_ZERO)
    assert [load.thermal.prior_heat for load in result] == [None, None, None]

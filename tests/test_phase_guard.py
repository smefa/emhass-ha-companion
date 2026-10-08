"""The phase guard's arithmetic, without Home Assistant.

The worked example throughout is the one from
docs/plan/phase_imbalance_safety.md: a 16 A three-phase service, the battery
charging ~8.6 kW evenly across the phases, and a 2.2 kW dishwasher heater on
L1 -- 24 A on a 16 A fuse.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.core import State
import pytest

from custom_components.emhass_companion.const import (
    MODE_FORCE_CHARGE,
    MODE_SELF_CONSUME,
    PHASE_GUARD_MIN_CHARGE_W,
)
from custom_components.emhass_companion.models import GridConfig
from custom_components.emhass_companion.phase_guard import (
    OVERLOAD_HEAVY,
    OVERLOAD_LIGHT,
    OVERLOAD_NONE,
    SlidingMin,
    charge_headroom_w,
    clamp_blocker,
    clamp_charge,
    overload_level,
    phase_import_limit_w,
    reading,
    to_amps,
    to_volts,
)

V = 230.0
T0 = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)


def _amps(*watts: float) -> tuple[float, ...]:
    return tuple(w / V for w in watts)


# -- units --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (2300, "W", 10.0),
        (2.3, "kW", 10.0),
        (10, "A", 10.0),
        (10000, "mA", 10.0),
        (-2300, "W", -10.0),
    ],
)
def test_readings_convert_to_amps(value: float, unit: str, expected: float) -> None:
    assert to_amps(value, unit, V) == pytest.approx(expected)


@pytest.mark.parametrize("unit", [None, "", "VA", "%"])
def test_an_unknown_unit_is_refused_not_guessed(unit: str | None) -> None:
    """Reading amps as watts would make every phase look 230x emptier."""
    assert to_amps(10, unit, V) is None


# -- the plan's import limit ----------------------------------------------------


def test_import_limit_matches_the_template_it_replaces() -> None:
    """docs/grid_limits.md: Σp + 3·fuse - 3·max(p), here 1000 + 10800 - 1200."""
    limit = phase_import_limit_w(_amps(400, 300, 300), 3600 / V, V)
    assert limit == pytest.approx(10600)


def test_balanced_phases_get_the_whole_connection() -> None:
    assert phase_import_limit_w(_amps(1000, 1000, 1000), 15, V) == pytest.approx(3 * 15 * V)


def test_an_exporting_phase_is_not_clamped_to_zero() -> None:
    """[+2000, -1000, 0] W on 3600 W/phase is 5800 W, not 6800 W."""
    assert phase_import_limit_w(_amps(2000, -1000, 0), 3600 / V, V) == pytest.approx(5800)


def test_a_phase_already_over_gives_zero_not_a_negative_limit() -> None:
    assert phase_import_limit_w(_amps(6000, 0, 0), 3600 / V, V) == 0.0


def test_single_phase_connection() -> None:
    assert phase_import_limit_w((10.0,), 15.0, V) == pytest.approx(15 * V)


# -- the executor's charge headroom -------------------------------------------


def test_dishwasher_on_l1_caps_the_battery_to_what_l1_allows() -> None:
    """The plan's scenario: battery 8610 W (2870/phase) + heater 2200 W on L1."""
    phases = _amps(400 + 2870 + 2200, 300 + 2870, 300 + 2870)
    headroom = charge_headroom_w(phases, 15.0, V, battery_w=8610)
    # L1 without the battery is 2600 W = 11.3 A; 3.7 A left, three phases' worth.
    assert headroom == pytest.approx(3 * (15 * V - 2600))
    # And charging at exactly that puts L1 at the limit, not over it.
    l1_after = (2600 + headroom / 3) / V
    assert l1_after == pytest.approx(15.0)


def test_headroom_reads_the_same_whether_the_battery_is_charging_or_not() -> None:
    house = (400, 300, 300)
    idle = charge_headroom_w(_amps(*house), 15.0, V, battery_w=0)
    charging = charge_headroom_w(_amps(*(w + 2000 for w in house)), 15.0, V, battery_w=6000)
    assert charging == pytest.approx(idle)


def test_headroom_is_negative_when_the_house_alone_is_over() -> None:
    assert charge_headroom_w(_amps(4000, 0, 0), 15.0, V, battery_w=0) < 0


def test_a_discharge_is_added_back_not_ignored() -> None:
    """The review case: 3400 W house on L1 hidden behind a 3000 W discharge."""
    meter = _amps(3400 - 1000, 300 - 1000, 300 - 1000)
    assert charge_headroom_w(meter, 15.0, V, battery_w=-3000) == pytest.approx(150)


@pytest.mark.parametrize(
    ("battery_enabled", "sensor", "interval", "expected"),
    [
        (True, "sensor.battery", 0, None),
        (True, "sensor.battery", 10, None),
        (True, None, 0, "no_battery_sensor"),
        (False, None, 0, None),  # no battery, nothing to clamp, nothing missing
        (True, "sensor.battery", 300, "slow_profile"),  # iSolarCloud
    ],
)
def test_clamp_blocker(
    battery_enabled: bool, sensor: str | None, interval: float, expected: str | None
) -> None:
    assert (
        clamp_blocker(
            battery_enabled=battery_enabled,
            battery_power_entity=sensor,
            min_write_interval_s=interval,
        )
        == expected
    )


# -- the Diazed curve -----------------------------------------------------------


@pytest.mark.parametrize(
    ("amps", "level"),
    [
        (16.0, OVERLOAD_NONE),  # runs indefinitely
        (19.0, OVERLOAD_LIGHT),
        (20.0, OVERLOAD_LIGHT),  # 1.25x: holds up to an hour
        (24.0, OVERLOAD_HEAVY),  # 1.5x: minutes
        (32.0, OVERLOAD_HEAVY),  # 2x: about a minute
    ],
)
def test_overload_level_follows_a_16a_gg_fuse(amps: float, level: str) -> None:
    assert overload_level(amps, 16.0) == level


# -- the clamp ------------------------------------------------------------------


def test_a_charge_inside_the_cap_is_untouched() -> None:
    assert clamp_charge(3000, 5000) == (MODE_FORCE_CHARGE, 3000, 0.0, [])


def test_no_cap_means_no_clamp() -> None:
    assert clamp_charge(3000, None) == (MODE_FORCE_CHARGE, 3000, 0.0, [])


def test_a_charge_over_the_cap_is_cut_to_it() -> None:
    action, power, cut, rules = clamp_charge(8610, 2550)
    assert (action, power, cut) == (MODE_FORCE_CHARGE, 2550, 8610 - 2550)
    assert "8610W→2550W" in rules[0]


def test_too_little_room_hands_the_battery_to_self_consumption() -> None:
    action, power, cut, rules = clamp_charge(5000, PHASE_GUARD_MIN_CHARGE_W - 1)
    assert (action, power, cut) == (MODE_SELF_CONSUME, 0.0, 5000)
    assert "self-consumption" in rules[0]


# -- the sliding minimum ----------------------------------------------------------


def test_sliding_min_drops_at_once_and_recovers_after_the_window() -> None:
    window = SlidingMin(timedelta(seconds=60))
    window.add(T0, 8000)
    window.add(T0 + timedelta(seconds=5), 2000)
    window.add(T0 + timedelta(seconds=10), 9000)

    assert window.value(T0 + timedelta(seconds=10)) == 2000
    # 2000 held from 5 s to 10 s, so it stays in the window until 70 s.
    assert window.value(T0 + timedelta(seconds=69)) == 2000
    assert window.value(T0 + timedelta(seconds=70)) == 9000


def test_sliding_min_holds_the_last_reading_of_a_quiet_meter() -> None:
    """No new sample is not no value: the last reading is still current."""
    window = SlidingMin(timedelta(seconds=60))
    window.add(T0, 1500)
    assert window.value(T0 + timedelta(hours=1)) == 1500


def test_sliding_min_counts_the_reading_current_when_the_window_opened() -> None:
    window = SlidingMin(timedelta(minutes=30))
    window.add(T0, 4000)
    window.add(T0 + timedelta(minutes=40), 9000)
    # At 50 min the window opens at 20 min, when 4000 still held.
    assert window.value(T0 + timedelta(minutes=50)) == 4000
    assert window.value(T0 + timedelta(minutes=70)) == 9000


def test_sliding_min_clear_forgets_everything() -> None:
    window = SlidingMin(timedelta(seconds=60))
    window.add(T0, 1)
    window.clear()
    assert window.value(T0) is None


# -- config -----------------------------------------------------------------------


def test_grid_config_reads_the_phase_guard_settings() -> None:
    grid = GridConfig.from_dict(
        {
            "phase_l1_entity": "sensor.l1",
            "phase_l2_entity": "sensor.l2",
            "phase_l3_entity": "sensor.l3",
            "main_fuse_a": 16,
            "phase_margin_a": 1.5,
        }
    )
    assert grid.phase_entities == ("sensor.l1", "sensor.l2", "sensor.l3")
    assert grid.phase_guard_enabled
    assert grid.phase_limit_a == 14.5


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"main_fuse_a": 16},
        {"phase_l1_entity": "sensor.l1", "phase_l2_entity": "sensor.l2", "main_fuse_a": 16},
        {"phase_l1_entity": "sensor.l1", "main_fuse_a": None},
    ],
)
def test_phase_guard_is_off_without_a_complete_configuration(data: dict) -> None:
    assert not GridConfig.from_dict(data).phase_guard_enabled


# -- voltage sensor ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "unit", "nominal", "expected"),
    [
        (236.4, "V", 230, 236.4),
        (0.2364, "kV", 230, 236.4),
        (236400, "mV", 230, 236.4),
        (118, "V", 120, 118),  # a 120 V supply, judged against its own nominal
        (400, "V", 230, None),  # line-to-line picked by mistake
        (0.236, "V", 230, None),  # kV reading with a V unit
        (236, None, 230, None),
        (236, "A", 230, None),
    ],
)
def test_a_voltage_reading_is_used_only_when_plausible(
    value: float, unit: str | None, nominal: float, expected: float | None
) -> None:
    result = to_volts(value, unit, nominal)
    assert result == (None if expected is None else pytest.approx(expected))


# -- reading ---------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "unavailable", "unknown", "12 A"])
def test_a_non_finite_or_non_numeric_state_is_no_reading(raw: str) -> None:
    """float() takes "nan" and "inf". Neither may reach a fuse guard: NaN
    compares false against every limit, so an overload would go unseen."""
    assert reading(State("sensor.phase_l1", raw)) is None


def test_a_numeric_state_is_a_reading() -> None:
    assert reading(State("sensor.phase_l1", "12.5")) == 12.5
    assert reading(None) is None

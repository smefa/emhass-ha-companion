"""Tests for snow cover detection (pure module).

Runs are simulated every 15 minutes from 08:00 UTC; raw forecast is 3 kW and
the day's peak 5 kW, so the 10 % sun floor (500 W) is always met.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.emhass_companion.snow_cover import (
    CLEAR_HOLD_WITHOUT_WEATHER,
    MANUAL_COVERED_FACTOR,
    MODE_AUTO,
    MODE_CLEAR,
    MODE_COVERED,
    SOURCE_MANUAL,
    SOURCE_MEASUREMENT,
    SOURCE_WEATHER,
    STATE_CLEAR,
    STATE_COVERED,
    STATE_SUSPECTED,
    SUSPECTED_FACTOR,
    SnowCoverState,
    advance,
    factor,
    from_json,
    gate_open,
    set_mode,
    to_json,
)

START = datetime(2026, 1, 10, 8, 0, tzinfo=UTC)
STEP = timedelta(minutes=15)
PLANNED = 3000.0
PEAK = 5000.0


def run(
    state: SnowCoverState,
    start: datetime,
    minutes: int,
    *,
    share: float | None = 0.09,
    temp: float | None = -2.0,
    condition: str | None = None,
    planned: float | None = PLANNED,
) -> tuple[SnowCoverState, datetime]:
    """Advance every 15 minutes; ``share`` is actual over raw forecast."""
    now = start
    for _ in range(minutes // 15):
        now += STEP
        actual = None if share is None or planned is None else planned * share
        state = advance(
            state,
            now=now,
            planned_w=planned,
            actual_w=actual,
            peak_w=PEAK,
            temperature_c=temp,
            condition=condition,
        )
    return state, now


def covered_state() -> tuple[SnowCoverState, datetime]:
    state, now = run(SnowCoverState(), START, 120)
    assert state.state == STATE_COVERED
    return state, now


# --- detection ------------------------------------------------------------------


def test_clear_sky_with_near_zero_output_in_the_cold_becomes_covered():
    state, _ = run(SnowCoverState(), START, 120)
    assert state.state == STATE_COVERED
    assert state.source == SOURCE_MEASUREMENT
    assert state.ratio == pytest.approx(0.09)
    assert factor(state) == pytest.approx(0.09)


def test_less_than_an_hour_of_sun_is_not_enough():
    state, _ = run(SnowCoverState(), START, 45)
    assert state.state == STATE_CLEAR
    assert factor(state) == 1.0


def test_healthy_output_never_triggers():
    state, _ = run(SnowCoverState(), START, 240, share=0.9)
    assert state.state == STATE_CLEAR


def test_midway_ratio_between_the_thresholds_neither_detects_nor_releases():
    state, _ = run(SnowCoverState(), START, 240, share=0.4)
    assert state.state == STATE_CLEAR


def test_an_hourly_step_detects_and_releases_too():
    """One sample per run stands for the whole step, so coverage still adds up."""
    hour = timedelta(hours=1)
    state, now = SnowCoverState(), START
    for share, expected in ((0.09, STATE_CLEAR), (0.09, STATE_COVERED), (0.9, STATE_COVERED)):
        now += hour
        state = advance(
            state,
            now=now,
            planned_w=PLANNED,
            actual_w=PLANNED * share,
            peak_w=PEAK,
            temperature_c=-2.0,
            condition=None,
            slot=hour,
        )
        assert state.state == expected
    for _ in range(2):
        now += hour
        state = advance(
            state,
            now=now,
            planned_w=PLANNED,
            actual_w=PLANNED * 0.9,
            peak_w=PEAK,
            temperature_c=-2.0,
            condition=None,
            slot=hour,
        )
    assert state.state == STATE_CLEAR


def test_summer_shortfall_is_never_snow():
    """A summer inverter fault: same shortfall, but the temperature gate is shut."""
    state, _ = run(SnowCoverState(), START, 240, temp=18.0)
    assert state.state == STATE_CLEAR
    assert not gate_open(state, START + timedelta(hours=4))
    assert factor(state) == 1.0


def test_no_temperature_at_all_keeps_the_feature_inactive():
    state, _ = run(SnowCoverState(), START, 240, temp=None)
    assert state.state == STATE_CLEAR


def test_gate_uses_the_recent_minimum_not_the_current_reading():
    """Frost overnight, +4 by morning: snow on the panels is still snow."""
    night = datetime(2026, 1, 10, 2, 0, tzinfo=UTC)
    state, _ = run(SnowCoverState(), night, 120, share=None, temp=-5.0, planned=None)
    state, _ = run(state, START, 180, temp=4.0)
    assert state.state == STATE_COVERED


def test_a_cold_spell_older_than_48_hours_no_longer_opens_the_gate():
    old = START - timedelta(hours=60)
    state, _ = run(SnowCoverState(), old, 60, share=None, temp=-5.0, planned=None)
    state, _ = run(state, START, 180, temp=6.0)
    assert state.state == STATE_CLEAR


def test_dim_intervals_below_the_sun_floor_are_ignored():
    """Dim intervals (below 10 % of the day's peak) are not real sun."""
    state, _ = run(SnowCoverState(), START, 240, planned=400.0)
    assert state.state == STATE_CLEAR


# --- snowfall requirement -------------------------------------------------------


def snowy_night(temp: float = -2.0) -> tuple[SnowCoverState, datetime]:
    """An hour of snowfall before dawn, no sun to measure."""
    return run(
        SnowCoverState(),
        START - timedelta(hours=6),
        60,
        share=None,
        temp=temp,
        condition="snowy",
        planned=None,
    )


def test_a_cold_forecast_miss_without_snowfall_is_not_snow():
    """Fog the forecast did not see: same shortfall, same cold, but no snow fell."""
    state, _ = run(SnowCoverState(), START, 240, condition="fog")
    assert state.state == STATE_CLEAR
    assert factor(state) == 1.0


def test_a_clear_day_after_a_snowy_night_is_detected():
    state, _ = snowy_night()
    state, _ = run(state, START, 120, condition="sunny")
    assert state.state == STATE_COVERED
    assert state.source == SOURCE_MEASUREMENT


def test_snowfall_in_mild_air_does_not_count():
    state, _ = snowy_night(temp=3.0)
    state, _ = run(state, START, 240, condition="sunny")
    assert state.state == STATE_CLEAR


def test_healthy_output_after_the_snowfall_forgets_it():
    """Snow fell, the panels produced normally, then fog: not snow."""
    state, _ = snowy_night()
    state, now = run(state, START, 90, share=0.9, condition="sunny")
    assert state.state == STATE_CLEAR
    assert state.snow_seen is None
    state, _ = run(state, now, 240, condition="fog")
    assert state.state == STATE_CLEAR


def test_sun_from_before_the_snowfall_does_not_forget_it():
    """Snow covers the panels mid-day: the release window still holds the sun before."""
    state, now = run(SnowCoverState(), START, 120, share=0.9, condition="sunny")
    state, now = run(state, now, 15, share=0.9, condition="snowy")
    assert state.snow_seen is not None
    state, _ = run(state, now, 180, condition="cloudy")
    assert state.state == STATE_COVERED


def test_a_weather_outage_does_not_fall_back_to_temperature_alone():
    state, now = run(SnowCoverState(), START, 15, condition="sunny")
    state, _ = run(state, now, 240, condition=None)
    assert state.state == STATE_CLEAR


def test_manual_clear_forgets_the_snowfall():
    state, now = snowy_night()
    state = set_mode(state, MODE_CLEAR, now)
    assert state.snow_seen is None
    state = set_mode(state, MODE_AUTO, now)
    state, _ = run(state, START, 240, condition="sunny")
    assert state.state == STATE_CLEAR


def test_the_remembered_snowfall_survives_storage():
    state, _ = snowy_night()
    again = from_json(to_json(state))
    assert again.snow_seen == state.snow_seen is not None
    assert again.weather_at == state.weather_at is not None


# --- scaling --------------------------------------------------------------------


def test_factor_is_the_measured_ratio_clamped_to_zero_one():
    assert factor(SnowCoverState()) == 1.0
    assert factor(SnowCoverState(state=STATE_COVERED, ratio=0.35)) == 0.35
    assert factor(SnowCoverState(state=STATE_COVERED, ratio=1.4)) == 1.0
    assert factor(SnowCoverState(state=STATE_COVERED, ratio=-0.2)) == 0.0
    assert factor(SnowCoverState(state=STATE_SUSPECTED, ratio=0.9)) == SUSPECTED_FACTOR


def test_ratio_is_measured_against_the_raw_forecast():
    """The samples hold raw planned watts, so the ratio keeps its meaning even
    though the plan itself is being scaled."""
    state, _ = covered_state()
    assert {s.planned_w for s in state.samples} == {PLANNED}


# --- priming from weather -------------------------------------------------------


def test_snowfall_in_the_cold_primes_suspected_at_night():
    state, _ = run(
        SnowCoverState(), START, 15, share=None, temp=-1.0, condition="snowy", planned=None
    )
    assert state.state == STATE_SUSPECTED
    assert state.source == SOURCE_WEATHER
    assert factor(state) == SUSPECTED_FACTOR


def test_snowy_rainy_primes_too():
    state, _ = run(
        SnowCoverState(), START, 15, share=None, temp=0.5, condition="snowy-rainy", planned=None
    )
    assert state.state == STATE_SUSPECTED


def test_snowfall_in_mild_air_does_not_prime():
    state, _ = run(
        SnowCoverState(), START, 15, share=None, temp=3.0, condition="snowy", planned=None
    )
    assert state.state == STATE_CLEAR


def test_no_weather_entity_means_no_priming():
    state, _ = run(SnowCoverState(), START, 15, share=None, temp=-3.0, condition=None, planned=None)
    assert state.state == STATE_CLEAR


def test_snowfall_while_output_is_healthy_does_not_prime():
    state, now = run(SnowCoverState(), START, 120, share=0.9)
    state, _ = run(state, now, 30, share=0.9, condition="snowy")
    assert state.state == STATE_CLEAR


def test_suspected_is_confirmed_by_daylight_measurement():
    state, now = run(
        SnowCoverState(),
        START - timedelta(hours=6),
        15,
        share=None,
        condition="snowy",
        planned=None,
    )
    assert state.state == STATE_SUSPECTED
    state, _ = run(state, now, 120)
    assert state.state == STATE_COVERED
    assert state.source == SOURCE_MEASUREMENT


def test_suspected_is_released_by_daylight_measurement():
    state, now = run(
        SnowCoverState(),
        START - timedelta(hours=6),
        15,
        share=None,
        condition="snowy",
        planned=None,
    )
    state, _ = run(state, now, 60, share=0.9, temp=-1.0)
    assert state.state == STATE_CLEAR
    assert factor(state) == 1.0


def test_morning_before_any_sun_the_suspicion_survives():
    state, now = run(
        SnowCoverState(),
        START - timedelta(hours=8),
        15,
        share=None,
        condition="snowy",
        planned=None,
    )
    state, _ = run(state, now, 240, share=None, temp=-4.0, planned=None)
    assert state.state == STATE_SUSPECTED


def test_suspected_lapses_once_the_cold_is_gone_for_good():
    state, now = run(
        SnowCoverState(), START, 15, share=None, temp=-1.0, condition="snowy", planned=None
    )
    state, _ = run(state, now, 49 * 60, share=None, temp=8.0, planned=None)
    assert state.state == STATE_CLEAR


# --- release --------------------------------------------------------------------


def test_recovery_releases_without_a_temperature_gate():
    state, now = covered_state()
    state, _ = run(state, now, 60, share=0.85, temp=12.0)
    assert state.state == STATE_CLEAR
    assert state.source is None
    assert factor(state) == 1.0


def test_a_short_spike_does_not_release():
    state, now = covered_state()
    state, _ = run(state, now, 15, share=0.9)
    assert state.state == STATE_COVERED


def test_release_is_not_immediately_undone_by_the_covered_hours_before_it():
    state, now = covered_state()
    state, now = run(state, now, 60, share=0.85)
    assert state.state == STATE_CLEAR
    state, _ = run(state, now, 90, share=0.85)
    assert state.state == STATE_CLEAR


# --- thaw and rain --------------------------------------------------------------


def test_a_few_warm_hours_downgrade_covered_to_suspected():
    state, now = covered_state()
    state, _ = run(state, now, 150, temp=4.0)
    assert state.state == STATE_COVERED  # not yet three hours warm
    state, _ = run(state, now + timedelta(minutes=150), 60, temp=4.0)
    assert state.state == STATE_SUSPECTED
    assert state.source == SOURCE_WEATHER


def test_rain_downgrades_covered_to_suspected():
    state, now = covered_state()
    state, _ = run(state, now, 15, condition="rainy")
    assert state.state == STATE_SUSPECTED


def test_downgraded_state_is_reconfirmed_only_by_fresh_daylight():
    state, _ = snowy_night()
    state, now = run(state, START, 120, condition="sunny")
    assert state.state == STATE_COVERED
    state, now = run(state, now, 15, condition="rainy")
    assert state.state == STATE_SUSPECTED
    # Samples from before the downgrade must not flip it straight back.
    state, now = run(state, now, 30, condition="rainy")
    assert state.state == STATE_SUSPECTED
    state, _ = run(state, now, 90)
    assert state.state == STATE_COVERED


def test_a_cold_reading_resets_the_thaw_clock():
    state, now = covered_state()
    state, now = run(state, now, 120, temp=4.0)
    state, now = run(state, now, 15, temp=-1.0)
    state, _ = run(state, now, 120, temp=4.0)
    assert state.state == STATE_COVERED


# --- manual override ------------------------------------------------------------


def test_manual_covered_holds_until_measurement_shows_recovery():
    state = set_mode(SnowCoverState(), MODE_COVERED, START)
    assert state.state == STATE_COVERED and state.source == SOURCE_MANUAL
    assert factor(state) == MANUAL_COVERED_FACTOR
    # Rain and warmth do not dissolve what the user said.
    state, now = run(state, START, 240, share=0.3, temp=8.0, condition="rainy")
    assert state.state == STATE_COVERED
    state, _ = run(state, now, 60, share=0.9, temp=8.0)
    assert state.state == STATE_CLEAR
    assert state.override == MODE_AUTO


def test_manual_covered_ignores_healthy_output_from_before_it():
    """Sun all morning, then snow and the user says so: the morning must not undo it."""
    state, now = run(SnowCoverState(), START, 120, share=0.9)
    state = set_mode(state, MODE_COVERED, now)
    assert factor(state) == MANUAL_COVERED_FACTOR
    state, now = run(state, now, 15, share=0.05)
    assert state.state == STATE_COVERED
    assert state.override == MODE_COVERED


def test_manual_covered_uses_the_measured_ratio_once_there_is_one():
    state = set_mode(SnowCoverState(), MODE_COVERED, START)
    state, _ = run(state, START, 120, share=0.3)
    assert factor(state) == pytest.approx(0.3)


def test_manual_clear_holds_against_a_shortfall_until_the_next_snowfall():
    state = set_mode(SnowCoverState(), MODE_CLEAR, START)
    state, now = run(state, START, 240, condition="cloudy")
    assert state.state == STATE_CLEAR
    assert state.override == MODE_CLEAR
    state, now = run(state, now, 15, condition="snowy")
    assert state.override == MODE_AUTO
    assert state.state == STATE_SUSPECTED


def test_manual_clear_empties_a_covered_state():
    state, now = covered_state()
    state = set_mode(state, MODE_CLEAR, now)
    assert state.state == STATE_CLEAR
    assert factor(state) == 1.0


def test_manual_clear_without_weather_lapses_after_a_few_days():
    state = set_mode(SnowCoverState(), MODE_CLEAR, START)
    state, _ = run(state, START, 60, share=None, planned=None)
    assert state.override == MODE_CLEAR
    state, _ = run(state, START + CLEAR_HOLD_WITHOUT_WEATHER, 15, share=None, planned=None)
    assert state.override == MODE_AUTO


def test_auto_removes_the_override_and_keeps_the_state():
    state = set_mode(SnowCoverState(), MODE_COVERED, START)
    state = set_mode(state, MODE_AUTO, START)
    assert state.override == MODE_AUTO
    assert state.state == STATE_COVERED


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        set_mode(SnowCoverState(), "melted", START)


# --- persistence ----------------------------------------------------------------


def test_state_round_trips_through_json():
    state, now = covered_state()
    state = set_mode(state, MODE_COVERED, now)
    again = from_json(to_json(state))
    assert again == state


def test_a_fresh_state_round_trips():
    assert from_json(to_json(SnowCoverState())) == SnowCoverState()


@pytest.mark.parametrize(
    "bad", [None, "x", {"state": "mud"}, {"samples": [["nope"]]}, {"ratio": "a"}]
)
def test_unreadable_storage_gives_a_clear_state(bad):
    assert from_json(bad) == SnowCoverState()


def test_restored_state_keeps_counting_where_it_left_off():
    state, now = covered_state()
    state = from_json(to_json(state))
    state, _ = run(state, now, 60, share=0.9)
    assert state.state == STATE_CLEAR


def test_naive_times_are_refused():
    with pytest.raises(ValueError):
        advance(
            SnowCoverState(),
            now=datetime(2026, 1, 1),
            planned_w=1.0,
            actual_w=1.0,
            peak_w=1.0,
            temperature_c=0.0,
            condition=None,
        )

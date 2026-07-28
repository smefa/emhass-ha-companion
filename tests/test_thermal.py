"""Tests for thermal deferrable loads.

The comfort band is a per-timestep list aligned to the horizon, so an
off-by-one or a timezone slip means a house is heated at the wrong hour rather
than raising anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from custom_components.emhass_companion.thermal import (
    SENSE_COOL,
    SENSE_HEAT,
    ThermalConfig,
    build_def_load_config,
)

HALF_HOUR = timedelta(minutes=30)
DAY_STEPS = 48


def _config(**overrides) -> ThermalConfig:
    defaults = {
        "comfort_temperature": 21.0,
        "setback_temperature": 18.0,
        "max_temperature": 24.0,
        "comfort_start": time(6, 0),
        "comfort_end": time(22, 0),
    }
    return ThermalConfig(**{**defaults, **overrides})


def _midnight_utc() -> datetime:
    """Local midnight, expressed in UTC (the fixture pins Europe/Stockholm)."""
    from homeassistant.util import dt as dt_util

    local = datetime(2026, 7, 28, 0, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return local.astimezone(UTC)


# --- band shape --------------------------------------------------------------


@pytest.mark.usefixtures("stockholm_timezone")
def test_band_has_exactly_one_value_per_timestep():
    """EMHASS errors on a short list; a long one is silently truncated."""
    minimums, maximums = _config().comfort_band(_midnight_utc(), HALF_HOUR, DAY_STEPS)
    assert len(minimums) == DAY_STEPS
    assert len(maximums) == DAY_STEPS


@pytest.mark.usefixtures("stockholm_timezone")
def test_comfort_window_maps_to_the_right_timesteps():
    """06:00-22:00 from local midnight is steps 12 through 43 at 30 minutes."""
    minimums, _ = _config().comfort_band(_midnight_utc(), HALF_HOUR, DAY_STEPS)

    assert minimums[0] == 18.0  # 00:00, setback
    assert minimums[11] == 18.0  # 05:30, still setback
    assert minimums[12] == 21.0  # 06:00, comfort begins
    assert minimums[43] == 21.0  # 21:30, last comfort step
    assert minimums[44] == 18.0  # 22:00, setback resumes


@pytest.mark.usefixtures("stockholm_timezone")
def test_the_maximum_is_flat_across_the_horizon():
    _, maximums = _config(max_temperature=23.5).comfort_band(_midnight_utc(), HALF_HOUR, DAY_STEPS)
    assert set(maximums) == {23.5}


@pytest.mark.usefixtures("stockholm_timezone")
def test_an_overnight_comfort_window_wraps_midnight():
    """A night-shift household, or simply comfort held through the small hours."""
    minimums, _ = _config(comfort_start=time(22, 0), comfort_end=time(6, 0)).comfort_band(
        _midnight_utc(), HALF_HOUR, DAY_STEPS
    )

    assert minimums[0] == 21.0  # 00:00 is inside 22:00 -> 06:00
    assert minimums[11] == 21.0  # 05:30
    assert minimums[12] == 18.0  # 06:00, window closes
    assert minimums[44] == 21.0  # 22:00, window opens again


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_zero_length_window_means_always_comfortable():
    """Reading it as "never" would quietly let a house go cold."""
    minimums, _ = _config(comfort_start=time(9, 0), comfort_end=time(9, 0)).comfort_band(
        _midnight_utc(), HALF_HOUR, DAY_STEPS
    )
    assert set(minimums) == {21.0}


@pytest.mark.usefixtures("stockholm_timezone")
def test_the_band_follows_the_launch_time_not_the_clock():
    """Timestep 0 is *now*, so the same window lands at different indices."""
    start = _midnight_utc() + timedelta(hours=6)  # 06:00 local
    minimums, _ = _config().comfort_band(start, HALF_HOUR, DAY_STEPS)

    assert minimums[0] == 21.0  # comfort has already begun
    assert minimums[32] == 18.0  # 22:00, setback


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_finer_timestep_yields_a_longer_band():
    minimums, _ = _config().comfort_band(_midnight_utc(), timedelta(minutes=15), 96)
    assert len(minimums) == 96
    assert minimums[23] == 18.0  # 05:45
    assert minimums[24] == 21.0  # 06:00


# --- the EMHASS payload fragment ---------------------------------------------


@pytest.mark.usefixtures("stockholm_timezone")
def test_thermal_config_carries_every_key_emhass_needs():
    config = _config(heating_rate=5.0, cooling_constant=0.15, thermal_inertia=1.0, sense=SENSE_HEAT)
    config.current_temperature = 19.4

    emhass = config.to_emhass(_midnight_utc(), HALF_HOUR, DAY_STEPS)

    assert emhass["heating_rate"] == 5.0
    assert emhass["cooling_constant"] == 0.15
    assert emhass["thermal_inertia"] == 1.0
    assert emhass["sense"] == SENSE_HEAT
    assert emhass["start_temperature"] == 19.4
    assert len(emhass["min_temperatures"]) == DAY_STEPS
    assert len(emhass["max_temperatures"]) == DAY_STEPS


@pytest.mark.usefixtures("stockholm_timezone")
def test_an_unavailable_room_sensor_omits_the_start_temperature():
    """Better to let EMHASS use its own default than to invent a temperature."""
    emhass = _config().to_emhass(_midnight_utc(), HALF_HOUR, DAY_STEPS)
    assert "start_temperature" not in emhass


@pytest.mark.usefixtures("stockholm_timezone")
def test_cooling_is_expressed_through_sense():
    emhass = _config(sense=SENSE_COOL).to_emhass(_midnight_utc(), HALF_HOUR, DAY_STEPS)
    assert emhass["sense"] == SENSE_COOL


# --- def_load_config ---------------------------------------------------------


@pytest.mark.usefixtures("stockholm_timezone")
def test_no_thermal_loads_sends_nothing():
    """Sending def_load_config at all would overwrite the load count."""
    assert build_def_load_config({}, 3, _midnight_utc(), HALF_HOUR, DAY_STEPS) is None


@pytest.mark.usefixtures("stockholm_timezone")
def test_ordinary_loads_get_an_empty_entry():
    """The list must cover every load, in order, or loads fall off the end.

    EMHASS overwrites number_of_deferrable_loads with this list's length, so a
    list holding only the thermal loads would silently drop the others from the
    optimisation entirely.
    """
    entries = build_def_load_config({1: _config()}, 3, _midnight_utc(), HALF_HOUR, DAY_STEPS)

    assert len(entries) == 3
    assert entries[0] == {}
    assert entries[2] == {}
    assert "thermal_config" in entries[1]


@pytest.mark.usefixtures("stockholm_timezone")
def test_several_thermal_loads_keep_their_positions():
    entries = build_def_load_config(
        {0: _config(comfort_temperature=21.0), 2: _config(comfort_temperature=19.0)},
        3,
        _midnight_utc(),
        HALF_HOUR,
        DAY_STEPS,
    )

    assert entries[0]["thermal_config"]["min_temperatures"][20] == 21.0
    assert entries[1] == {}
    assert entries[2]["thermal_config"]["min_temperatures"][20] == 19.0


# --- parsing -----------------------------------------------------------------


def test_from_dict_accepts_stored_strings():
    config = ThermalConfig.from_dict(
        {
            "sense": SENSE_COOL,
            "heating_rate": "4.5",
            "comfort_start": "07:15:00",
            "comfort_end": "23:30:00",
        }
    )
    assert config.sense == SENSE_COOL
    assert config.heating_rate == 4.5
    assert config.comfort_start == time(7, 15)
    assert config.comfort_end == time(23, 30)


def test_from_dict_falls_back_to_safe_defaults():
    config = ThermalConfig.from_dict({})
    assert config.sense == SENSE_HEAT
    assert config.comfort_temperature < config.max_temperature
    assert config.setback_temperature < config.comfort_temperature

"""The Dala Energi built-in profile renders to two independent demand charges."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.emhass_companion.network_calendar import HolidayCache, NetworkCalendar
from custom_components.emhass_companion.peaks import effective_rate_per_kw
from custom_components.emhass_companion.profiles import _load_profiles
from custom_components.emhass_companion.profiles.engine import resolve_network


def _dala_profile(tmp_path):
    result = _load_profiles(tmp_path / "profiles")
    return result.profiles["network/dala_energi"]


def _resolve_at(hass: HomeAssistant, tmp_path, when: datetime):
    """Render the profile as if wall-clock ``when`` were now (seasonal rates)."""
    profile = _dala_profile(tmp_path)
    options = {
        "overforing_ore": 9.0,
        "hoglast_vinter_kr": 105.0,
        "hoglast_sommar_kr": 35.0,
        "laglast_kr": 35.0,
        "workday_entity": "binary_sensor.workday_sensor",
    }
    with patch("homeassistant.util.dt.now", return_value=when):
        return resolve_network(hass, profile, options)


async def test_winter_weekday_day_is_hoglast(hass: HomeAssistant, tmp_path) -> None:
    winter = dt_util.as_utc(datetime(2026, 1, 14, 10, 0))
    calendar = NetworkCalendar.from_resolved(_resolve_at(hass, tmp_path, winter))
    holidays = HolidayCache()
    day = dt_util.as_utc(datetime(2026, 1, 14, 10, 0))  # Wed
    assert calendar.in_demand_window(day, holidays, index=0)
    assert not calendar.in_demand_window(day, holidays, index=1)
    assert calendar.demand_charges[0].rate_per_kw == 105.0
    assert (
        effective_rate_per_kw(
            rate_per_kw=105.0,
            rate_basis="month",
            aggregate="mean_top_n",
            n=3,
            days_in_period=31,
        )
        == 35.0
    )


async def test_saturday_and_night_are_laglast(hass: HomeAssistant, tmp_path) -> None:
    winter = dt_util.as_utc(datetime(2026, 1, 14, 10, 0))
    calendar = NetworkCalendar.from_resolved(_resolve_at(hass, tmp_path, winter))
    holidays = HolidayCache()
    saturday = dt_util.as_utc(datetime(2026, 1, 17, 10, 0))
    night = dt_util.as_utc(datetime(2026, 1, 14, 22, 0))
    for when in (saturday, night):
        assert not calendar.in_demand_window(when, holidays, index=0)
        assert calendar.in_demand_window(when, holidays, index=1)


async def test_summer_weekday_still_hoglast_window_with_summer_rate(
    hass: HomeAssistant, tmp_path
) -> None:
    summer = dt_util.as_utc(datetime(2026, 7, 15, 10, 0))  # Wed
    calendar = NetworkCalendar.from_resolved(_resolve_at(hass, tmp_path, summer))
    holidays = HolidayCache()
    day = dt_util.as_utc(datetime(2026, 7, 15, 10, 0))
    assert calendar.in_demand_window(day, holidays, index=0)
    assert not calendar.in_demand_window(day, holidays, index=1)
    assert calendar.demand_charges[0].rate_per_kw == 35.0
    assert calendar.demand_charges[1].rate_per_kw == 35.0
    assert len(calendar.bands) == 1
    assert calendar.bands[0].buy.adder == 0.09

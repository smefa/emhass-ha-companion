"""The Austria built-in demand profile renders to a 15-minute monthly peak."""

from __future__ import annotations

from datetime import datetime

from homeassistant.core import HomeAssistant

from custom_components.emhass_companion.network_calendar import NetworkCalendar
from custom_components.emhass_companion.profiles import _load_profiles
from custom_components.emhass_companion.profiles.engine import resolve_network


def _calendar(hass: HomeAssistant, tmp_path, **extra) -> NetworkCalendar:
    profile = _load_profiles(tmp_path / "profiles").profiles["network/austria_demand"]
    options = {
        "demand_rate": 5.0,
        "window_start": "06:00:00",
        "window_end": "22:00:00",
        **extra,
    }
    return NetworkCalendar.from_resolved(resolve_network(hass, profile, options))


async def test_monthly_rate_and_15_minute_max(hass: HomeAssistant, tmp_path) -> None:
    calendar = _calendar(hass, tmp_path)
    charge = calendar.demand_charge
    assert charge.rate_basis == "month"
    assert charge.rate_per_kw == 5.0
    assert charge.measure.interval.total_seconds() == 15 * 60
    assert charge.measure.aggregate == "max"
    assert not calendar.bands


async def test_window_all_year_unless_months_ticked(hass: HomeAssistant, tmp_path) -> None:
    window = _calendar(hass, tmp_path).demand_charge.window
    assert window.months is None
    assert window.matches(datetime(2026, 8, 16, 12), is_holiday=False)
    assert not window.matches(datetime(2026, 8, 16, 23), is_holiday=False)

    seasonal = _calendar(hass, tmp_path, window_months=["12", "1", "2"]).demand_charge.window
    assert seasonal.matches(datetime(2026, 1, 16, 12), is_holiday=False)
    assert not seasonal.matches(datetime(2026, 8, 16, 12), is_holiday=False)

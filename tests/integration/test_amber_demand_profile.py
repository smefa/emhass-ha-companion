"""The Amber Electric built-in profile actually renders to a usable calendar.

``test_profiles.py`` checks every built-in document is schema-valid; it does
not render any template. This is the one network profile whose ``options``
feed a *window*, not just a rate -- ``window_months`` is optional and, left
unticked, must render to "no restriction" rather than to an empty list that
would match no month at all. See ``network_calendar.Window.matches`` and the
profile's own ``{{ options.window_months or None }}``.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.core import HomeAssistant

from custom_components.emhass_companion.profiles import _load_profiles
from custom_components.emhass_companion.profiles.engine import resolve_network
from custom_components.emhass_companion.network_calendar import NetworkCalendar


def _amber_profile(tmp_path):
    result = _load_profiles(tmp_path / "profiles")
    return result.profiles["network/amber_demand"]


async def test_all_year_window_with_no_months_ticked(hass: HomeAssistant, tmp_path) -> None:
    profile = _amber_profile(tmp_path)
    resolved = resolve_network(
        hass,
        profile,
        {"demand_rate": 0.30, "window_start": "15:00:00", "window_end": "20:00:00"},
    )
    calendar = NetworkCalendar.from_resolved(resolved)
    window = calendar.demand_charge.window
    assert window.months is None
    assert window.matches(datetime(2026, 1, 16, 16), is_holiday=False)
    assert window.matches(datetime(2026, 8, 16, 16), is_holiday=False)
    assert not window.matches(datetime(2026, 8, 10, 10), is_holiday=False)


async def test_seasonal_window_with_months_ticked(hass: HomeAssistant, tmp_path) -> None:
    profile = _amber_profile(tmp_path)
    resolved = resolve_network(
        hass,
        profile,
        {
            "demand_rate": 0.30,
            "window_start": "15:00:00",
            "window_end": "20:00:00",
            "window_months": ["12", "1", "2"],
        },
    )
    calendar = NetworkCalendar.from_resolved(resolved)
    window = calendar.demand_charge.window
    assert window.matches(datetime(2026, 1, 16, 16), is_holiday=False)
    assert not window.matches(datetime(2026, 8, 16, 16), is_holiday=False)


async def test_effective_rate_basis_is_per_day(hass: HomeAssistant, tmp_path) -> None:
    profile = _amber_profile(tmp_path)
    resolved = resolve_network(
        hass,
        profile,
        {"demand_rate": 0.30, "window_start": "15:00:00", "window_end": "20:00:00"},
    )
    calendar = NetworkCalendar.from_resolved(resolved)
    assert calendar.demand_charge.rate_basis == "day"
    assert calendar.demand_charge.rate_per_kw == 0.30
    assert calendar.demand_charge.measure.interval.total_seconds() == 30 * 60
    assert calendar.demand_charge.measure.aggregate == "max"
    assert not calendar.bands

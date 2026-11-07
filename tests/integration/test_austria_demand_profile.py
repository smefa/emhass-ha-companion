"""The Austria built-in profile: a 15-minute monthly peak at any hour, plus SNAP/WiNAP."""

from __future__ import annotations

from datetime import datetime

import pytest
from homeassistant.core import HomeAssistant

from custom_components.emhass_companion.network_calendar import NetworkCalendar
from custom_components.emhass_companion.profiles import _load_profiles
from custom_components.emhass_companion.profiles.engine import resolve_network


def _calendar(hass: HomeAssistant, tmp_path, **extra) -> NetworkCalendar:
    profile = _load_profiles(tmp_path / "profiles").profiles["network/austria_demand"]
    options = {
        "demand_rate": 5.0,
        "energy_price_ct": 0.0,
        "snap_reduction_pct": 20.0,
        "winap_reduction_pct": 0.0,
        **extra,
    }
    return NetworkCalendar.from_resolved(resolve_network(hass, profile, options))


async def test_monthly_rate_and_15_minute_max(hass: HomeAssistant, tmp_path) -> None:
    charge = _calendar(hass, tmp_path).demand_charge
    assert charge.rate_basis == "month"
    assert charge.rate_per_kw == 5.0
    assert charge.measure.interval.total_seconds() == 15 * 60
    assert charge.measure.aggregate == "max"


async def test_demand_charge_has_no_window(hass: HomeAssistant, tmp_path) -> None:
    charge = _calendar(hass, tmp_path).demand_charge
    assert charge.window is None or charge.window.matches(
        datetime(2026, 8, 16, 3), is_holiday=False
    )


async def test_snap_and_winap_bands(hass: HomeAssistant, tmp_path) -> None:
    calendar = _calendar(hass, tmp_path, energy_price_ct=10.0, winap_reduction_pct=50.0)
    names = {b.name: b for b in calendar.bands}
    assert set(names) == {"SNAP", "WiNAP", "standard"}
    snap, winap = names["SNAP"], names["WiNAP"]
    assert snap.buy.adder == pytest.approx(0.08)
    assert winap.buy.adder == pytest.approx(0.05)
    assert names["standard"].buy.adder == pytest.approx(0.10)
    assert snap.window.matches(datetime(2026, 7, 1, 12), is_holiday=False)
    assert not snap.window.matches(datetime(2026, 7, 1, 17), is_holiday=False)
    assert not snap.window.matches(datetime(2026, 12, 1, 12), is_holiday=False)
    assert winap.window.matches(datetime(2026, 12, 1, 23, 30), is_holiday=False)
    assert winap.window.matches(datetime(2026, 12, 1, 4), is_holiday=False)
    assert not winap.window.matches(datetime(2026, 7, 1, 23, 30), is_holiday=False)

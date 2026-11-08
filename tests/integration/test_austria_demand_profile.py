"""The Austria built-in profile: a 15-minute monthly peak at any hour, plus SNAP/WiNAP."""

from __future__ import annotations

from datetime import datetime

from homeassistant.core import HomeAssistant
import pytest

from custom_components.emhass_companion.network_calendar import (
    NetworkCalendar,
    NetworkCalendarError,
)
from custom_components.emhass_companion.profiles import _load_profiles
from custom_components.emhass_companion.profiles.engine import resolve_network


def _calendar(hass: HomeAssistant, tmp_path, **extra) -> NetworkCalendar:
    profile = _load_profiles(tmp_path / "profiles").profiles["network/austria_demand"]
    options = {
        "demand_rate": 5.0,
        "demand_rate_above": 0.0,
        "tier_limit_kw": 10.0,
        "energy_price_ct": 0.0,
        "snap_reduction_pct": 20.0,
        "winap_reduction_pct": 0.0,
        **extra,
    }
    return NetworkCalendar.from_resolved(resolve_network(hass, profile, options))


async def test_two_tier_components(hass: HomeAssistant, tmp_path) -> None:
    calendar = _calendar(hass, tmp_path, demand_rate_above=8.0)
    tier1, tier2 = calendar.demand_charges
    assert tier1.rate_per_kw == 5.0
    assert tier1.min_peak_kw == 0.0
    assert tier2.rate_per_kw == pytest.approx(3.0)
    assert tier2.min_peak_kw == 10.0
    for charge in (tier1, tier2):
        assert charge.rate_basis == "month"
        assert charge.measure.interval.total_seconds() == 15 * 60
        assert charge.measure.aggregate == "max"


async def test_tier_limit_is_configurable(hass: HomeAssistant, tmp_path) -> None:
    calendar = _calendar(hass, tmp_path, demand_rate_above=8.0, tier_limit_kw=6.0)
    assert calendar.demand_charges[1].min_peak_kw == 6.0


async def test_unset_second_rate_charges_everything_at_rate_1(
    hass: HomeAssistant, tmp_path
) -> None:
    calendar = _calendar(hass, tmp_path, demand_rate_above=0.0)
    assert calendar.demand_charges[1].rate_per_kw == 0.0


async def test_second_rate_below_first_is_rejected(hass: HomeAssistant, tmp_path) -> None:
    with pytest.raises(NetworkCalendarError):
        _calendar(hass, tmp_path, demand_rate_above=3.0)


async def test_missing_tier_options_fall_back_to_defaults(hass: HomeAssistant, tmp_path) -> None:
    profile = _load_profiles(tmp_path / "profiles").profiles["network/austria_demand"]
    options = {
        "demand_rate": 5.0,
        "energy_price_ct": 0.0,
        "snap_reduction_pct": 20.0,
        "winap_reduction_pct": 0.0,
    }
    calendar = NetworkCalendar.from_resolved(resolve_network(hass, profile, options))
    assert calendar.demand_charges[1].min_peak_kw == 10.0


async def test_demand_charges_have_no_window(hass: HomeAssistant, tmp_path) -> None:
    for charge in _calendar(hass, tmp_path).demand_charges:
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

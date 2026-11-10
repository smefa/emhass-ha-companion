"""A fixed tariff goes through the same tariff and network bands as spot prices.

The fixed price profile fetches nothing and lets EMHASS price the run from a
peak / off-peak split. Before, that meant the tariff step's adjustments and the
network profile's energy fees silently never reached it. Now, whenever either
would change a price, the split is expanded onto the step grid and composed
like any spot series; otherwise EMHASS keeps pricing the run itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    CONF_PRICE,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    DOMAIN,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.network_calendar import (
    Band,
    NetworkCalendar,
    PriceAdjustment,
    Window,
)
from custom_components.emhass_companion.profiles import async_load_profiles
from custom_components.emhass_companion.tariff import native_tariff_series

STEP = timedelta(minutes=30)
# 2026-08-04 is CEST (UTC+2): 04:00 UTC is 06:00 local.
START = datetime(2026, 8, 4, 4, 0, tzinfo=UTC)
FIXED = {
    "peak_cost": 2.0,
    "offpeak_cost": 1.0,
    "peak_start": "07:00:00",
    "peak_end": "22:00:00",
    "sell_price": 0.3,
}


def _settings(**overrides) -> dict:
    settings = {
        "load_cost_forecast_method": "hp_hc_periods",
        "load_peak_hours_cost": "2.0",
        "load_offpeak_hours_cost": "1.0",
        "load_peak_hour_periods": {"period_hp_1": [{"start": "07:00:00"}, {"end": "22:00:00"}]},
        "production_price_forecast_method": "constant",
        "photovoltaic_production_sell_price": "0.3",
        "tariff_schedule_time_zone": "Europe/Stockholm",
    }
    settings.update(overrides)
    return settings


def _price_at(series, hour_utc: int, minute: int = 0) -> float:
    return series.value_at(START.replace(hour=hour_utc, minute=minute))


def test_peak_is_local_time_and_both_ends_inclusive() -> None:
    buy, sell = native_tariff_series(_settings(), START, START + timedelta(hours=20), STEP)
    assert _price_at(buy, 4, 30) == 1.0  # 06:30 local
    assert _price_at(buy, 5) == 2.0  # 07:00 local, start inclusive
    assert _price_at(buy, 20) == 2.0  # 22:00 local, end inclusive (pandas between_time)
    assert _price_at(buy, 20, 30) == 1.0  # 22:30 local
    assert set(sell.values) == {0.3}
    assert buy.covers(START + timedelta(hours=20))


def test_period_ending_before_it_starts_wraps_past_midnight() -> None:
    settings = _settings(
        load_peak_hour_periods={"p": [{"start": "22:00"}, {"end": "06:00"}]},
    )
    buy, _ = native_tariff_series(settings, START, START + timedelta(hours=20), STEP)
    assert _price_at(buy, 4) == 2.0  # 06:00 local
    assert _price_at(buy, 10) == 1.0  # 12:00 local
    assert _price_at(buy, 20) == 2.0  # 22:00 local


@pytest.mark.parametrize(
    "overrides",
    [
        {"load_cost_forecast_method": "list"},
        {"load_peak_hours_cost": "abc"},
        {"load_peak_hour_periods": {"p": [{"start": "nope"}, {"end": "06:00"}]}},
    ],
)
def test_unreadable_settings_leave_pricing_to_emhass(overrides) -> None:
    assert native_tariff_series(_settings(**overrides), START, START, STEP) is None


def test_non_constant_export_price_gives_no_sell_series() -> None:
    settings = _settings(production_price_forecast_method="csv")
    buy, sell = native_tariff_series(settings, START, START + STEP, STEP)
    assert buy
    assert not sell


async def _coordinator(hass: HomeAssistant, tariff: dict | None) -> EmhassCoordinator:
    await hass.config.async_set_time_zone("Europe/Stockholm")
    options = {CONF_PRICE: {CONF_PROFILE: "price/fixed", CONF_PROFILE_OPTIONS: FIXED}}
    if tariff is not None:
        options["tariff"] = tariff
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"}, options=options)
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)
    coordinator.profiles = (await async_load_profiles(hass)).profiles
    return coordinator


async def test_tariff_adjustments_and_network_fees_reach_a_fixed_tariff(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(
        hass,
        {
            "buy": {"mode": "linear", "multiplier": 1.25, "adder": 0.5},
            "sell": {"mode": "linear", "multiplier": 1.0, "adder": 0.1},
        },
    )
    prices = coordinator._native_prices(START, START + timedelta(hours=20), STEP)
    assert prices is not None
    buy, sell = prices
    assert _price_at(buy, 4) == pytest.approx(1.0 * 1.25 + 0.5)
    assert _price_at(buy, 10) == pytest.approx(2.0 * 1.25 + 0.5)
    assert _price_at(sell, 10) == pytest.approx(0.4)


async def test_network_profile_alone_still_expands_the_fixed_tariff(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(hass, None)
    coordinator.network_calendar = NetworkCalendar(
        bands=[
            Band(
                name="all",
                window=Window(months=None, days=None, hours=None),
                buy=PriceAdjustment(adder=0.25),
                sell=PriceAdjustment(),
            )
        ]
    )
    buy, sell = coordinator._native_prices(START, START + timedelta(hours=20), STEP)
    # Expanded but not yet banded: _build applies the bands to any buy series.
    assert _price_at(buy, 10) == 2.0
    banded, _ = coordinator.network_calendar.apply_bands(buy, sell, coordinator.holiday_cache)
    assert _price_at(banded, 10) == pytest.approx(2.25)


async def test_identity_tariff_without_a_network_profile_leaves_emhass_pricing(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(hass, None)
    assert coordinator._native_prices(START, START + timedelta(hours=20), STEP) is None

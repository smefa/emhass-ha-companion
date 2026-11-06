"""Only estimated prices within the next 24 hours warrant a health warning."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import DOMAIN
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.models import Point, Series
from custom_components.emhass_companion.price_forecast import PriceForecastError, fill_with_forecast

NOW = datetime(2026, 11, 2, 10, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _coordinator(hass: HomeAssistant) -> EmhassCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        options={"price_forecast": {"provider": "eupowerprices", "api_key": "test", "area": "SE2"}},
    )
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    return EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)


@pytest.mark.parametrize("flat", [False, True])
async def test_forecast_blend_keeps_source_end_and_reports_short_warning(hass, monkeypatch, flat):
    coordinator = _coordinator(hass)
    forecast = Series(Point(NOW + hour * HOUR, 50 + hour % 24) for hour in range(72))
    spot = Series(
        Point(NOW + hour * HOUR, 0.7 if flat else 0.01 * (50 + hour % 24)) for hour in range(23)
    )
    until = NOW + 48 * HOUR
    expected = fill_with_forecast(spot, forecast, until)
    monkeypatch.setattr(coordinator.price_forecast, "async_get", AsyncMock(return_value=forecast))
    series, source_end, warning = await coordinator._fill_spot_from_forecast(spot, NOW, until)
    assert list(series) == list(expected.series)
    assert source_end == spot.end
    if flat:
        assert warning.endswith(": previous-day prices.")
    else:
        assert f"{expected.forecast_weight:.0%} forecast" in warning
        assert f"{1 - expected.forecast_weight:.0%} previous day" in warning
    assert "correlation" not in warning
    assert "scale" not in warning and "offset" not in warning
    boundary = dt_util.as_local(expected.forecast_from)
    assert warning.startswith(f"Estimated prices from {boundary:%d %b %H:%M %Z}: ")
    assert len(warning) < 100
    assert list(series)[: len(spot)] == list(spot)


async def test_no_warning_for_forecast_beyond_tomorrows_published_prices(hass, monkeypatch):
    coordinator = _coordinator(hass)
    now = datetime(2026, 11, 3, 14, tzinfo=UTC)
    forecast = Series(Point(now + hour * HOUR, 50 + hour % 24) for hour in range(96))
    spot = Series(Point(point.time, point.value * 0.01) for point in list(forecast)[:33])
    monkeypatch.setattr(coordinator.price_forecast, "async_get", AsyncMock(return_value=forecast))
    await hass.config.async_set_time_zone("Europe/Stockholm")

    series, source_end, warning = await coordinator._fill_spot_from_forecast(
        spot, now, now + 72 * HOUR
    )

    assert source_end == spot.end
    assert warning is None
    assert series.end > spot.end
    assert list(series)[: len(spot)] == list(spot)
    assert series.value_at(now + 24 * HOUR) == spot.value_at(now + 24 * HOUR)


async def test_warning_uses_actual_first_forecast_point(hass, monkeypatch):
    coordinator = _coordinator(hass)
    forecast = Series(Point(NOW + hour * HOUR, 50 + hour % 24) for hour in range(72))
    spot = Series(
        Point(NOW + quarter * HOUR / 4, forecast.value_at(NOW + quarter * HOUR / 4) * 0.01)
        for quarter in range(95)
    )
    monkeypatch.setattr(coordinator.price_forecast, "async_get", AsyncMock(return_value=forecast))

    _, _, warning = await coordinator._fill_spot_from_forecast(spot, NOW, NOW + 48 * HOUR)

    boundary = dt_util.as_local(NOW + 24 * HOUR)
    assert warning.startswith(f"Estimated prices from {boundary:%d %b %H:%M %Z}: ")


async def test_invalid_forecast_preserves_the_real_source_and_explains_fallback(hass, monkeypatch):
    coordinator = _coordinator(hass)
    spot = Series(Point(NOW + hour * HOUR, 0.5) for hour in range(23))
    monkeypatch.setattr(
        coordinator.price_forecast,
        "async_get",
        AsyncMock(
            side_effect=PriceForecastError("forecast area does not match the configured area")
        ),
    )
    series, source_end, warning = await coordinator._fill_spot_from_forecast(
        spot, NOW, NOW + 48 * HOUR
    )
    assert series is spot
    assert source_end is None
    assert (
        warning == "Price forecast unavailable: forecast area does not match the configured area."
    )


@pytest.mark.parametrize("step_minutes", [15, 60])
@pytest.mark.parametrize("missing_last_interval", [False, True])
@pytest.mark.parametrize("failed", [False, True])
async def test_warning_threshold_counts_final_published_interval(
    hass, monkeypatch, step_minutes, missing_last_interval, failed
):
    coordinator = _coordinator(hass)
    step = timedelta(minutes=step_minutes)
    count = 24 * 60 // step_minutes - int(missing_last_interval)
    spot = Series(Point(NOW + index * step, 0.5 + index % 4 * 0.01) for index in range(count))
    forecast = Series(Point(NOW + hour * HOUR, 50 + hour % 24) for hour in range(72))
    getter = AsyncMock(
        side_effect=PriceForecastError("request failed") if failed else None,
        return_value=forecast,
    )
    monkeypatch.setattr(coordinator.price_forecast, "async_get", getter)

    series, source_end, warning = await coordinator._fill_spot_from_forecast(
        spot, NOW, NOW + 48 * HOUR
    )

    assert (warning is not None) == missing_last_interval
    getter.assert_awaited_once_with(NOW)
    if failed:
        assert series is spot
        assert source_end is None
    else:
        assert source_end == spot.end
        assert series.end > spot.end
        assert list(series)[: len(spot)] == list(spot)

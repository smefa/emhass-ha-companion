"""Filling price hours the real source lacks with the eupowerprices forecast."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.emhass_companion.models import Point, Series
from custom_components.emhass_companion.price_forecast import (
    PriceForecastConfig,
    PriceForecastError,
    fill_with_forecast,
    parse_forecast,
)

T0 = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def hourly(start: datetime, values: list[float]) -> Series:
    return Series(Point(start + HOUR * i, v) for i, v in enumerate(values))


def day_shape(hours: int) -> list[float]:
    # A daily curve with an evening peak, so correlation is meaningful.
    return [50 + 40 * ((h % 24) in (7, 8, 17, 18, 19)) - 20 * ((h % 24) < 5) for h in range(hours)]


def test_forecast_is_scaled_to_the_real_prices_and_extends_them():
    forecast = hourly(T0, day_shape(72))  # EUR/MWh
    # Real prices: SEK/kWh with 25% VAT, so ~ x0.0138 over EUR/MWh.
    spot = hourly(T0, [v * 0.0138 for v in day_shape(36)])
    fill = fill_with_forecast(spot, forecast, T0 + timedelta(hours=60))
    assert fill.scale == pytest.approx(0.0138)
    assert fill.overlap == 36
    assert fill.series.end >= T0 + timedelta(hours=60)
    assert fill.forecast_from == T0 + timedelta(hours=36)
    # Real points are untouched and the forecast continues the curve.
    assert fill.series.value_at(T0 + timedelta(hours=10)) == spot.value_at(T0 + timedelta(hours=10))
    assert fill.series.value_at(T0 + timedelta(hours=43)) == pytest.approx(
        day_shape(72)[43] * 0.0138
    )


def test_fixed_markup_is_fitted_as_an_offset_not_multiplied_up():
    shape = day_shape(72)
    shape[40] = -50.0  # a negative forecast hour past the real prices
    forecast = hourly(T0, shape)
    spot = hourly(T0, [v * 0.001 + 0.10 for v in shape[:30]])
    fill = fill_with_forecast(spot, forecast, T0 + timedelta(hours=45))
    assert fill.scale == pytest.approx(0.001)
    assert fill.offset == pytest.approx(0.10)
    assert fill.series.value_at(T0 + timedelta(hours=40)) == pytest.approx(0.05)


def test_missing_forecast_hours_before_the_horizon_are_refused():
    forecast = Series(p for p in hourly(T0, day_shape(72)) if p.time != T0 + timedelta(hours=32))
    spot = hourly(T0, [v * 0.01 for v in day_shape(30)])
    with pytest.raises(PriceForecastError, match="missing"):
        fill_with_forecast(spot, forecast, T0 + timedelta(hours=50))


def test_fill_stops_at_the_first_point_covering_the_horizon():
    forecast = hourly(T0, day_shape(24 * 14))
    spot = hourly(T0, [v * 0.01 for v in day_shape(30)])
    fill = fill_with_forecast(spot, forecast, T0 + timedelta(hours=50, minutes=30))
    assert fill.series.end == T0 + timedelta(hours=51)


def test_quarter_hour_spot_is_fitted_against_hold_last_hourly_forecast():
    forecast = hourly(T0, day_shape(72))
    spot = Series(
        Point(T0 + timedelta(minutes=15 * i), day_shape(72)[i // 4] * 0.5) for i in range(4 * 30)
    )
    fill = fill_with_forecast(spot, forecast, T0 + timedelta(hours=40))
    assert fill.scale == pytest.approx(0.5)
    assert fill.forecast_from == T0 + timedelta(hours=30)


def test_three_quarter_hour_hours_do_not_count_as_twelve_hours_of_overlap():
    forecast = hourly(T0, day_shape(72))
    spot = Series(
        Point(T0 + timedelta(minutes=15 * i), day_shape(72)[i // 4] * 0.5) for i in range(12)
    )
    with pytest.raises(PriceForecastError, match="overlap"):
        fill_with_forecast(spot, forecast, T0 + timedelta(hours=40))


def test_a_missing_first_forecast_hour_after_quarter_hour_prices_is_refused():
    shape = day_shape(72)
    forecast = Series(p for p in hourly(T0, shape) if p.time != T0 + timedelta(hours=30))
    spot = Series(Point(T0 + timedelta(minutes=15 * i), shape[i // 4] * 0.5) for i in range(4 * 30))
    with pytest.raises(PriceForecastError, match="missing"):
        fill_with_forecast(spot, forecast, T0 + timedelta(hours=40))


def test_too_little_overlap_is_refused():
    forecast = hourly(T0, day_shape(72))
    spot = hourly(T0, day_shape(6))
    with pytest.raises(PriceForecastError, match="overlap"):
        fill_with_forecast(spot, forecast, T0 + timedelta(hours=40))


def test_a_forecast_that_does_not_follow_the_prices_is_refused():
    forecast = hourly(T0, day_shape(72))
    spot = hourly(T0, [1.0 if h % 2 else 2.0 for h in range(30)])
    with pytest.raises(PriceForecastError, match=r"move with|correlation"):
        fill_with_forecast(spot, forecast, T0 + timedelta(hours=40))


def test_non_positive_prices_are_refused():
    forecast = hourly(T0, day_shape(72))
    spot = hourly(T0, [-v for v in day_shape(30)])
    with pytest.raises(PriceForecastError):
        fill_with_forecast(spot, forecast, T0 + timedelta(hours=40))


def test_forecast_ending_before_the_spot_does_not_extend_it():
    forecast = hourly(T0, day_shape(20))
    spot = hourly(T0, day_shape(30))
    with pytest.raises(PriceForecastError, match="reach"):
        fill_with_forecast(spot, forecast, T0 + timedelta(hours=40))


def test_empty_spot_is_refused():
    with pytest.raises(PriceForecastError):
        fill_with_forecast(Series.empty(), hourly(T0, day_shape(24)), T0 + HOUR)


def body(**overrides):
    data = {
        "unit": "EUR/MWh",
        "series": [
            {"ts_utc": "2026-10-13T22:00:00Z", "price_eur_mwh": 6.69},
            {"ts_utc": "2026-10-13T23:00:00Z", "price_eur_mwh": 5.77},
        ],
    }
    data.update(overrides)
    return data


def test_parse_reads_the_documented_shape():
    series = parse_forecast(body())
    assert len(series) == 2
    assert series.value_at(datetime(2026, 10, 13, 23, 30, tzinfo=UTC)) == 5.77


def test_parse_skips_null_prices():
    rows = [*body()["series"], {"ts_utc": "2026-10-14T00:00:00Z", "price_eur_mwh": None}]
    assert len(parse_forecast(body(series=rows))) == 2


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        body(unit="EUR/kWh"),
        body(series="nope"),
        body(series=[]),
        body(series=[{"ts_utc": "garbage", "price_eur_mwh": 1}]),
        body(series=[{"price_eur_mwh": 1}]),
        body(series=[{"ts_utc": "2026-10-13T22:00:00Z", "price_eur_mwh": "NaN"}]),
    ],
)
def test_parse_rejects_unexpected_bodies(bad):
    with pytest.raises(PriceForecastError):
        parse_forecast(bad)


def test_config_needs_provider_key_and_area_and_uppercases_the_area():
    full = {"provider": "eupowerprices", "api_key": " k ", "area": "se2"}
    assert PriceForecastConfig.from_dict(None) is None
    assert PriceForecastConfig.from_dict({**full, "api_key": ""}) is None
    assert PriceForecastConfig.from_dict({**full, "area": ""}) is None
    assert PriceForecastConfig.from_dict({**full, "provider": "none"}) is None
    assert PriceForecastConfig.from_dict({**full, "provider": "nope"}) is None
    assert PriceForecastConfig.from_dict(full) == PriceForecastConfig("eupowerprices", "k", "SE2")


class _Response:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self._payload


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        return self.responses.pop(0)


async def test_client_caches_and_sends_the_key_header():
    from custom_components.emhass_companion.price_forecast import PriceForecastClient

    session = _Session(_Response(200, body()))
    client = PriceForecastClient(session, PriceForecastConfig("eupowerprices", "secret", "SE2"))
    first = await client.async_get(T0)
    again = await client.async_get(T0 + timedelta(hours=2))
    assert again is first
    assert len(session.calls) == 1
    assert session.calls[0][0].endswith("/v1/forecasts/SE2/latest")
    assert session.calls[0][1] == {"X-API-Key": "secret"}


async def test_client_backs_off_after_a_failure_and_serves_stale_data():
    from custom_components.emhass_companion.price_forecast import PriceForecastClient

    session = _Session(_Response(200, body()), _Response(500, None))
    client = PriceForecastClient(session, PriceForecastConfig("eupowerprices", "k", "SE2"))
    first = await client.async_get(T0)
    later = T0 + timedelta(hours=4)  # cache expired
    assert await client.async_get(later) is first  # failed fetch -> stale copy
    assert await client.async_get(later + timedelta(minutes=5)) is first
    assert len(session.calls) == 2  # no retry inside the back-off


async def test_client_without_any_data_raises_on_a_refused_key():
    from custom_components.emhass_companion.price_forecast import PriceForecastClient

    client = PriceForecastClient(
        _Session(_Response(401, None)), PriceForecastConfig("eupowerprices", "k", "SE2")
    )
    with pytest.raises(PriceForecastError, match="refused"):
        await client.async_get(T0)

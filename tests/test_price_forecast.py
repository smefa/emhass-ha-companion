"""Filling price hours the real source lacks with the eupowerprices forecast."""

from datetime import UTC, datetime, timedelta
import math

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
    assert fill.forecast_weight == pytest.approx(1.0)
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


def test_a_forecast_that_does_not_follow_the_prices_uses_the_repeat():
    forecast = hourly(T0, day_shape(72))
    spot = hourly(T0, [1.0 if h % 2 else 2.0 for h in range(30)])
    until = T0 + timedelta(hours=40)
    fill = fill_with_forecast(spot, forecast, until)
    assert fill.forecast_weight == 0.0
    assert list(fill.series) == list(spot.extended_with_previous_day(until))


def test_negatively_correlated_prices_use_the_repeat_without_inverting_the_forecast():
    forecast = hourly(T0, day_shape(72))
    spot = hourly(T0, [-v for v in day_shape(30)])
    until = T0 + timedelta(hours=40)
    fill = fill_with_forecast(spot, forecast, until)
    assert fill.correlation == pytest.approx(-1.0)
    assert fill.scale == pytest.approx(1.0)
    assert fill.forecast_weight == 0.0
    assert list(fill.series) == list(spot.extended_with_previous_day(until))


@pytest.mark.parametrize("correlation", [-0.6, 0.0, 0.47, 0.49, 0.5, 0.51, 0.9, 1.0])
def test_confidence_blending_has_no_correlation_cutoff_or_double_shrinkage(correlation):
    forecast = hourly(
        T0,
        [50 + 20 * math.cos(math.tau * hour / 24) + 5 * (hour // 24) for hour in range(72)],
    )
    spot = hourly(
        T0,
        [
            0.7
            + 0.2
            * (
                correlation * math.cos(math.tau * hour / 24)
                + math.sqrt(1 - correlation**2) * math.sin(math.tau * hour / 24)
            )
            for hour in range(24)
        ],
    )
    fill = fill_with_forecast(spot, forecast, T0 + 60 * HOUR)
    weight = max(0, correlation) ** 2
    assert fill.correlation == pytest.approx(correlation)
    assert fill.forecast_weight == pytest.approx(weight, abs=1e-12)
    assert fill.scale == pytest.approx(0.01)
    assert fill.offset == pytest.approx(0.2)
    assert list(fill.series)[: len(spot)] == list(spot)
    repeated = spot.extended_with_previous_day(T0 + 60 * HOUR)
    for point in fill.series:
        if point.time <= spot.end:
            continue
        expected = weight * (forecast.value_at(point.time) * 0.01 + 0.2) + (
            1 - weight
        ) * repeated.value_at(point.time)
        assert point.value == pytest.approx(expected)


def test_blending_retains_quarter_hour_changes_from_the_previous_day():
    forecast = hourly(T0, day_shape(72))
    quarter = timedelta(minutes=15)
    spot = Series(
        Point(T0 + quarter * index, day_shape(24)[index // 4] * 0.01 + (index % 4) * 0.1)
        for index in range(96)
    )
    fill = fill_with_forecast(spot, forecast, T0 + 48 * HOUR)
    assert 0 < fill.forecast_weight < 1
    assert fill.forecast_from == T0 + 24 * HOUR
    for index in range(4):
        when = T0 + 24 * HOUR + quarter * index
        adjusted = forecast.value_at(when) * fill.scale + fill.offset
        expected = adjusted * fill.forecast_weight + spot.value_at(T0 + quarter * index) * (
            1 - fill.forecast_weight
        )
        assert fill.series.value_at(when) == pytest.approx(expected)
    assert fill.series.value_at(T0 + 24 * HOUR) != fill.series.value_at(T0 + 24 * HOUR + quarter)


@pytest.mark.parametrize("flat_spot,flat_forecast", [(True, False), (False, True), (True, True)])
def test_flat_overlap_uses_only_the_previous_day(flat_spot, flat_forecast):
    spot = hourly(T0, [0.7] * 24 if flat_spot else day_shape(24))
    forecast = hourly(T0, [50.0] * 72 if flat_forecast else day_shape(72))
    until = T0 + 48 * HOUR
    fill = fill_with_forecast(spot, forecast, until)
    assert fill.correlation == 0.0
    assert fill.forecast_weight == 0.0
    assert math.isfinite(fill.scale) and math.isfinite(fill.offset)
    assert list(fill.series) == list(spot.extended_with_previous_day(until))


def test_negative_prices_with_positive_correlation_are_not_rejected_or_clipped():
    forecast = hourly(T0, day_shape(72))
    spot = hourly(T0, [value * 0.01 - 2 for value in day_shape(24)])
    fill = fill_with_forecast(spot, forecast, T0 + 48 * HOUR)
    assert fill.forecast_weight == pytest.approx(1.0)
    assert fill.series.value_at(T0 + 30 * HOUR) == pytest.approx(-1.5)


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("source", ["spot", "forecast"])
def test_non_finite_input_prices_are_rejected(source, invalid):
    spot = hourly(T0, day_shape(24))
    forecast = hourly(T0, day_shape(72))
    bad_series = Series([*(spot if source == "spot" else forecast), Point(T0, invalid)])
    if source == "spot":
        spot = bad_series
    else:
        forecast = bad_series
    with pytest.raises(PriceForecastError, match="non-finite"):
        fill_with_forecast(spot, forecast, T0 + 48 * HOUR)


def test_a_short_forecast_still_extends_prices_without_claiming_full_coverage():
    spot = hourly(T0, day_shape(24))
    forecast = hourly(T0, day_shape(30))
    until = T0 + 48 * HOUR
    fill = fill_with_forecast(spot, forecast, until)
    assert fill.series.end == forecast.end
    assert fill.series.end > spot.end
    assert not fill.series.covers(until)


def test_missing_forecast_hours_in_the_overlap_are_rejected():
    spot = hourly(T0, day_shape(24))
    forecast = Series(point for point in hourly(T0, day_shape(72)) if point.time != T0 + 8 * HOUR)
    with pytest.raises(PriceForecastError, match="missing"):
        fill_with_forecast(spot, forecast, T0 + 48 * HOUR)


def test_empty_forecast_is_rejected():
    with pytest.raises(PriceForecastError, match="empty"):
        fill_with_forecast(hourly(T0, day_shape(24)), Series.empty(), T0 + 48 * HOUR)


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
        "area": "SE2",
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


@pytest.mark.parametrize("area", ["SE3", 123])
async def test_client_rejects_a_response_for_another_area(area):
    from custom_components.emhass_companion.price_forecast import PriceForecastClient

    client = PriceForecastClient(
        _Session(_Response(200, body(area=area))), PriceForecastConfig("eupowerprices", "k", "SE2")
    )
    with pytest.raises(PriceForecastError, match="area"):
        await client.async_get(T0)


@pytest.mark.parametrize("area", ["se2", " SE2 ", None])
async def test_client_accepts_normalized_or_optional_area_metadata(area):
    from custom_components.emhass_companion.price_forecast import PriceForecastClient

    data = body(area=area)
    if area is None:
        data.pop("area")
    client = PriceForecastClient(
        _Session(_Response(200, data)), PriceForecastConfig("eupowerprices", "k", "SE2")
    )
    assert len(await client.async_get(T0)) == 2

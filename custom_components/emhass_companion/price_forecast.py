"""Spot-price forecast used to fill the hours the real price source lacks.

A day-ahead source (Nord Pool and friends) stops at the end of the day it has
published, so for part of every day the horizon reaches past it. The payload
builder's last resort is to repeat yesterday; this module offers a better one
when the user has an eupowerprices.com key: a two-week hourly forecast for the
bidding zone.

Sources are providers in :data:`PROVIDERS`; adding one (outside the EU, say)
means a URL, an auth header and a parser that returns a Series. The fitting
below does not care what unit or currency a provider quotes.

The eupowerprices forecast is quoted in EUR/MWh, the user's price entity in whatever
currency and unit it likes, often with taxes folded in. Rather than configure
a conversion, the forecast is rescaled to the real prices over the hours where
both exist -- the one ratio absorbs currency, MWh against kWh, and VAT alike.
The fill is refused, and the caller falls back, whenever that fit looks wrong.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import itertools
import logging
import math
from typing import Any, Final

from aiohttp import ClientError, ClientSession, ClientTimeout

from .models import Point, Series

_LOGGER = logging.getLogger(__name__)

API_BASE: Final = "https://api.eupowerprices.com"
REQUEST_TIMEOUT: Final = ClientTimeout(total=30)

# The published forecast changes a few times a day at most; asking every few
# minutes would only spend the user's quota.
CACHE_TTL: Final = timedelta(hours=3)
# After a failed call, wait before trying again -- a run happens every few
# minutes and each would otherwise wait out the timeout.
RETRY_AFTER_FAILURE: Final = timedelta(minutes=15)
# How old a cached forecast may be and still be better than repeating
# yesterday, when the API cannot be reached.
STALE_LIMIT: Final = timedelta(hours=36)

# The scale fit needs enough overlapping hours to mean something, and enough
# agreement in shape to be the same market. Below the correlation floor the
# configured area is likelier wrong than the forecast merely noisy.
MIN_OVERLAP_POINTS: Final = 12
MIN_CORRELATION: Final = 0.5


class PriceForecastError(Exception):
    """The forecast could not be fetched, read or fitted. Never fatal to a run."""


def parse_eupowerprices(data: Any) -> Series:
    """Read the ``/v1/forecasts/<AREA>/latest`` body into a EUR/MWh Series."""
    if not isinstance(data, dict):
        raise PriceForecastError("unexpected response shape")
    unit = data.get("unit")
    if unit != "EUR/MWh":
        # Guard rather than convert: the scale fit would hide a unit change,
        # and a silent factor-1000 change is the kind that is not noticed.
        raise PriceForecastError(f"unexpected price unit {unit!r}")
    rows = data.get("series")
    if not isinstance(rows, list):
        raise PriceForecastError("response has no series")
    points: list[Point] = []
    for row in rows:
        try:
            when = datetime.fromisoformat(str(row["ts_utc"]).replace("Z", "+00:00"))
            value = row["price_eur_mwh"]
            if value is None:
                continue
            price = float(value)
            if not math.isfinite(price):
                raise ValueError(f"non-finite price {value!r}")
            points.append(Point(when.astimezone(UTC), price))
        except (KeyError, TypeError, ValueError) as err:
            raise PriceForecastError(f"unreadable forecast row: {err}") from err
    if not points:
        raise PriceForecastError("forecast is empty")
    return Series(points)


parse_forecast = parse_eupowerprices


@dataclass(frozen=True, slots=True)
class ForecastProvider:
    """One forecast service: where to ask, how to authenticate, how to read."""

    key: str
    name: str
    url: Callable[[str], str]
    """The request URL for an area code."""
    headers: Callable[[str], dict[str, str]]
    """The request headers for an API key."""
    parse: Callable[[Any], Series]


PROVIDER_EUPOWERPRICES: Final = "eupowerprices"
PROVIDER_NONE: Final = "none"

PROVIDERS: Final[dict[str, ForecastProvider]] = {
    PROVIDER_EUPOWERPRICES: ForecastProvider(
        key=PROVIDER_EUPOWERPRICES,
        name="eupowerprices.com",
        url=lambda area: f"{API_BASE}/v1/forecasts/{area}/latest",
        headers=lambda api_key: {"X-API-Key": api_key},
        parse=parse_eupowerprices,
    ),
}


@dataclass(frozen=True, slots=True)
class ForecastFill:
    """A spot series extended with forecast points, and how it was fitted."""

    series: Series
    scale: float
    offset: float
    correlation: float
    overlap: int
    forecast_from: datetime


def fill_with_forecast(spot: Series, forecast: Series, until: datetime) -> ForecastFill:
    """Extend ``spot`` to ``until`` with ``forecast`` rescaled onto it.

    Raises :class:`PriceForecastError` when the forecast cannot be trusted to
    line up with ``spot`` -- the caller then keeps the series it had.
    """
    if not spot:
        raise PriceForecastError("no real prices to fit the forecast against")
    pairs: list[tuple[float, float]] = []
    overlap_times: set[datetime] = set()
    for point in spot:
        if not forecast.start <= point.time <= forecast.end:
            continue
        forecast_value = forecast.value_at(point.time)
        if forecast_value is not None:
            pairs.append((point.value, forecast_value))
            overlap_times.add(point.time)
    # Counted in distinct forecast hours: a quarter-hourly spot series holds the
    # same hourly forecast value across four points, which is one observation.
    overlap_hours = len(
        {int(point.time.timestamp() // 3600) for point in spot if point.time in overlap_times}
    )
    if overlap_hours < MIN_OVERLAP_POINTS:
        raise PriceForecastError(
            f"only {overlap_hours} hours overlap the real prices (need {MIN_OVERLAP_POINTS})"
        )
    mean_real = sum(real for real, _ in pairs) / len(pairs)
    mean_fore = sum(fore for _, fore in pairs) / len(pairs)
    var_fore = sum((fore - mean_fore) ** 2 for _, fore in pairs)
    cov = sum((real - mean_real) * (fore - mean_fore) for real, fore in pairs)
    if var_fore <= 0 or cov <= 0:
        raise PriceForecastError("forecast does not move with the real prices")
    # real ~ scale * forecast + offset. The offset carries fixed per-kWh
    # additions a price entity may already contain (taxes, markups); a pure
    # ratio would multiply those up and flip the sign of a future negative
    # price.
    scale = cov / var_fore
    offset = mean_real - scale * mean_fore
    correlation = _correlation(pairs)
    if not (math.isfinite(scale) and math.isfinite(offset) and math.isfinite(correlation)):
        raise PriceForecastError("forecast could not be fitted")
    if correlation < MIN_CORRELATION:
        raise PriceForecastError(
            f"forecast does not follow the real prices (correlation {correlation:.2f})"
        )

    until = until.astimezone(UTC)
    extra: list[Point] = []
    for point in forecast:
        if point.time <= spot.end:
            continue
        extra.append(Point(point.time, point.value * scale + offset))
        if point.time >= until:
            break
    if not extra:
        raise PriceForecastError("forecast does not reach past the real prices")
    # A null price is dropped when parsing; left in the gap, the series would
    # hold the previous price across it and still look like it covers the
    # horizon, which switches off the previous-day fallback.
    step = forecast.step() or timedelta(hours=1)
    # The hand-over gap is judged by the real source's own cadence: a missing
    # first forecast hour after quarter-hour prices is only 75 minutes.
    join_limit = (spot.step() or step) * 2
    if extra[0].time - spot.end > join_limit or any(
        later - earlier > step * 1.5
        for earlier, later in itertools.pairwise(point.time for point in extra)
    ):
        raise PriceForecastError("forecast has missing hours before the horizon")
    return ForecastFill(
        series=Series([*spot, *extra]),
        scale=scale,
        offset=offset,
        correlation=correlation,
        overlap=len(pairs),
        forecast_from=extra[0].time,
    )


def _correlation(pairs: list[tuple[float, float]]) -> float:
    n = len(pairs)
    mean_x = sum(x for x, _ in pairs) / n
    mean_y = sum(y for _, y in pairs) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    var_x = sum((x - mean_x) ** 2 for x, _ in pairs)
    var_y = sum((y - mean_y) ** 2 for _, y in pairs)
    if var_x <= 0 or var_y <= 0:
        return 0.0
    return cov / math.sqrt(var_x * var_y)


@dataclass(frozen=True, slots=True)
class PriceForecastConfig:
    """The chosen provider and the user's key and bidding zone with it."""

    provider: str
    api_key: str
    area: str

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PriceForecastConfig | None:
        """None unless both fields are set -- blank means the feature is off."""
        data = data or {}
        provider = str(data.get("provider") or PROVIDER_NONE)
        api_key = str(data.get("api_key") or "").strip()
        area = str(data.get("area") or "").strip().upper()
        if provider not in PROVIDERS or not api_key or not area:
            return None
        return cls(provider, api_key, area)


class PriceForecastClient:
    """Fetches and caches the forecast for one configured area."""

    def __init__(self, session: ClientSession, config: PriceForecastConfig) -> None:
        self._session = session
        self._config = config
        self._provider = PROVIDERS[config.provider]
        self._series: Series | None = None
        self._fetched: datetime | None = None
        self._failed: datetime | None = None

    @property
    def config(self) -> PriceForecastConfig:
        return self._config

    async def async_get(self, now: datetime) -> Series:
        """The forecast, from cache while fresh. Raises PriceForecastError."""
        if self._series is not None and self._fetched and now - self._fetched < CACHE_TTL:
            return self._series
        if self._failed and now - self._failed < RETRY_AFTER_FAILURE:
            return self._stale_or_raise(now, "recent request failed")
        try:
            series = self._provider.parse(await self._fetch())
        except PriceForecastError as err:
            self._failed = now
            _LOGGER.warning("Price forecast unavailable: %s", err)
            return self._stale_or_raise(now, str(err))
        self._series, self._fetched, self._failed = series, now, None
        return series

    def _stale_or_raise(self, now: datetime, reason: str) -> Series:
        if self._series is not None and self._fetched and now - self._fetched < STALE_LIMIT:
            return self._series
        raise PriceForecastError(reason)

    async def _fetch(self) -> Any:
        try:
            async with self._session.get(
                self._provider.url(self._config.area),
                headers=self._provider.headers(self._config.api_key),
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status in (401, 403):
                    raise PriceForecastError("the API key was refused")
                if response.status == 404:
                    raise PriceForecastError(f"area {self._config.area} is not available")
                if response.status != 200:
                    raise PriceForecastError(f"HTTP {response.status}")
                return await response.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as err:
            raise PriceForecastError(f"request failed: {err}") from err

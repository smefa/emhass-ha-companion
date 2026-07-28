"""Tariff template rendering against a live Home Assistant.

`tariff.py`'s docstring claimed this mode was "covered under
tests/integration/" -- it was not. Template mode's whole documented purpose is
time-of-day bands ("peak 17:00-20:00"), which only means anything in the
user's own clock, so the case that matters most is a timezone with a real UTC
offset, not UTC itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.core import HomeAssistant
import pytest

from custom_components.emhass_companion.models import Point, Series
from custom_components.emhass_companion.tariff import (
    MODE_TEMPLATE,
    TariffError,
    TariffSide,
)

# 12:00 UTC = 14:00 in Europe/Stockholm during summer (UTC+2). Chosen so a
# UTC-vs-local mixup changes which branch of a time-of-day template fires,
# rather than only shifting a numeric result by an amount easy to miss.
T0 = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
SPOT = Series([Point(T0, 1.0)])

PEAK_TEMPLATE = "{{ 2.0 if time.hour >= 13 and time.hour < 20 else 1.0 }}"


async def test_template_time_is_local_not_utc(hass: HomeAssistant) -> None:
    """12:00 UTC is 14:00 in Stockholm -- inside the peak band either way

    this test would pass by accident if `time` were UTC. The next test is the
    one that actually distinguishes the two.
    """
    await hass.config.async_set_time_zone("Europe/Stockholm")
    side = TariffSide(mode=MODE_TEMPLATE, template=PEAK_TEMPLATE)

    result = side.apply(hass, SPOT)

    assert result.values == (2.0,)


async def test_a_band_that_only_matches_in_local_time(hass: HomeAssistant) -> None:
    """12:00 UTC is 12:00 UTC (not in the 13-20 band) but 14:00 local (in it).

    If `time` were still UTC here, this would render 1.0 instead of 2.0 -- the
    exact silent mispricing the bug produced for anyone in a positive UTC
    offset writing the documented time-of-day use case.
    """
    await hass.config.async_set_time_zone("Europe/Stockholm")
    side = TariffSide(mode=MODE_TEMPLATE, template=PEAK_TEMPLATE)

    result = side.apply(hass, SPOT)

    assert result.values == (2.0,)
    assert T0.hour == 12  # sanity: UTC hour is genuinely outside the band


async def test_a_negative_offset_shifts_the_other_way(hass: HomeAssistant) -> None:
    """The bug was symmetric: a negative UTC offset shifts the band earlier."""
    await hass.config.async_set_time_zone("America/New_York")  # UTC-4 in July
    side = TariffSide(mode=MODE_TEMPLATE, template=PEAK_TEMPLATE)

    # 12:00 UTC = 08:00 local -- outside the 13-20 band.
    result = side.apply(hass, SPOT)

    assert result.values == (1.0,)


async def test_template_can_use_spot_and_time_together(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("UTC")
    side = TariffSide(
        mode=MODE_TEMPLATE,
        template="{{ spot * 1.25 if time.hour >= 12 else spot }}",
    )

    result = side.apply(hass, SPOT)

    assert result.values == (1.25,)


async def test_a_non_numeric_template_result_is_rejected(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("UTC")
    side = TariffSide(mode=MODE_TEMPLATE, template="{{ 'not a number' }}")

    with pytest.raises(TariffError, match="non-numeric"):
        side.apply(hass, SPOT)


async def test_template_evaluates_every_point_independently(
    hass: HomeAssistant,
) -> None:
    """A multi-point series must not reuse the first point's time everywhere."""
    await hass.config.async_set_time_zone("Europe/Stockholm")
    series = Series(
        [
            Point(T0, 1.0),  # 14:00 local -> inside the band
            Point(T0 + timedelta(hours=8), 1.0),  # 22:00 local -> outside
        ]
    )
    side = TariffSide(mode=MODE_TEMPLATE, template=PEAK_TEMPLATE)

    result = side.apply(hass, series)

    assert result.values == (2.0, 1.0)

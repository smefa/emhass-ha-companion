"""Network-tariff band resolution across a DST transition.

``network_calendar.py`` matches bands against local wall-clock time via
``dt_util.as_local``, exactly like ``tariff.py``'s template mode -- see
tests/integration/test_tariff_template.py for the UTC-vs-local bug this
guards against. The case unique to a network profile is the calendar: Sweden's
late-October DST transition lands squarely inside Göteborg Energi's Nov-Mar
high-load season, so the same wall-clock band boundary (07:00) has to survive
the UTC offset changing by an hour underneath it.

Local instants are built via ``dt_util.as_utc`` on a naive datetime, after
pinning the timezone with ``hass.config.async_set_time_zone`` -- letting
``dt_util`` compute the UTC offset rather than hand-working it, which is
exactly the kind of arithmetic a human gets backwards under DST.
"""

from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.emhass_companion.models import Point, Series
from custom_components.emhass_companion.network_calendar import HolidayCache, NetworkCalendar

RESOLVED = {
    "calendar": {
        "business_day": {
            "weekdays": ["mon", "tue", "wed", "thu", "fri"],
            "exclude_holidays": False,
        },
    },
    "energy_bands": [
        {
            "name": "höglast",
            "months": [11, 12, 1, 2, 3],
            "days": "business_day",
            "hours": "07:00-20:00",
            "buy": {"adder": 0.34},
        },
        {"name": "låglast", "buy": {"adder": 0.065}},
    ],
}


def _local(*args: int) -> datetime:
    """A local wall-clock instant, converted to UTC by dt_util itself."""
    return dt_util.as_utc(datetime(*args))


async def test_band_boundary_survives_spring_forward(hass: HomeAssistant) -> None:
    """Sweden moves clocks forward on the last Sunday in March (2026-03-29).

    07:00 local before the transition and 07:00 local after it are different
    UTC instants; matching against the raw UTC hour would put the boundary in
    the wrong place on transition day specifically.
    """
    await hass.config.async_set_time_zone("Europe/Stockholm")
    calendar = NetworkCalendar.from_resolved(RESOLVED)
    holidays = HolidayCache()

    # 2026-03-30, the Monday after the transition, still inside the höglast
    # season (March). 06:59 and 07:00 local either side of the band boundary.
    band_before = calendar.current_band(_local(2026, 3, 30, 6, 59), holidays)
    band_at = calendar.current_band(_local(2026, 3, 30, 7, 0), holidays)

    assert band_before is not None and band_before.name == "låglast"
    assert band_at is not None and band_at.name == "höglast"


async def test_band_boundary_survives_fall_back(hass: HomeAssistant) -> None:
    """Sweden moves clocks back on the last Sunday in October (2026-10-25).

    Still inside the Nov-Mar season only from November, so this checks the
    band-matching arithmetic survives the offset change, not the season
    itself -- both instants below fall in October, both should be "låglast".
    """
    await hass.config.async_set_time_zone("Europe/Stockholm")
    calendar = NetworkCalendar.from_resolved(RESOLVED)
    holidays = HolidayCache()

    # 2026-10-26, the Monday after the fall-back transition.
    band = calendar.current_band(_local(2026, 10, 26, 6, 30), holidays)

    assert band is not None
    assert band.name == "låglast"  # October is outside the Nov-Mar season


async def test_apply_bands_matches_every_point_across_the_transition(
    hass: HomeAssistant,
) -> None:
    """A series spanning the spring-forward transition must band each point
    against its own local time, not a single offset computed once."""
    await hass.config.async_set_time_zone("Europe/Stockholm")
    calendar = NetworkCalendar.from_resolved(RESOLVED)
    holidays = HolidayCache()

    before = _local(2026, 3, 30, 6, 59)  # låglast
    after = _local(2026, 3, 30, 7, 0)  # höglast
    buy = Series([Point(before, 1.0), Point(after, 1.0)])

    buy2, _sell2 = calendar.apply_bands(buy, Series.empty(), holidays)

    assert buy2.values == (1.065, 1.34)


async def test_a_negative_offset_shifts_the_boundary_the_other_way(
    hass: HomeAssistant,
) -> None:
    """Same shape as tariff.py's own negative-offset regression test."""
    await hass.config.async_set_time_zone("America/New_York")
    calendar = NetworkCalendar.from_resolved(RESOLVED)
    holidays = HolidayCache()

    # 07:00 UTC on a January weekday is 02:00 local in New York -- well
    # outside the höglast window even though the UTC hour is inside it.
    when = datetime(2026, 1, 14, 7, 0, tzinfo=UTC)
    band = calendar.current_band(when, holidays)

    assert band is not None
    assert band.name == "låglast"

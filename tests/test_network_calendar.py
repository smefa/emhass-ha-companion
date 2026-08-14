"""Tests for network-tariff band resolution.

DST-dependent behaviour (a wall-clock hour boundary that only matters because
it is local, not UTC) is covered in tests/integration/test_network_calendar_dst.py,
which needs a running Home Assistant to pin the timezone through
``hass.config.async_set_time_zone``. Everything that can be tested against a
fixed ``dt_util.DEFAULT_TIME_ZONE`` lives here, following tests/test_tariff.py's
style: pure functions, no ``hass``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.emhass_companion.models import Point, Series
from custom_components.emhass_companion.network_calendar import (
    DateSet,
    HolidayCache,
    HourRange,
    NetworkCalendar,
    NetworkCalendarError,
    PriceAdjustment,
    Window,
)

GOTEBORG_RESOLVED = {
    "calendar": {
        "business_day": {
            "weekdays": ["mon", "tue", "wed", "thu", "fri"],
            "exclude_holidays": True,
            "holiday_entity": "binary_sensor.workday_sensor",
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
    "demand_charge": {
        "rate_per_kw": 135.0,
        "rate_basis": "month",
        "window": {
            "months": [11, 12, 1, 2, 3],
            "days": "business_day",
            "hours": "07:00-20:00",
        },
        "measure": {"interval": "60min", "aggregate": "mean_top_n", "n": 3, "distinct_days": True},
        "period": "month",
    },
}


@pytest.fixture(autouse=True)
def _stockholm(stockholm_timezone):
    """Every test in this file cares about local wall-clock time."""


def _local(year, month, day, hour, minute=0):
    from homeassistant.util import dt as dt_util

    return dt_util.as_utc(datetime(year, month, day, hour, minute))


# --- PriceAdjustment -----------------------------------------------------------


def test_price_adjustment_applies_multiplier_then_adder():
    adjustment = PriceAdjustment(multiplier=1.1, adder=0.34)
    assert adjustment.apply(1.0) == pytest.approx(1.44)


def test_price_adjustment_default_is_a_no_op():
    assert PriceAdjustment().apply(0.65) == pytest.approx(0.65)


# --- HourRange -------------------------------------------------------------------


def test_hour_range_plain():
    hours = HourRange.parse("07:00-20:00")
    assert hours.contains(datetime(2026, 1, 1, 7, 0).time())
    assert hours.contains(datetime(2026, 1, 1, 19, 59).time())
    assert not hours.contains(datetime(2026, 1, 1, 20, 0).time())
    assert not hours.contains(datetime(2026, 1, 1, 6, 59).time())


def test_hour_range_wraps_midnight():
    hours = HourRange.parse("22:00-06:00")
    assert hours.contains(datetime(2026, 1, 1, 23, 0).time())
    assert hours.contains(datetime(2026, 1, 1, 0, 0).time())
    assert hours.contains(datetime(2026, 1, 1, 5, 59).time())
    assert not hours.contains(datetime(2026, 1, 1, 6, 0).time())
    assert not hours.contains(datetime(2026, 1, 1, 21, 59).time())


def test_hour_range_invalid_text_raises():
    with pytest.raises(NetworkCalendarError, match="Invalid hours range"):
        HourRange.parse("not-a-range")


# --- DateSet ---------------------------------------------------------------------


def test_date_set_defaults_to_every_day():
    date_set = DateSet.from_dict({})
    assert len(date_set.weekdays) == 7


def test_date_set_excludes_holiday_only_when_configured():
    date_set = DateSet.from_dict({"weekdays": ["mon"], "exclude_holidays": True})
    monday = datetime(2026, 1, 5)  # a Monday
    assert date_set.matches(monday, is_holiday=False)
    assert not date_set.matches(monday, is_holiday=True)


def test_date_set_unknown_weekday_raises():
    with pytest.raises(NetworkCalendarError, match="Unknown weekday"):
        DateSet.from_dict({"weekdays": ["funday"]})


# --- Window ------------------------------------------------------------------


def test_window_unknown_calendar_reference_raises():
    with pytest.raises(NetworkCalendarError, match="Unknown calendar reference"):
        Window.from_dict({"days": "no_such_calendar"}, {})


def test_window_with_no_fields_matches_everything():
    window = Window.from_dict({}, {})
    assert window.matches(datetime(2026, 6, 15, 3, 0), is_holiday=True)


# --- NetworkCalendar: band matching --------------------------------------------


def test_winter_weekday_daytime_matches_hoglast():
    calendar = NetworkCalendar.from_resolved(GOTEBORG_RESOLVED)
    holidays = HolidayCache()
    when = _local(2026, 1, 14, 11, 0)  # Wednesday, January
    band = calendar.current_band(when, holidays)
    assert band is not None
    assert band.name == "höglast"


def test_summer_weekday_daytime_falls_back_to_laglast():
    """Same wall-clock hour, out of season -- the low-load band applies."""
    calendar = NetworkCalendar.from_resolved(GOTEBORG_RESOLVED)
    holidays = HolidayCache()
    when = _local(2026, 7, 14, 11, 0)  # Wednesday, July
    band = calendar.current_band(when, holidays)
    assert band is not None
    assert band.name == "låglast"


def test_winter_weekend_falls_back_to_laglast():
    calendar = NetworkCalendar.from_resolved(GOTEBORG_RESOLVED)
    holidays = HolidayCache()
    when = _local(2026, 1, 17, 11, 0)  # Saturday
    band = calendar.current_band(when, holidays)
    assert band is not None
    assert band.name == "låglast"


def test_winter_weekday_night_falls_back_to_laglast():
    calendar = NetworkCalendar.from_resolved(GOTEBORG_RESOLVED)
    holidays = HolidayCache()
    when = _local(2026, 1, 14, 21, 0)  # Wednesday, after 20:00
    band = calendar.current_band(when, holidays)
    assert band is not None
    assert band.name == "låglast"


def test_holiday_excludes_a_weekday_from_hoglast():
    from homeassistant.util import dt as dt_util

    calendar = NetworkCalendar.from_resolved(GOTEBORG_RESOLVED)
    holidays = HolidayCache()
    when = _local(2026, 1, 14, 11, 0)
    holidays._answers[dt_util.as_local(when).date()] = True
    band = calendar.current_band(when, holidays)
    assert band is not None
    assert band.name == "låglast"


def test_first_match_wins():
    resolved = {
        "energy_bands": [
            {"name": "a", "buy": {"adder": 1.0}},
            {"name": "b", "buy": {"adder": 2.0}},
        ]
    }
    calendar = NetworkCalendar.from_resolved(resolved)
    band = calendar.current_band(_local(2026, 1, 1, 12, 0), HolidayCache())
    assert band is not None
    assert band.name == "a"


def test_no_band_matches_leaves_price_unchanged():
    """A profile with no unconditional fallback band is not an error."""
    resolved = {
        "energy_bands": [
            {"name": "only-november", "months": [11], "buy": {"adder": 99.0}},
        ]
    }
    calendar = NetworkCalendar.from_resolved(resolved)
    holidays = HolidayCache()
    t0 = _local(2026, 6, 1, 12, 0)
    buy = Series([Point(t0, 1.0)])
    buy2, _sell2 = calendar.apply_bands(buy, Series.empty(), holidays)
    assert buy2.values == pytest.approx((1.0,))


def test_apply_bands_is_a_no_op_with_no_bands_configured():
    calendar = NetworkCalendar.from_resolved({**GOTEBORG_RESOLVED, "energy_bands": []})
    t0 = _local(2026, 1, 1, 12, 0)
    buy = Series([Point(t0, 1.0)])
    sell = Series([Point(t0, 0.5)])
    buy2, sell2 = calendar.apply_bands(buy, sell, HolidayCache())
    assert buy2.values == buy.values
    assert sell2.values == sell.values


def test_apply_bands_adjusts_buy_and_sell_independently():
    resolved = {
        "energy_bands": [
            {"name": "band", "buy": {"adder": 0.34}, "sell": {"adder": -0.02}},
        ]
    }
    calendar = NetworkCalendar.from_resolved(resolved)
    t0 = _local(2026, 1, 1, 12, 0)
    buy = Series([Point(t0, 1.0)])
    sell = Series([Point(t0, 1.0)])
    buy2, sell2 = calendar.apply_bands(buy, sell, HolidayCache())
    assert buy2.values == pytest.approx((1.34,))
    assert sell2.values == pytest.approx((0.98,))


# --- demand charge / window --------------------------------------------------


def test_demand_charge_parses_measure():
    calendar = NetworkCalendar.from_resolved(GOTEBORG_RESOLVED)
    assert calendar.demand_charge is not None
    assert calendar.demand_charge.measure.interval == timedelta(minutes=60)
    assert calendar.demand_charge.measure.aggregate == "mean_top_n"
    assert calendar.demand_charge.measure.n == 3
    assert calendar.demand_charge.measure.distinct_days is True


def test_in_demand_window_true_with_no_demand_charge_configured():
    calendar = NetworkCalendar.from_resolved({**GOTEBORG_RESOLVED, "demand_charge": {}})
    assert calendar.in_demand_window(_local(2026, 7, 1, 3, 0), HolidayCache())


def test_in_demand_window_respects_the_window():
    calendar = NetworkCalendar.from_resolved(GOTEBORG_RESOLVED)
    holidays = HolidayCache()
    assert calendar.in_demand_window(_local(2026, 1, 14, 11, 0), holidays)
    assert not calendar.in_demand_window(_local(2026, 1, 14, 21, 0), holidays)
    assert not calendar.in_demand_window(_local(2026, 7, 14, 11, 0), holidays)


def test_unknown_aggregate_raises():
    with pytest.raises(NetworkCalendarError, match="Unknown measure\\.aggregate"):
        NetworkCalendar.from_resolved({"demand_charge": {"measure": {"aggregate": "banana"}}})


# --- HolidayCache ------------------------------------------------------------


def test_holiday_cache_defaults_to_not_a_holiday():
    cache = HolidayCache()
    assert cache.get(datetime(2026, 1, 1).date()) is False


async def test_holiday_cache_with_no_entity_degrades_safely():
    cache = HolidayCache()
    warnings = await cache.async_ensure(None, None, {datetime(2026, 1, 1).date()})
    assert cache.degraded is True
    assert warnings


# --- next_change ---------------------------------------------------------------


def test_next_change_finds_the_hour_boundary():
    calendar = NetworkCalendar.from_resolved(GOTEBORG_RESOLVED)
    holidays = HolidayCache()
    now = _local(2026, 1, 14, 6, 30)  # Wednesday, before höglast opens
    change = calendar.next_change(now, holidays)
    assert change is not None
    when, band = change
    assert band is not None
    assert band.name == "höglast"
    from homeassistant.util import dt as dt_util

    assert dt_util.as_local(when).hour == 7
    assert dt_util.as_local(when).minute == 0


def test_next_change_none_for_a_single_unconditional_band():
    calendar = NetworkCalendar.from_resolved({"energy_bands": [{"name": "flat", "buy": {}}]})
    assert calendar.next_change(_local(2026, 1, 1, 12, 0), HolidayCache()) is None


def test_next_change_none_with_no_bands():
    calendar = NetworkCalendar.from_resolved({**GOTEBORG_RESOLVED, "energy_bands": []})
    assert calendar.next_change(_local(2026, 1, 1, 12, 0), HolidayCache()) is None

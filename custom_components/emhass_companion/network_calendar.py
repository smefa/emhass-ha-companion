"""Resolve a network (grid operator) tariff profile against wall-clock time.

Copyright (C) 2026 Tomas Smedberg.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. It is distributed without any warranty; see the LICENSE file at
the repository root for the full terms.

A network profile (``profiles/schema.py``'s ``network`` kind) describes a
calendar in the tariff sheet's own terms: a season, a weekday set, an hour
range, a red-day exclusion. Nothing here is a function of ``(spot, time)`` the
way ``tariff.py`` is -- ``energy_bands`` need a season and a business-day
test, and a demand-charge window needs the same two things again. This module
is the one place that turns those YAML-shaped, already-rendered blocks into
something that can actually be asked "does this instant fall inside band X".

Everything here matches against **local** wall-clock time, for the same reason
``tariff.py``'s ``_apply_template`` does: a tariff sheet's "07:00-20:00" means
the user's own clock, and ``Series`` normalises every timestamp to UTC
internally. Getting this wrong shifts every band by the local UTC offset, and
in a country with daylight saving, by a different amount depending on the time
of year -- see ``tests/integration/test_network_calendar_dst.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .models import Point, Series

_LOGGER = logging.getLogger(__name__)

_WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# How far ahead the band sensor's "next change" search is willing to look
# before giving up. Bounded rather than open-ended: a profile whose only band
# boundary is a season edge (Nov 1) asked about on Nov 2 would otherwise walk
# almost a year, one minute at a time, for a value nobody is watching that
# closely -- see NetworkCalendar.next_change.
_NEXT_CHANGE_SEARCH = timedelta(days=7)
_NEXT_CHANGE_STEP = timedelta(minutes=1)


class NetworkCalendarError(ValueError):
    """A network profile's calendar/band/window configuration is unusable."""


# --- price adjustment ---------------------------------------------------------


@dataclass(slots=True)
class PriceAdjustment:
    """The same ``multiplier``/``adder`` shape a tariff side already uses.

    Kept separate from ``tariff.TariffSide`` rather than reused: a tariff side
    also carries a ``mode`` (linear/template/passthrough) that means nothing
    for a band, which only ever adds a flat per-kWh figure on top of whatever
    the supplier-side tariff already produced.
    """

    multiplier: float = 1.0
    adder: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PriceAdjustment:
        data = data or {}
        try:
            return cls(
                multiplier=float(data.get("multiplier", 1.0)),
                adder=float(data.get("adder", 0.0)),
            )
        except (TypeError, ValueError) as err:
            raise NetworkCalendarError(f"Invalid multiplier/adder in {data!r}: {err}") from err

    def apply(self, value: float) -> float:
        return value * self.multiplier + self.adder


# --- calendar / window matching -----------------------------------------------


@dataclass(slots=True)
class DateSet:
    """A named ``calendar:`` entry: a weekday set, optionally minus red days."""

    weekdays: frozenset[int]
    """Python's ``date.weekday()`` convention: 0 = Monday .. 6 = Sunday."""
    exclude_holidays: bool
    holiday_entity: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DateSet:
        if not isinstance(data, dict):
            raise NetworkCalendarError(f"A calendar entry must be a mapping, got {data!r}")
        names = data.get("weekdays") or list(_WEEKDAY_NAMES)
        try:
            weekdays = frozenset(_WEEKDAY_NAMES.index(str(name).strip().lower()) for name in names)
        except ValueError as err:
            raise NetworkCalendarError(
                f"Unknown weekday in {names!r}; expected one of {_WEEKDAY_NAMES}"
            ) from err
        return cls(
            weekdays=weekdays,
            exclude_holidays=bool(data.get("exclude_holidays", False)),
            holiday_entity=(data.get("holiday_entity") or None),
        )

    def matches(self, local_when: datetime, *, is_holiday: bool) -> bool:
        if local_when.weekday() not in self.weekdays:
            return False
        return not (self.exclude_holidays and is_holiday)


@dataclass(slots=True)
class HourRange:
    """A local wall-clock range, allowed to wrap midnight (``"22:00-06:00"``)."""

    start: time
    end: time

    @classmethod
    def parse(cls, text: str) -> HourRange:
        try:
            start_text, end_text = str(text).split("-", 1)
            return cls(time.fromisoformat(start_text.strip()), time.fromisoformat(end_text.strip()))
        except (TypeError, ValueError) as err:
            raise NetworkCalendarError(
                f'Invalid hours range {text!r}; expected e.g. "07:00-20:00"'
            ) from err

    def contains(self, when: time) -> bool:
        if self.start <= self.end:
            return self.start <= when < self.end
        # Wraps midnight: "inside" is everything from start to midnight, plus
        # everything from midnight to end -- e.g. 22:00-06:00 is 22:00..23:59
        # and 00:00..05:59.
        return when >= self.start or when < self.end


@dataclass(slots=True)
class Window:
    """One ``months``/``days``/``hours`` match condition.

    Shared shape between an energy band and a demand-charge window -- the plan
    calls both a "window" for exactly this reason (network_tariffs_plan.md,
    "The window mask"). Any field left unset matches everything on that axis.
    """

    months: frozenset[int] | None
    days: DateSet | None
    hours: HourRange | None

    @classmethod
    def from_dict(cls, data: dict[str, Any], calendars: dict[str, DateSet]) -> Window:
        months_raw = data.get("months")
        months = None
        if months_raw not in (None, ""):
            try:
                months = frozenset(int(month) for month in months_raw)
            except (TypeError, ValueError) as err:
                raise NetworkCalendarError(f"Invalid months {months_raw!r}: {err}") from err

        days: DateSet | None = None
        days_key = data.get("days")
        if days_key:
            days = calendars.get(str(days_key))
            if days is None:
                raise NetworkCalendarError(
                    f"Unknown calendar reference {days_key!r}; defined calendars: "
                    f"{', '.join(sorted(calendars)) or '(none)'}"
                )

        hours_raw = data.get("hours")
        hours = HourRange.parse(hours_raw) if hours_raw else None

        return cls(months=months, days=days, hours=hours)

    def matches(self, local_when: datetime, *, is_holiday: bool) -> bool:
        if self.months is not None and local_when.month not in self.months:
            return False
        if self.days is not None and not self.days.matches(local_when, is_holiday=is_holiday):
            return False
        return not (self.hours is not None and not self.hours.contains(local_when.time()))

    @property
    def is_unrestricted(self) -> bool:
        """Whether this window matches every instant -- no month, day or hour
        restriction at all. What ``mechanism 2``'s v0.18.0 fallback checks
        before pricing a peak with no window mask to restrict it: a demand
        charge whose own window is already "all the time" needs no mask to be
        priced correctly, since EMHASS's unmasked epigraph *is* that window.
        """
        return self.months is None and self.days is None and self.hours is None


# --- energy bands ---------------------------------------------------------------


@dataclass(slots=True)
class Band:
    """One ``energy_bands:`` entry: a window, plus what it adds to buy/sell."""

    name: str
    window: Window
    buy: PriceAdjustment
    sell: PriceAdjustment


def _parse_bands(raw_bands: list[Any], calendars: dict[str, DateSet]) -> list[Band]:
    bands: list[Band] = []
    for index, raw in enumerate(raw_bands):
        if not isinstance(raw, dict):
            raise NetworkCalendarError(f"energy_bands[{index}] must be a mapping, got {raw!r}")
        bands.append(
            Band(
                name=str(raw.get("name") or f"band {index + 1}"),
                window=Window.from_dict(raw, calendars),
                buy=PriceAdjustment.from_dict(raw.get("buy")),
                sell=PriceAdjustment.from_dict(raw.get("sell")),
            )
        )
    return bands


# --- demand charge (consulted by peaks.py and payload.py) --------------------


@dataclass(slots=True)
class DemandMeasure:
    interval: timedelta
    aggregate: str
    """``"max"`` or ``"mean_top_n"`` -- see peaks.py's aggregation core."""
    n: int
    distinct_days: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DemandMeasure:
        raw_interval = str(data.get("interval") or "60min").strip().lower()
        try:
            minutes = int(raw_interval.removesuffix("min"))
        except ValueError as err:
            raise NetworkCalendarError(
                f'Invalid measure.interval {raw_interval!r}; expected e.g. "60min"'
            ) from err
        aggregate = str(data.get("aggregate") or "max")
        if aggregate not in ("max", "mean_top_n"):
            raise NetworkCalendarError(
                f"Unknown measure.aggregate {aggregate!r}; expected 'max' or 'mean_top_n'"
            )
        return cls(
            interval=timedelta(minutes=minutes),
            aggregate=aggregate,
            n=int(data.get("n") or 1),
            distinct_days=bool(data.get("distinct_days", False)),
        )


@dataclass(slots=True)
class DemandCharge:
    window: Window | None
    measure: DemandMeasure
    period: str
    rate_per_kw: float | None
    rate_basis: str

    @classmethod
    def from_dict(cls, data: dict[str, Any], calendars: dict[str, DateSet]) -> DemandCharge:
        window_raw = data.get("window")
        window = Window.from_dict(window_raw, calendars) if window_raw else None
        rate_raw = data.get("rate_per_kw")
        try:
            rate = float(rate_raw) if rate_raw not in (None, "") else None
        except (TypeError, ValueError) as err:
            raise NetworkCalendarError(f"Invalid demand_charge.rate_per_kw {rate_raw!r}") from err
        return cls(
            window=window,
            measure=DemandMeasure.from_dict(data.get("measure") or {}),
            period=str(data.get("period") or "month"),
            rate_per_kw=rate,
            rate_basis=str(data.get("rate_basis") or "month"),
        )


# --- capacity limit (consulted by payload.py) --------------------------------


@dataclass(slots=True)
class CapacityLimit:
    """A ``capacity_limit:`` block: an optional hard ceiling on grid import.

    Distinct from ``demand_charge`` even though both describe a window and a
    kW figure: this one is a subscribed capacity tier a household is
    contractually inside, priced or not, while the demand charge is what
    *sets* the number worth protecting. The two are allowed to share a window
    -- ``window: same_as_demand_charge`` -- because a subscribed tier is
    usually the same high-load period the demand charge bills against, but
    nothing requires it.
    """

    subscribed_kw: float | None
    window: Window | None
    headroom_kw: float

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        calendars: dict[str, DateSet],
        demand_window: Window | None,
    ) -> CapacityLimit:
        if not isinstance(data, dict):
            raise NetworkCalendarError(f"capacity_limit must be a mapping, got {data!r}")
        subscribed_raw = data.get("subscribed_kw")
        try:
            subscribed_kw = float(subscribed_raw) if subscribed_raw not in (None, "") else None
        except (TypeError, ValueError) as err:
            raise NetworkCalendarError(
                f"Invalid capacity_limit.subscribed_kw {subscribed_raw!r}: {err}"
            ) from err
        window_raw = data.get("window")
        if window_raw in (None, "same_as_demand_charge"):
            window = demand_window
        else:
            window = Window.from_dict(window_raw, calendars)
        try:
            headroom_kw = float(data.get("headroom_kw") or 0.0)
        except (TypeError, ValueError) as err:
            raise NetworkCalendarError(
                f"Invalid capacity_limit.headroom_kw {data.get('headroom_kw')!r}: {err}"
            ) from err
        return cls(subscribed_kw=subscribed_kw, window=window, headroom_kw=headroom_kw)

    @property
    def ceiling_kw(self) -> float | None:
        """``subscribed_kw - headroom_kw``, or ``None`` with no tier set at
        all -- a profile may define ``capacity_limit:`` for its window alone
        (shared by the demand-charge fallback) with no hard ceiling of its
        own."""
        if self.subscribed_kw is None:
            return None
        return max(0.0, self.subscribed_kw - self.headroom_kw)


# --- the resolved calendar --------------------------------------------------


class HolidayCache:
    """Per-date "is this a red day" answers, resolved via ``workday.check_date``.

    A public holiday calendar is fixed once a year has passed, so an answer is
    cached forever rather than expired -- the cost of this cache is a few dozen
    dates a year, for the lifetime of the Home Assistant process.

    Unresolved dates read as "not a holiday". That is the safe direction on
    both sides of this feature: it over-prices a high-load-season energy band
    rather than under-pricing it, and it over-protects a demand-charge window
    rather than letting a real high-load hour through unshaved. See
    docs/network_tariffs_plan.md, "Calendars: weekdays, seasons and holidays".
    """

    def __init__(self) -> None:
        self._answers: dict[date, bool] = {}
        self.degraded = False
        """Set once any lookup has failed. Surfaced so a run can warn that the
        weekday-only fallback is in effect, rather than silently over-shaving."""

    def get(self, day: date) -> bool:
        return self._answers.get(day, False)

    async def async_ensure(
        self, hass: HomeAssistant, entity_id: str | None, dates: set[date]
    ) -> list[str]:
        """Resolve every date not already cached. Returns run-warning strings.

        One call per missing date, not one call for the whole set: ``workday.
        check_date`` answers for a single date, and a horizon only ever spans
        two or three distinct local dates (network_tariffs_plan.md: "resolve
        each distinct local date in the horizon... once per run, cache per
        date"), so this is a handful of calls, not one per timestep.
        """
        missing = sorted(dates - self._answers.keys())
        if not missing:
            return []
        if not entity_id:
            for day in missing:
                self._answers[day] = False
            self.degraded = True
            return [
                "No Workday sensor configured for this network profile; treating every "
                "day as a business day, which over- rather than under-prices the tariff."
            ]

        warnings: list[str] = []
        for day in missing:
            try:
                response = await hass.services.async_call(
                    "workday",
                    "check_date",
                    {"check_date": day.isoformat()},
                    target={"entity_id": entity_id},
                    blocking=True,
                    return_response=True,
                )
                per_entity = (response or {}).get(entity_id) or {}
                is_workday = bool(per_entity["workday"])
            except Exception as err:  # noqa: BLE001 - any of these means "cannot answer today"
                self.degraded = True
                warnings.append(
                    f"Could not resolve {day.isoformat()} against {entity_id} ({err}); "
                    "treating it as a business day, which over- rather than "
                    "under-prices the network tariff."
                )
                self._answers[day] = False
                continue
            # `workday` already conflates "not a weekend" and "not a holiday"
            # under however that entity is configured; our own DateSet.weekdays
            # already excludes weekends independently, so treating `not
            # workday` as "holiday" only ever *adds* a redundant weekend
            # exclusion on top of one already applied -- it never wrongly
            # excludes a plain business day. See the Göteborg profile's notes
            # for why the entity must itself be configured with weekends and
            # holidays excluded.
            self._answers[day] = not is_workday
        return warnings


@dataclass(slots=True)
class NetworkCalendar:
    """A fully-parsed network profile, ready to be matched against timestamps."""

    calendars: dict[str, DateSet] = field(default_factory=dict)
    bands: list[Band] = field(default_factory=list)
    demand_charge: DemandCharge | None = None
    capacity_limit: CapacityLimit | None = None

    @classmethod
    def from_resolved(cls, resolved: dict[str, Any]) -> NetworkCalendar:
        """Build from the already-rendered dict :func:`profiles.resolve_network` returns."""
        calendars = {
            str(key): DateSet.from_dict(value)
            for key, value in (resolved.get("calendar") or {}).items()
        }
        bands = _parse_bands(resolved.get("energy_bands") or [], calendars)
        demand_raw = resolved.get("demand_charge") or {}
        demand_charge = DemandCharge.from_dict(demand_raw, calendars) if demand_raw else None
        capacity_raw = resolved.get("capacity_limit") or {}
        capacity_limit = (
            CapacityLimit.from_dict(
                capacity_raw, calendars, demand_charge.window if demand_charge else None
            )
            if capacity_raw
            else None
        )
        return cls(
            calendars=calendars,
            bands=bands,
            demand_charge=demand_charge,
            capacity_limit=capacity_limit,
        )

    # -- holiday plumbing -------------------------------------------------------

    def holiday_entities(self) -> set[str]:
        return {
            c.holiday_entity
            for c in self.calendars.values()
            if c.exclude_holidays and c.holiday_entity
        }

    def dates_in(self, series: Series) -> set[date]:
        return {dt_util.as_local(point.time).date() for point in series}

    # -- energy bands -------------------------------------------------------------

    def _match(self, local_when: datetime, holidays: HolidayCache) -> Band | None:
        is_holiday = holidays.get(local_when.date())
        for band in self.bands:
            if band.window.matches(local_when, is_holiday=is_holiday):
                return band
        return None

    def apply_bands(
        self, buy: Series, sell: Series, holidays: HolidayCache
    ) -> tuple[Series, Series]:
        """Apply the first matching band's adder/multiplier to every point.

        Same ``Series`` in, same ``Series`` out, exactly like ``Tariff.compose``
        -- ``payload.py`` never has to know a network profile exists. A point
        matching no band at all (no unconditional fallback band was written)
        passes through unchanged rather than erroring: a profile author who
        forgot the fallback gets silently-correct behaviour for the hours they
        did cover, not a broken run for the ones they didn't.
        """
        if not self.bands or not buy:
            return buy, sell
        return (
            self._apply_side(buy, holidays, side="buy"),
            self._apply_side(sell, holidays, side="sell") if sell else sell,
        )

    def _apply_side(self, series: Series, holidays: HolidayCache, *, side: str) -> Series:
        points: list[Point] = []
        for point in series:
            local_when = dt_util.as_local(point.time)
            band = self._match(local_when, holidays)
            adjustment = (band.buy if side == "buy" else band.sell) if band else PriceAdjustment()
            points.append(Point(point.time, adjustment.apply(point.value)))
        return Series(points)

    def current_band(self, now: datetime, holidays: HolidayCache) -> Band | None:
        """The band in force at ``now``, or ``None`` if nothing matched."""
        return self._match(dt_util.as_local(now), holidays)

    def next_change(
        self, now: datetime, holidays: HolidayCache
    ) -> tuple[datetime, Band | None] | None:
        """When the matched band next differs from the one in force at ``now``.

        Bounded to :data:`_NEXT_CHANGE_SEARCH` -- a season edge months away is
        real, but walking towards it a minute at a time to answer a sensor
        attribute nobody is watching that closely is not worth the cycles.
        Returns ``None`` both when there is nothing to change into (a single
        unconditional band) and when the next change is further out than the
        search window; either way, the sensor simply omits the attribute.
        """
        if len(self.bands) <= 1 and all(band.window.is_unrestricted for band in self.bands):
            return None

        current = self.current_band(now, holidays)
        # Start one step in: `now` itself is already known.
        cursor = _floor_to_minute(now) + _NEXT_CHANGE_STEP
        end = now + _NEXT_CHANGE_SEARCH
        while cursor <= end:
            candidate = self._match(dt_util.as_local(cursor), holidays)
            if candidate is not current and (
                candidate is None or current is None or candidate.name != current.name
            ):
                return cursor, candidate
            cursor += _NEXT_CHANGE_STEP
        return None

    # -- demand charge window (consumed by peaks.py via the coordinator) -------

    def in_demand_window(self, when: datetime, holidays: HolidayCache) -> bool:
        """Whether ``when`` falls inside the configured demand-charge window.

        ``True`` with no ``demand_charge:`` block at all -- a profile with no
        demand charge has nothing to window, and a caller asking anyway (an
        energy-bands-only profile) should see every timestep count rather than
        none, which is the "no restriction configured" reading of an absent
        window everywhere else in this schema.
        """
        if self.demand_charge is None or self.demand_charge.window is None:
            return True
        return self.demand_charge.window.matches(
            dt_util.as_local(when), is_holiday=holidays.get(dt_util.as_local(when).date())
        )

    def in_capacity_window(self, when: datetime, holidays: HolidayCache) -> bool:
        """Whether ``when`` falls inside the configured ``capacity_limit`` window.

        ``True`` with no ``capacity_limit:`` block, or one with no window of
        its own -- same "no restriction configured" reading as
        :meth:`in_demand_window`.
        """
        if self.capacity_limit is None or self.capacity_limit.window is None:
            return True
        return self.capacity_limit.window.matches(
            dt_util.as_local(when), is_holiday=holidays.get(dt_util.as_local(when).date())
        )


def _floor_to_minute(when: datetime) -> datetime:
    return when.replace(second=0, microsecond=0)

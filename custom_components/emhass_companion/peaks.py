"""The billing-period demand-charge peak: tracked, and now priced.

Copyright (C) 2026 Tomas Smedberg.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. It is distributed without any warranty; see the LICENSE file at
the repository root for the full terms.

Phase 2 of ``docs/network_tariffs_plan.md`` built the observation-only half of
this: a household's demand-charge peak for the current billing period --
Göteborg Energi's "mean of the three highest hourly peaks, on three distinct
days" or Amber's "single highest 30-minute interval" -- persisted and
published for a sensor to read. Phase 3 (``effective_rate_per_kw`` below) adds
the arithmetic that turns a tariff sheet's own rate into what ``payload.py``
sends as ``capacity_cost_per_kw``, and ``coordinator.py`` sends
:attr:`PeakTracker.floor_kw` on as EMHASS's ``current_period_peak`` -- the
"memory" described in the plan's "What exists today, and where it falls
short" section.

Shaped like ``metering.py``'s ``SavingsTracker`` on purpose, one level up: a
:class:`~.metering.Meter` watched on every state change rather than a timer,
for the same reason -- an interval average built from sparse samples is wrong
in the same way a net grid position is -- a ``Store``, a scheduled rollover,
and restore-on-restart. ``metering.py`` keeps the money in ``savings.py`` and
the Home Assistant plumbing in itself; this module keeps that same split, but
in one file rather than two, because the aggregation arithmetic below is not
enough code to earn a second module the way a full day's ledger is.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .metering import Meter

_LOGGER = logging.getLogger(__name__)

_STORE_VERSION = 1

_PUBLISH_INTERVAL = timedelta(seconds=60)
"""How often the sensors are allowed to be rewritten. Same figure and the
same reasoning as ``metering.py``'s: the record updates on every meter tick,
only publication is rate-limited."""

_PUBLISH_THRESHOLD = 0.005
"""Smallest change in kW worth a state write -- below what any of these
sensors would display, and what keeps a quiet house out of the recorder."""

_SAVE_DELAY = 120
"""Seconds to coalesce writes over. Losing the last two minutes of the
current partial interval to a hard power cut is cheaper than a ``.storage``
write on every meter tick; see :meth:`PeakTracker.async_load` for how the
rest of that interval is not lost either way."""

AGGREGATE_MAX = "max"
"""The highest qualifying interval so far this period. Amber's shape."""

AGGREGATE_MEAN_TOP_N = "mean_top_n"
"""The mean of the top ``n`` qualifying intervals, optionally deduplicated to
one per local day first. Göteborg's shape, with ``n=3`` and
``distinct_days=True``."""

AGGREGATES = (AGGREGATE_MAX, AGGREGATE_MEAN_TOP_N)


# -- the pure aggregation math --------------------------------------------------
#
# No Home Assistant below this line down to "the Home Assistant plumbing":
# testable against hand-picked intervals the same way savings.py's Ledger is
# testable against hand-worked money. Kept in this file rather than a second
# module because, unlike the day's full cost ledger, there is only a handful
# of functions' worth of it.


@dataclass(frozen=True, slots=True)
class Interval:
    """One completed, qualifying demand interval.

    ``local_day`` is precomputed by the caller rather than derived from
    ``start`` in here, mirroring how ``Ledger.day`` is handed to it by
    ``metering.py`` rather than computed by ``savings.py`` itself -- it keeps
    the aggregation functions below free of any timezone dependency of their
    own, and therefore trivially testable against days chosen by hand.
    """

    start: datetime
    """The interval's own start, UTC-normalised like every other timestamp in
    this codebase (see ``models.py``'s ``Series``) even though the boundary
    it sits on was chosen in local wall-clock terms -- see ``_bucket_start``.
    """

    local_day: str
    """ISO date the interval's *start* falls on, in local time. Grouping key
    for "distinct days"; a date rather than a datetime because a tariff's
    calendar clause is about calendar days, not about instants."""

    kw: float
    """``kwh_in_interval / interval_hours`` -- the interval's own average
    power, which is what a demand meter actually bills, not its peak
    instantaneous draw."""

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "local_day": self.local_day, "kw": self.kw}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Interval | None:
        """Restore one stored interval, or None for anything unreadable.

        Skipped rather than raised: one corrupt entry in a month's worth of
        stored peaks is not worth taking the tracker down over, the same
        tolerance ``Ledger.from_dict`` extends to an unrecognised field.
        """
        if not data:
            return None
        raw_start = data.get("start")
        start = dt_util.parse_datetime(raw_start) if isinstance(raw_start, str) else None
        local_day = data.get("local_day")
        kw = data.get("kw")
        if start is None or not isinstance(local_day, str) or kw is None:
            return None
        try:
            return cls(start=start, local_day=local_day, kw=float(kw))
        except (TypeError, ValueError):
            return None


def _per_day_bests(intervals: Sequence[Interval]) -> dict[str, Interval]:
    """One interval per local day: whichever is highest.

    A demand tariff bills a day's contribution by its single worst interval,
    not by every qualifying interval in it -- Göteborg's "on three different
    days" clause exists specifically so one bad afternoon cannot supply more
    than one of the top three. Reducing to this dict first is what makes
    "distinct days" true by construction rather than by a second check
    threaded through the rest of the arithmetic.
    """
    best: dict[str, Interval] = {}
    for interval in intervals:
        current = best.get(interval.local_day)
        if current is None or interval.kw > current.kw:
            best[interval.local_day] = interval
    return best


def _top_n_intervals(
    intervals: Sequence[Interval], *, n: int, distinct_days: bool
) -> list[Interval]:
    """The intervals a ``mean_top_n`` aggregate is built from, highest first."""
    pool = list(_per_day_bests(intervals).values()) if distinct_days else list(intervals)
    pool.sort(key=lambda interval: interval.kw, reverse=True)
    return pool[:n]


def aggregate_kw(
    intervals: Sequence[Interval], *, mode: str, n: int = 1, distinct_days: bool = False
) -> float:
    """The billed aggregate so far this period, per ``mode``."""
    if not intervals:
        return 0.0
    if mode == AGGREGATE_MAX:
        return max(interval.kw for interval in intervals)
    top = _top_n_intervals(intervals, n=n, distinct_days=distinct_days)
    return sum(interval.kw for interval in top) / len(top) if top else 0.0


def incurred_floor_kw(
    intervals: Sequence[Interval],
    *,
    mode: str,
    n: int = 1,
    distinct_days: bool = False,
    today: str,
) -> float:
    """The level at which a new peak *today* would actually raise the bill.

    Not simply "the highest interval so far" -- see
    ``docs/network_tariffs_plan.md``'s "The incurred-peak floor" section, and
    :meth:`PeakTracker.floor_kw` for why this is what feeds
    ``current_period_peak`` rather than :func:`aggregate_kw`'s own result.

    * ``max``: the current aggregate. Any higher interval is, by definition,
      a new maximum.
    * ``mean_top_n``, not deduplicated by day: every qualifying interval is
      an independent candidate for the top ``n``, today's included, so a new
      one only ever *adds* a competing candidate. The threshold it has to
      clear is simply the smallest value already in the top ``n`` -- there is
      no "replaces its own earlier entry" case, because nothing here ever
      reduces a day to one representative.
    * ``mean_top_n``, deduplicated by day (Göteborg's shape): a day is
      reduced to its own best interval *before* the top ``n`` is taken, so a
      bigger peak later the same day does not add a fourth candidate -- it
      can only raise today's existing one. If today already holds one of the
      top ``n`` entries, the level worth beating is therefore not the
      smallest of the top ``n`` (which may well be some *other* day, and
      today has already cleared it); it is today's own current entry,
      whichever of the two is larger. Sending only the smallest would have
      the plan defend a level it has already exceeded today, for nothing --
      exactly the case the plan calls out as the one worth getting right.

    When fewer than ``n`` days qualify yet -- the start of a month, or the
    start of a season -- the floor is 0: any peak at all would enter the top
    ``n``, so there is nothing yet to protect.
    """
    if mode == AGGREGATE_MAX:
        return aggregate_kw(intervals, mode=mode)
    top = _top_n_intervals(intervals, n=n, distinct_days=distinct_days)
    if len(top) < n:
        return 0.0
    displacement = min(interval.kw for interval in top)
    if not distinct_days:
        return displacement
    todays = next((interval for interval in top if interval.local_day == today), None)
    return max(displacement, todays.kw) if todays is not None else displacement


def effective_rate_per_kw(
    *, rate_per_kw: float, rate_basis: str, aggregate: str, n: int, days_in_period: int
) -> float:
    """Convert a tariff sheet's own rate into what ``capacity_cost_per_kw``
    actually has to be.

    EMHASS charges ``rate * peak_import`` **once per optimisation** against a
    billing period that runs for many optimisations, so ``rate_per_kw`` has to
    be the marginal cost, over one run, of raising the period's measured peak
    by one kW -- not the number printed on the invoice. Two conversions stack:

    * ``rate_basis``: a rate quoted **per day** (Amber) is charged for every
      day of the billing period once a new peak is set, so it is multiplied by
      ``days_in_period``. A rate quoted **per month** (Göteborg) already covers
      the whole period and is left alone.
    * ``aggregate``: a plain ``max`` peak is charged in full the moment it is
      set. A ``mean_top_n`` peak (Göteborg's "mean of the three highest,
      on three different days") only ever contributes ``1/n`` of itself to the
      bill, so the rate is divided by ``n``.

    See ``docs/network_tariffs_plan.md``, "Pricing the peak correctly", for
    the worked Göteborg (``135 / 3 = 45 kr/kW``) and Amber
    (``0.30 * 30 ≈ 9 $/kW``) examples this mirrors.
    """
    basis_multiplier = days_in_period if rate_basis == "day" else 1
    divisor = n if aggregate == AGGREGATE_MEAN_TOP_N else 1
    return rate_per_kw * basis_multiplier / divisor


def days_in_current_period(now: datetime) -> int:
    """Days in the local calendar month ``now`` falls in.

    What ``rate_basis: day`` converts against -- only ``period: month`` is
    supported today (this module's own rollover is monthly), so "the period"
    always means the local calendar month.
    """
    local = dt_util.as_local(now)
    return calendar.monthrange(local.year, local.month)[1]


def _bucket_start(when: datetime, interval: timedelta) -> datetime:
    """Floor ``when`` onto the interval boundary, in local wall-clock time.

    Local for the same reason ``tariff.py``'s ``_apply_template`` insists on
    it: a demand window like "07:00-20:00" and a metering interval like
    "on the hour" both mean the user's own clock, and flooring in UTC would
    shift every boundary by the UTC offset -- and, across Sweden's DST
    transitions, by a different amount depending on the time of year. The
    result is converted back to an absolute UTC-normalised instant before
    being handed back, matching every other timestamp in this codebase; only
    the arithmetic that chooses *which* instant is local.
    """
    local = dt_util.as_local(when)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    whole, _ = divmod(local - midnight, interval)
    return (midnight + whole * interval).astimezone(UTC)


def _local_day(when: datetime) -> str:
    """The local calendar date ``when`` falls on, as an ISO string. Same
    reasoning as ``metering.py``'s function of the same name, one level up:
    a demand tariff's calendar is the user's own, not UTC's."""
    return dt_util.as_local(when).date().isoformat()


def _local_period(when: datetime) -> str:
    """The local calendar month ``when`` falls in, as ``YYYY-MM``."""
    local = dt_util.as_local(when)
    return f"{local.year:04d}-{local.month:02d}"


def _next_period_start(local_now: datetime) -> datetime:
    """Local midnight of the 1st of the month after ``local_now``'s."""
    if local_now.month == 12:
        return local_now.replace(
            year=local_now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return local_now.replace(
        month=local_now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0
    )


# -- the Home Assistant plumbing -------------------------------------------------


class PeakTracker:
    """Keeps one billing period's demand-charge peak current.

    Owned by the config entry for its lifetime, exactly like
    ``SavingsTracker``, and for the same reason: a sensor subscribes to this
    rather than to the coordinator because a meter tick is not an
    optimisation, and a period peak that only updated once per solve would
    lag the real one by up to an MPC interval for no reason.

    Constructor-injected rather than config-reading, again like
    ``SavingsTracker``: the calendar logic that decides whether an instant
    falls inside a demand window belongs to ``network_calendar.py`` (a
    separate module, not yet written when this one was), and the caller --
    whatever resolves the network profile's ``demand_charge.measure`` block
    -- hands over plain values and a predicate rather than this class reading
    configuration for itself.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        meter: Meter,
        *,
        interval: timedelta,
        aggregate: str,
        top_n: int,
        distinct_days: bool,
        in_window: Callable[[datetime], bool],
    ) -> None:
        if aggregate not in AGGREGATES:
            raise ValueError(f"Unknown peak aggregate mode: {aggregate!r}")
        if aggregate == AGGREGATE_MEAN_TOP_N and top_n < 1:
            raise ValueError(f"mean_top_n needs top_n >= 1, got {top_n!r}")

        self.hass = hass
        self.entry = entry
        self.meter = meter
        self._interval = interval
        self._aggregate = aggregate
        self._top_n = top_n
        self._distinct_days = distinct_days
        self._in_window = in_window

        self._period = _local_period(dt_util.utcnow())
        self._intervals: list[Interval] = []
        self._current_start: datetime | None = None
        self._current_kwh: float = 0.0

        self._store: Store[dict[str, Any]] = Store(
            hass, _STORE_VERSION, f"{DOMAIN}_{entry.entry_id}_peaks"
        )
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._last_published: datetime | None = None
        self._last_snapshot: tuple[float, ...] = ()

    # -- lifecycle ------------------------------------------------------------

    async def async_load(self) -> None:
        """Restore the record and the meter baseline from the last run.

        The sharp edge this exists for: a restart five minutes into an hourly
        bucket must neither forget that five minutes nor double-count it once
        the meter's own restored reading picks back up. The fix is that the
        *partial* interval -- ``current_start``/``current_kwh`` -- is
        persisted alongside the completed ones, not just the completed list.
        ``Meter.restore`` is what makes putting the two back together safe:
        it re-adopts its own last reading so the very next ``take()`` returns
        the delta since *before* the restart, which is additional energy on
        top of what was already folded into ``current_kwh`` -- never a
        replay of it, because that energy left the meter's own baseline the
        moment the original ``take()`` call consumed it, before the restart
        ever happened.
        """
        stored = await self._store.async_load()
        now = dt_util.utcnow()
        if not stored:
            return

        if stored.get("period") == self._period:
            self._intervals = [
                interval
                for data in stored.get("intervals") or []
                if (interval := Interval.from_dict(data)) is not None
            ]
            raw_start = stored.get("current_start")
            current_start = (
                dt_util.parse_datetime(raw_start) if isinstance(raw_start, str) else None
            )
            current_kwh = float(stored.get("current_kwh") or 0.0)
        else:
            # Home Assistant was down across a month boundary (or this store
            # predates the current period entirely). Mirrors
            # SavingsTracker's day rollover: last period's record already
            # stood as it was when it closed, and this period starts clean
            # rather than inheriting a stale one.
            current_start = None
            current_kwh = 0.0

        missed = self.meter.restore(self.hass, stored.get("meter"), now=now)
        if missed:
            # A gap too long to price is a gap too long to trust as a demand
            # reading either. Meter has already disowned its own baseline in
            # this branch (see Meter.restore), so there is no honest instant
            # to say the partial interval resumed from -- counted as a
            # qualifying interval would be inventing a number, so, per the
            # plan, it is simply dropped and a fresh bucket opens on the next
            # settle instead.
            current_start = None
            current_kwh = 0.0

        self._current_start = current_start
        self._current_kwh = current_kwh

    @callback
    def async_start(self) -> None:
        """Subscribe to the meter and to the monthly rollover."""
        self.async_stop()
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self.meter.entity_id], self._async_source_changed
            )
        )
        # Same five-seconds-past-the-hour offset as metering.py's own
        # midnight timer, for the same reason: relying on a meter tick to
        # land exactly at local midnight of the 1st would leave a quiet house
        # never rolling over, and the small delay gives anything else with
        # its own midnight reset a chance to land first so it is attributed
        # to the period that is ending rather than the one starting.
        self._unsubs.append(
            async_track_time_change(self.hass, self._async_midnight, hour=0, minute=0, second=5)
        )
        # One immediate settle so a restart's bucket boundary is evaluated
        # now rather than whenever the source next happens to move.
        self._settle(dt_util.utcnow())

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    async def async_shutdown(self) -> None:
        """Flush the record before the entry goes away."""
        self.async_stop()
        self._settle(dt_util.utcnow())
        await self._store.async_save(self._data_to_save())

    @callback
    def async_add_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    # -- accumulation -----------------------------------------------------------

    @callback
    def _async_source_changed(self, _event: Event[EventStateChangedData]) -> None:
        self._settle(dt_util.utcnow())

    @callback
    def _async_midnight(self, now: datetime) -> None:
        self._settle(now)
        self._publish(force=True)

    def _settle(self, now: datetime) -> None:
        """Fold everything that moved since the last settle into the record."""
        self._roll_over(now)
        self._advance_interval(now)
        self._current_kwh += self.meter.take(self.hass, now)
        self._store.async_delay_save(self._data_to_save, _SAVE_DELAY)
        self._publish()

    def _roll_over(self, now: datetime) -> None:
        """Start a fresh period when the local ``(year, month)`` has advanced.

        Checked on every settle rather than trusting the midnight timer
        alone, for exactly the reason ``metering.py``'s ``_roll_over``
        docstring gives one level down: a restart at 00:05 on the 1st would
        otherwise merge two periods into one, and a machine asleep through
        midnight would never fire the timer at all.
        """
        period = _local_period(now)
        if self._period == period:
            return
        _LOGGER.debug(
            "Closing peak record for %s: aggregate=%.2f kW over %d qualifying interval(s)",
            self._period,
            self.current_aggregate_kw,
            len(self._intervals),
        )
        self._period = period
        self._intervals = []
        self._current_start = None
        self._current_kwh = 0.0

    def _advance_interval(self, now: datetime) -> None:
        """Close the open bucket once wall-clock time has moved past it, and
        open the next one aligned to ``now``.

        This is the one mechanism that has to be right both in ordinary
        operation and across a restart: whether the boundary was crossed
        because a meter tick simply arrived in the next hour, or because Home
        Assistant was down across it, the answer is the same -- finalise
        whatever energy the still-open bucket actually saw, at whatever
        fraction of a full interval that turns out to be, and start counting
        the next one from zero. Only the bucket that was actually open gets
        finalised this way; if a downtime spanned more than one boundary, the
        buckets in between are left unrecorded rather than invented, for the
        same reason ``Meter`` treats a long outage as "no reading" rather
        than as a flat power draw (see ``_RESTORE_MAX_GAP`` in
        ``metering.py``). The one honest inaccuracy left is a bucket that
        happens to close *during* a gap: its measured minutes are all it
        gets, which understates that one interval's average rather than
        losing or duplicating any energy -- the energy that flowed during the
        gap itself lands in the *new* bucket, via the very next ``take()``.
        """
        bucket = _bucket_start(now, self._interval)
        if self._current_start is None:
            self._current_start = bucket
            return
        if bucket == self._current_start:
            return
        self._close_current_interval()
        self._current_start = bucket
        self._current_kwh = 0.0

    def _close_current_interval(self) -> None:
        if self._current_start is None:
            return
        hours = self._interval.total_seconds() / 3600
        kw = self._current_kwh / hours if hours > 0 else 0.0
        if self._in_window(self._current_start):
            self._intervals.append(
                Interval(
                    start=self._current_start, local_day=_local_day(self._current_start), kw=kw
                )
            )

    # -- published figures --------------------------------------------------------

    @property
    def period_key(self) -> str:
        """The current billing period, e.g. ``"2026-08"``."""
        return self._period

    @property
    def days_remaining(self) -> int:
        """Whole local days left before this period rolls over.

        For a sensor attribute: "3 days left" is the context that tells a
        user whether a peak this size is worth fighting today or worth
        riding out.
        """
        local_now = dt_util.as_local(dt_util.utcnow())
        return (_next_period_start(local_now).date() - local_now.date()).days

    @property
    def current_aggregate_kw(self) -> float:
        """The billed aggregate so far this period, per the aggregate mode."""
        return aggregate_kw(
            self._intervals, mode=self._aggregate, n=self._top_n, distinct_days=self._distinct_days
        )

    @property
    def floor_kw(self) -> float:
        """The level a new peak today would have to clear to move the bill.

        See :func:`incurred_floor_kw` for the arithmetic and, in particular,
        the "today already holds an entry" case -- the reason this is not
        simply :attr:`current_aggregate_kw`.
        """
        return incurred_floor_kw(
            self._intervals,
            mode=self._aggregate,
            n=self._top_n,
            distinct_days=self._distinct_days,
            today=_local_day(dt_util.utcnow()),
        )

    @property
    def contributing_intervals(self) -> tuple[Interval, ...]:
        """The completed intervals :attr:`current_aggregate_kw` is built from.

        Not every qualifying interval this period -- specifically the ones
        the billed number actually comes from, so a user asking "why is my
        peak sensor showing 6.2 kW" gets the handful of days and hours that
        answer, rather than a month of interval history to sift through.
        """
        if not self._intervals:
            return ()
        if self._aggregate == AGGREGATE_MAX:
            return (max(self._intervals, key=lambda interval: interval.kw),)
        return tuple(
            _top_n_intervals(self._intervals, n=self._top_n, distinct_days=self._distinct_days)
        )

    def headroom_kw(self, current_draw_kw: float) -> float:
        """How much more can be drawn right now before a new peak raises the bill.

        Against :attr:`floor_kw`, not :attr:`current_aggregate_kw`, even
        though the plan's own entity table
        (``network_tariffs_plan.md`` line 694) shorthands this as "current
        aggregate minus current draw". For a plain ``max`` aggregate the two
        are the same number, so the shorthand is harmless there -- but for
        ``mean_top_n`` the floor can sit on either side of the aggregate (see
        :func:`incurred_floor_kw`), and the floor is *defined* as the level a
        new peak has to clear to move the bill, which is exactly what this
        sensor is meant to answer. Against the aggregate instead, this number
        would be wrong in both directions: it could read "already over" while
        today's own already-dominant peak still has room to spare, and it
        could just as easily read "plenty of room" while the draw in question
        would in fact displace the smallest of the top ``n`` and raise the
        bill outright.
        """
        return self.floor_kw - current_draw_kw

    # -- publication ----------------------------------------------------------

    def _publish(self, *, force: bool = False) -> None:
        """Tell the sensors, if enough has changed to be worth a state write."""
        now = dt_util.utcnow()
        if not force:
            if self._last_published is not None and now - self._last_published < _PUBLISH_INTERVAL:
                return
            snapshot = self._snapshot()
            if self._last_snapshot and all(
                abs(new - old) < _PUBLISH_THRESHOLD
                for new, old in zip(snapshot, self._last_snapshot, strict=True)
            ):
                # Nothing a user could see has moved. Skipping the write
                # keeps a sleeping house out of the recorder entirely.
                self._last_published = now
                return
            self._last_snapshot = snapshot
        else:
            self._last_snapshot = self._snapshot()
        self._last_published = now
        for listener in list(self._listeners):
            listener()

    def _snapshot(self) -> tuple[float, ...]:
        return (self.current_aggregate_kw, self.floor_kw)

    # -- persistence ----------------------------------------------------------

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        return {
            "period": self._period,
            "intervals": [interval.as_dict() for interval in self._intervals],
            "current_start": self._current_start.isoformat() if self._current_start else None,
            "current_kwh": self._current_kwh,
            "meter": self.meter.as_dict(),
        }

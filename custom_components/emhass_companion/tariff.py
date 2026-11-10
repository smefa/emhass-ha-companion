"""Compose raw spot prices into the buy and sell prices EMHASS optimises against.

Kept deliberately country-agnostic. Tariff structures vary enormously between
markets and change from year to year; encoding any particular country's tax
rules here would guarantee this file is wrong for most users and out of date for
the rest. Instead there are three composition modes, and country-specific
numbers stay in the user's configuration where they can be corrected without a
release.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import template as template_helper
from homeassistant.util import dt as dt_util

from .const import CONF_ADDER, CONF_MODE, CONF_MULTIPLIER, CONF_TEMPLATE
from .models import Point, Series, floor_to_step

_LOGGER = logging.getLogger(__name__)

MODE_LINEAR = "linear"
"""spot * multiplier + adder. Covers VAT plus a flat per-kWh markup."""

MODE_TEMPLATE = "template"
"""Arbitrary expression, for tiered or time-of-day structures."""

MODE_PASSTHROUGH = "passthrough"
"""The source series already includes all costs; use it unchanged."""

TARIFF_MODES = (MODE_LINEAR, MODE_TEMPLATE, MODE_PASSTHROUGH)


class TariffError(ValueError):
    """A tariff definition could not be applied."""


@dataclass(slots=True)
class TariffSide:
    """One direction of a tariff: what you pay, or what you are paid."""

    mode: str = MODE_LINEAR
    multiplier: float = 1.0
    adder: float = 0.0
    template: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TariffSide:
        data = data or {}
        mode = data.get(CONF_MODE, MODE_LINEAR)
        if mode not in TARIFF_MODES:
            raise TariffError(f"Unknown tariff mode: {mode!r}")
        return cls(
            mode=mode,
            multiplier=float(data.get(CONF_MULTIPLIER, 1.0)),
            adder=float(data.get(CONF_ADDER, 0.0)),
            template=data.get(CONF_TEMPLATE),
        )

    @property
    def is_identity(self) -> bool:
        """Whether applying this side leaves every price unchanged."""
        if self.mode == MODE_PASSTHROUGH:
            return True
        return self.mode == MODE_LINEAR and self.multiplier == 1.0 and self.adder == 0.0

    def apply(self, hass: HomeAssistant, spot: Series) -> Series:
        """Apply this tariff to a raw spot series."""
        if self.mode == MODE_PASSTHROUGH:
            return spot
        if self.mode == MODE_LINEAR:
            return spot.scaled(self.multiplier, self.adder)
        return self._apply_template(hass, spot)

    def _apply_template(self, hass: HomeAssistant, spot: Series) -> Series:
        if not self.template:
            raise TariffError("Template tariff mode selected but no template given")

        compiled = template_helper.Template(self.template, hass)
        points: list[Point] = []
        for point in spot:
            # `time` is exposed so a template can express time-of-day bands
            # against the timestamp being priced rather than against now().
            # It must be local wall-clock time: `Series` normalises every
            # timestamp to UTC internally (see models.py), and "peak
            # 17:00-20:00" written against `time.hour` means the user's own
            # clock, not UTC -- passing the raw UTC value here would silently
            # shift every band by the local UTC offset.
            rendered = compiled.async_render(
                {"spot": point.value, "time": dt_util.as_local(point.time)},
                parse_result=True,
            )
            try:
                points.append(Point(point.time, float(rendered)))
            except (TypeError, ValueError) as err:
                raise TariffError(
                    f"Tariff template produced a non-numeric result: {rendered!r}"
                ) from err
        return Series(points)


@dataclass(slots=True)
class Tariff:
    """Buy and sell composition, applied to a single spot series."""

    buy: TariffSide
    sell: TariffSide

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Tariff:
        data = data or {}
        return cls(
            buy=TariffSide.from_dict(data.get("buy")),
            sell=TariffSide.from_dict(data.get("sell")),
        )

    @property
    def is_identity(self) -> bool:
        """Whether composing changes neither the buy nor the sell price."""
        return self.buy.is_identity and self.sell.is_identity

    def compose(self, hass: HomeAssistant, spot: Series) -> tuple[Series, Series]:
        """Return ``(buy, sell)`` series derived from ``spot``."""
        if not spot:
            return Series.empty(), Series.empty()
        return self.buy.apply(hass, spot), self.sell.apply(hass, spot)


def native_tariff_series(
    settings: dict[str, Any], start: datetime, end: datetime, step: timedelta
) -> tuple[Series, Series] | None:
    """Expand EMHASS's own peak / off-peak tariff onto the step grid.

    A settings-only price profile (the fixed tariff) hands its prices to
    EMHASS as ``hp_hc_periods`` and a ``constant`` export price, so no price
    series ever passes through :meth:`Tariff.compose` or the network bands.
    This builds the same prices EMHASS would, as ``(buy, sell)`` series from
    ``start`` to ``end``, so they can. Mirrors EMHASS's own expansion: a step
    is peak when its local start time lies within a period, both ends
    inclusive (pandas ``between_time``), and a period whose end is earlier
    than its start wraps past midnight.

    ``None`` when the settings are not a peak / off-peak tariff or cannot be
    read; EMHASS then keeps pricing the run itself. The sell series is empty
    unless the export price is ``constant``.
    """
    if settings.get("load_cost_forecast_method") != "hp_hc_periods":
        return None
    try:
        peak = float(settings["load_peak_hours_cost"])
        offpeak = float(settings["load_offpeak_hours_cost"])
    except (KeyError, TypeError, ValueError):
        return None
    periods = _peak_periods(settings.get("load_peak_hour_periods"))
    if periods is None:
        return None
    sell_value: float | None = None
    if settings.get("production_price_forecast_method") == "constant":
        try:
            sell_value = float(settings["photovoltaic_production_sell_price"])
        except (KeyError, TypeError, ValueError):
            return None
    zone: tzinfo | None = None
    if name := settings.get("tariff_schedule_time_zone"):
        zone = dt_util.get_time_zone(str(name))
    zone = zone or dt_util.get_default_time_zone()

    buy: list[Point] = []
    sell: list[Point] = []
    when = floor_to_step(start, step)
    while True:
        local = when.astimezone(zone).time()
        price = peak if any(_within(local, first, last) for first, last in periods) else offpeak
        buy.append(Point(when, price))
        if sell_value is not None:
            sell.append(Point(when, sell_value))
        if when >= end:
            break
        when += step
    return Series(buy), Series(sell)


def _peak_periods(raw: Any) -> list[tuple[time, time]] | None:
    """``{name: [{start: ..}, {end: ..}]}`` as ``(start, end)`` times, or None."""
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return None
    periods: list[tuple[time, time]] = []
    for entries in raw.values():
        if not isinstance(entries, list):
            return None
        merged: dict[str, Any] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                return None
            merged.update(entry)
        first = dt_util.parse_time(str(merged.get("start", "")))
        last = dt_util.parse_time(str(merged.get("end", "")))
        if first is None or last is None:
            return None
        periods.append((first, last))
    return periods


def _within(moment: time, first: time, last: time) -> bool:
    if first <= last:
        return first <= moment <= last
    return moment >= first or moment <= last

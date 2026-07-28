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
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import template as template_helper

from .const import CONF_ADDER, CONF_MODE, CONF_MULTIPLIER, CONF_TEMPLATE
from .models import Point, Series

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
            rendered = compiled.async_render(
                {"spot": point.value, "time": point.time}, parse_result=True
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

    def compose(self, hass: HomeAssistant, spot: Series) -> tuple[Series, Series]:
        """Return ``(buy, sell)`` series derived from ``spot``."""
        if not spot:
            return Series.empty(), Series.empty()
        return self.buy.apply(hass, spot), self.sell.apply(hass, spot)

"""Tests for spot-to-buy/sell composition.

Template mode needs a running Home Assistant to render, so its rendering path
is covered in tests/integration/test_tariff_template.py -- notably that `time`
is local, not UTC, which is what makes a time-of-day band mean what it says.
Only the template mode's pure error path (no template given) is tested here.
Linear and passthrough are pure and fully covered in this file.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.emhass_companion.models import Point, Series
from custom_components.emhass_companion.tariff import (
    MODE_LINEAR,
    MODE_PASSTHROUGH,
    Tariff,
    TariffError,
    TariffSide,
)

T0 = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
SPOT = Series([Point(T0, 0.40), Point(T0 + timedelta(minutes=30), 0.80)])


def test_linear_applies_multiplier_then_adder():
    """The common case: VAT as a multiplier, fees and tax as an adder."""
    side = TariffSide(mode=MODE_LINEAR, multiplier=1.25, adder=0.9)
    result = side.apply(None, SPOT)
    assert result.values == pytest.approx((1.40, 1.90))


def test_passthrough_leaves_the_series_untouched():
    """For sources that already bake in VAT and fees themselves."""
    side = TariffSide(mode=MODE_PASSTHROUGH, multiplier=99, adder=99)
    assert side.apply(None, SPOT).values == SPOT.values


def test_buy_and_sell_are_composed_independently():
    tariff = Tariff.from_dict(
        {
            "buy": {"mode": MODE_LINEAR, "multiplier": 1.25, "adder": 0.9},
            "sell": {"mode": MODE_LINEAR, "multiplier": 1.0, "adder": 0.05},
        }
    )
    buy, sell = tariff.compose(None, SPOT)
    assert buy.values == pytest.approx((1.40, 1.90))
    assert sell.values == pytest.approx((0.45, 0.85))


def test_empty_spot_yields_empty_sides():
    """No prices must not become a plan built on zeros."""
    buy, sell = Tariff.from_dict({}).compose(None, Series.empty())
    assert not buy
    assert not sell


def test_unknown_mode_is_rejected():
    with pytest.raises(TariffError, match="Unknown tariff mode"):
        TariffSide.from_dict({"mode": "magic"})


def test_template_mode_without_a_template_is_rejected():
    side = TariffSide(mode="template", template=None)
    with pytest.raises(TariffError, match="no template"):
        side.apply(None, SPOT)


def test_default_tariff_is_a_no_op():
    """An unconfigured tariff must pass spot through unchanged, not zero it."""
    buy, sell = Tariff.from_dict({}).compose(None, SPOT)
    assert buy.values == SPOT.values
    assert sell.values == SPOT.values

"""The network tariff status sensor and the untracked-peak repair."""

from __future__ import annotations

from types import SimpleNamespace

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.emhass_companion import _report_peak_tracking
from custom_components.emhass_companion.const import (
    DOMAIN,
    ISSUE_PEAK_TRACKING_OFF,
    NETWORK_STATUS_ACTIVE,
    NETWORK_STATUS_NOT_CONFIGURED,
    NETWORK_STATUS_NOT_PRICED,
    NETWORK_STATUS_PEAK_TRACKING_OFF,
    NETWORK_STATUS_PROFILE_ERROR,
)
from custom_components.emhass_companion.sensor import NetworkTariffStatusSensor


def _coordinator(key="", calendar=None, trackers=(), rate=1.0):
    return SimpleNamespace(
        config=SimpleNamespace(network=SimpleNamespace(key=key)),
        network_calendar=calendar,
        peak_trackers=list(trackers),
        holiday_cache=SimpleNamespace(degraded=False),
        demand_charge_pricing=lambda now: SimpleNamespace(
            effective_rate_per_kw=rate, reason=None if rate else "old"
        ),
    )


def _status(coordinator) -> str:
    sensor = object.__new__(NetworkTariffStatusSensor)
    sensor.coordinator = coordinator
    return sensor.native_value


def _calendar(demand=0):
    return SimpleNamespace(bands=[1], demand_charges=[1] * demand)


def test_status_walks_through_every_state():
    assert _status(_coordinator()) == NETWORK_STATUS_NOT_CONFIGURED
    assert _status(_coordinator("network/x")) == NETWORK_STATUS_PROFILE_ERROR
    assert (
        _status(_coordinator("network/x", _calendar(demand=1))) == NETWORK_STATUS_PEAK_TRACKING_OFF
    )
    assert _status(_coordinator("network/x", _calendar(demand=1), [object()])) == (
        NETWORK_STATUS_ACTIVE
    )
    assert _status(_coordinator("network/x", _calendar())) == NETWORK_STATUS_ACTIVE
    unpriced = _coordinator("network/x", _calendar(demand=1), [object()], rate=None)
    assert _status(unpriced) == NETWORK_STATUS_NOT_PRICED


async def test_repair_raised_only_for_an_untracked_demand_charge(hass: HomeAssistant) -> None:
    registry = ir.async_get(hass)

    _report_peak_tracking(hass, _coordinator("network/x", _calendar(demand=1)))
    assert registry.async_get_issue(DOMAIN, ISSUE_PEAK_TRACKING_OFF) is not None

    _report_peak_tracking(hass, _coordinator("network/x", _calendar(demand=1), [object()]))
    assert registry.async_get_issue(DOMAIN, ISSUE_PEAK_TRACKING_OFF) is None

    _report_peak_tracking(hass, _coordinator("network/x", _calendar(demand=1)))
    _report_peak_tracking(hass, _coordinator("network/x", _calendar()))
    assert registry.async_get_issue(DOMAIN, ISSUE_PEAK_TRACKING_OFF) is None

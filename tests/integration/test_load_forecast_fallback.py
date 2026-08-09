"""What EMHASS Companion sends instead of "mlforecaster" before it is trained.

EMHASS's own "typical" load forecast method, for a sensor mlforecaster has
never been fit against, reads whatever a stale or mismatched reference pickle
happens to hold -- not this household's own consumption, and often far off
from it. Its "naive" method describes the household properly (it repeats the
sensor's actual last day), but reaching it means sending no load series, which
is the one request shape that makes EMHASS fetch a day of history from Home
Assistant itself, on terms out of this integration's hands.

So ``_load_forecast_fallback`` builds the series here instead, out of whatever
real readings the sensor already has (or, lacking any at all, its single
current live reading), repeated forward to cover the horizon -- which is what
"naive" would have computed from the same recorder anyway. EMHASS's "typical"
is left as the last rung, for a sensor that does not exist or has never
reported. See test_ml_forecaster_fallback.py for how this plugs into the
mlforecaster readiness check that calls it.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import (
    CONF_LOAD,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    DOMAIN,
)
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.models import Point, Series
from custom_components.emhass_companion.profiles import async_load_profiles

LOAD_SENSOR = "sensor.house_load_without_emhass_consumers"

_NOW = dt_util.utcnow()
_HORIZON_END = _NOW + timedelta(hours=24)


async def _coordinator(hass: HomeAssistant) -> EmhassCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        options={
            CONF_LOAD: {
                CONF_PROFILE: "load/sensor",
                CONF_PROFILE_OPTIONS: {"entity": LOAD_SENSOR, "method": "mlforecaster"},
            },
        },
    )
    entry.add_to_hass(hass)

    loads = DeferrableRegistry(hass, entry)
    loads.sync()

    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)
    coordinator.profiles = (await async_load_profiles(hass)).profiles
    return coordinator


async def test_no_sensor_falls_all_the_way_back_to_typical(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)

    method, bootstrap = await coordinator._load_forecast_fallback(None, _NOW, _HORIZON_END)

    assert method == "typical"
    assert bootstrap is None


async def test_a_full_day_of_history_is_still_sent_as_our_own_series(
    hass: HomeAssistant,
) -> None:
    """Ample history must not hand the job back to EMHASS's "naive".

    Naive means sending no load series, and a request with no load series
    sends naive-mpc-optim back to Home Assistant for the history itself --
    including PV and battery sensor names out of EMHASS's own config that this
    integration never sets. Where those do not exist, EMHASS answers with a
    bare 500 and the fallback fails exactly when it is needed.
    """
    day = Series(
        [Point(_NOW - timedelta(hours=24) + timedelta(minutes=15 * i), 400.0) for i in range(96)]
    )

    with (
        patch.object(coordinator := await _coordinator(hass), "_has_history_since") as has_history,
        patch.object(coordinator, "_recent_load_series", AsyncMock(return_value=day)),
    ):
        method, bootstrap = await coordinator._load_forecast_fallback(
            LOAD_SENSOR, _NOW, _HORIZON_END
        )

    assert method == "list"
    assert bootstrap is not None
    assert bootstrap.covers(_HORIZON_END)
    # Not consulted at all any more: what the sensor has is what gets repeated
    # forward, however much or little of it there is.
    has_history.assert_not_called()


async def test_partial_history_is_repeated_forward_as_a_bootstrap_series(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(hass)
    partial = Series([Point(_NOW - timedelta(hours=2), 500.0), Point(_NOW, 700.0)])

    with (
        patch.object(coordinator, "_has_history_since", AsyncMock(return_value=False)),
        patch.object(coordinator, "_recent_load_series", AsyncMock(return_value=partial)),
    ):
        method, bootstrap = await coordinator._load_forecast_fallback(
            LOAD_SENSOR, _NOW, _HORIZON_END
        )

    assert method == "list"
    assert bootstrap is not None
    assert bootstrap.covers(_HORIZON_END)
    # The real, measured points are kept verbatim -- only the gap past them is filled in.
    assert bootstrap.value_at(_NOW) == 700.0


async def test_zero_history_bootstraps_from_the_live_reading(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    hass.states.async_set(LOAD_SENSOR, "842")

    with (
        patch.object(coordinator, "_has_history_since", AsyncMock(return_value=False)),
        patch.object(coordinator, "_recent_load_series", AsyncMock(return_value=Series.empty())),
    ):
        method, bootstrap = await coordinator._load_forecast_fallback(
            LOAD_SENSOR, _NOW, _HORIZON_END
        )

    assert method == "list"
    assert bootstrap is not None
    assert bootstrap.covers(_HORIZON_END)
    # No shape to go on at all -- the live reading is held flat across the horizon.
    assert all(point.value == pytest.approx(842.0) for point in bootstrap)


async def test_no_history_and_no_live_reading_falls_back_to_typical(
    hass: HomeAssistant,
) -> None:
    coordinator = await _coordinator(hass)
    hass.states.async_set(LOAD_SENSOR, STATE_UNAVAILABLE)

    with (
        patch.object(coordinator, "_has_history_since", AsyncMock(return_value=False)),
        patch.object(coordinator, "_recent_load_series", AsyncMock(return_value=Series.empty())),
    ):
        method, bootstrap = await coordinator._load_forecast_fallback(
            LOAD_SENSOR, _NOW, _HORIZON_END
        )

    assert method == "typical"
    assert bootstrap is None

"""Detecting the optimisation time step from a chosen price profile.

Nord Pool has published 15-minute prices since October 2025; other sources
are still hourly. `optimization_time_step` was previously a fixed dropdown
with no connection to whatever the user's actual price source provides --
this is the detection half of fixing that.
"""

from __future__ import annotations

import json
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.util.yaml import load_yaml

from custom_components.emhass_companion.config_flow import _async_detect_time_step
from custom_components.emhass_companion.const import CONF_PROFILE, CONF_PROFILE_OPTIONS
from custom_components.emhass_companion.profiles import BUILTIN_ROOT, async_load_profiles
from custom_components.emhass_companion.profiles.schema import Profile, validate_document

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _builtin(key: str) -> Profile:
    kind, _, stem = key.partition("/")
    path = BUILTIN_ROOT / kind / f"{stem}.yaml"
    document = validate_document(load_yaml(str(path)))
    return Profile(key=key, path=str(path), kind=kind, name=document["name"], document=document)


async def test_detects_15_minute_nordpool_resolution(hass: HomeAssistant) -> None:
    data = json.loads((FIXTURES / "nordpool_custom.json").read_text(encoding="utf-8"))
    hass.states.async_set(
        "sensor.nordpool",
        "0.84",
        {"raw_today": data["raw_today"], "raw_tomorrow": data["raw_tomorrow"]},
    )
    profiles = {"price/nordpool_custom": _builtin("price/nordpool_custom")}

    minutes = await _async_detect_time_step(
        hass,
        profiles,
        {
            CONF_PROFILE: "price/nordpool_custom",
            CONF_PROFILE_OPTIONS: {"entity": "sensor.nordpool"},
        },
    )

    assert minutes == 15


async def test_detects_hourly_tibber_resolution(hass: HomeAssistant) -> None:
    from homeassistant.core import SupportsResponse

    data = json.loads((FIXTURES / "tibber.json").read_text(encoding="utf-8"))

    async def _handler(call):
        return data

    hass.services.async_register(
        "tibber", "get_prices", _handler, supports_response=SupportsResponse.ONLY
    )
    profiles = {"price/tibber": _builtin("price/tibber")}

    minutes = await _async_detect_time_step(
        hass, profiles, {CONF_PROFILE: "price/tibber", CONF_PROFILE_OPTIONS: {"home": "Hemma"}}
    )

    assert minutes == 60


async def test_no_price_profile_chosen_yet_detects_nothing(hass: HomeAssistant) -> None:
    """Reachable if a user somehow lands on grid before finishing price setup."""
    minutes = await _async_detect_time_step(hass, {}, {})
    assert minutes is None


async def test_a_settings_only_profile_detects_nothing(hass: HomeAssistant) -> None:
    """The fixed-tariff profile fetches no series; there is nothing to measure."""
    profiles = {"price/fixed": _builtin("price/fixed")}

    minutes = await _async_detect_time_step(
        hass, profiles, {CONF_PROFILE: "price/fixed", CONF_PROFILE_OPTIONS: {}}
    )

    assert minutes is None


async def test_a_profile_that_fails_to_resolve_detects_nothing(
    hass: HomeAssistant,
) -> None:
    """A missing entity must not abort setup -- just skip the smart default."""
    profiles = {"price/nordpool_custom": _builtin("price/nordpool_custom")}

    minutes = await _async_detect_time_step(
        hass,
        profiles,
        {
            CONF_PROFILE: "price/nordpool_custom",
            CONF_PROFILE_OPTIONS: {"entity": "sensor.does_not_exist"},
        },
    )

    assert minutes is None


async def test_all_builtin_price_profiles_load_for_detection(hass: HomeAssistant) -> None:
    """Sanity check that the real loader (not just the test helper) agrees."""
    result = await async_load_profiles(hass)
    assert not result.errors
    assert "price/nordpool_custom" in result.profiles
    assert "price/tibber" in result.profiles

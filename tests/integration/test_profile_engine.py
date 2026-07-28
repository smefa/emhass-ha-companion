"""Profile resolution against a live Home Assistant state machine.

Covers the parts of the engine that need templates, entity states, or service
calls, using the same recorded fixtures as the platform-independent tests.
"""

from __future__ import annotations

import json
from pathlib import Path

from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.util.yaml import load_yaml
import pytest

from custom_components.emhass_companion.profiles import BUILTIN_ROOT, Profile
from custom_components.emhass_companion.profiles.engine import async_resolve_series, render
from custom_components.emhass_companion.profiles.schema import ProfileError, validate_document

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _builtin(key: str) -> Profile:
    kind, _, stem = key.partition("/")
    path = BUILTIN_ROOT / kind / f"{stem}.yaml"
    document = validate_document(load_yaml(str(path)))
    return Profile(key=key, path=str(path), kind=kind, name=document["name"], document=document)


# --- attribute sources -------------------------------------------------------


async def test_nordpool_custom_profile_resolves(hass: HomeAssistant) -> None:
    data = _fixture("nordpool_custom.json")
    hass.states.async_set(
        "sensor.nordpool",
        "0.84",
        {"raw_today": data["raw_today"], "raw_tomorrow": data["raw_tomorrow"]},
    )

    series = await async_resolve_series(
        hass, _builtin("price/nordpool_custom"), {"entity": "sensor.nordpool"}
    )

    assert len(series) == len(data["raw_today"]) + len(data["raw_tomorrow"])


async def test_missing_attribute_is_skipped_not_fatal(hass: HomeAssistant) -> None:
    """raw_tomorrow is simply absent until the market publishes."""
    data = _fixture("nordpool_custom.json")
    hass.states.async_set("sensor.nordpool", "0.84", {"raw_today": data["raw_today"]})

    series = await async_resolve_series(
        hass, _builtin("price/nordpool_custom"), {"entity": "sensor.nordpool"}
    )

    assert len(series) == len(data["raw_today"])


async def test_missing_entity_is_reported_clearly(hass: HomeAssistant) -> None:
    with pytest.raises(ProfileError, match=r"sensor\.nope"):
        await async_resolve_series(
            hass, _builtin("price/nordpool_custom"), {"entity": "sensor.nope"}
        )


async def test_solcast_profile_sums_multiple_entities(hass: HomeAssistant) -> None:
    """Today and tomorrow are separate entities whose series concatenate."""
    records = _fixture("solcast.json")["detailedForecast"]
    hass.states.async_set("sensor.today", "50", {"detailedForecast": records})
    hass.states.async_set("sensor.tomorrow", "40", {"detailedForecast": []})

    series = await async_resolve_series(
        hass,
        _builtin("pv/solcast"),
        {
            "entities": ["sensor.today", "sensor.tomorrow"],
            "estimate": "pv_estimate",
        },
    )

    assert len(series) == len(records)
    # kW in the source, W in the result.
    assert series.values[0] == pytest.approx(7374.9)


async def test_solcast_estimate_option_selects_the_field(hass: HomeAssistant) -> None:
    records = _fixture("solcast.json")["detailedForecast"]
    hass.states.async_set("sensor.today", "50", {"detailedForecast": records})

    p10 = await async_resolve_series(
        hass, _builtin("pv/solcast"), {"entities": ["sensor.today"], "estimate": "pv_estimate10"}
    )
    p90 = await async_resolve_series(
        hass, _builtin("pv/solcast"), {"entities": ["sensor.today"], "estimate": "pv_estimate90"}
    )

    assert p10.values[0] < p90.values[0]


# --- service sources ---------------------------------------------------------


async def test_service_source_iterates_days_and_digs_the_response(
    hass: HomeAssistant,
) -> None:
    calls: list[dict] = []

    async def _handler(call):
        calls.append(dict(call.data))
        return {
            "SE3": [
                {"start": f"{call.data['date']}T00:00:00+00:00", "price": 400.0},
                {"start": f"{call.data['date']}T01:00:00+00:00", "price": 500.0},
            ]
        }

    hass.services.async_register(
        "nordpool",
        "get_prices_for_date",
        _handler,
        supports_response=SupportsResponse.ONLY,
    )

    series = await async_resolve_series(
        hass, _builtin("price/nordpool_core"), {"config_entry": "abc", "area": "SE3"}
    )

    # Today and tomorrow, two records each.
    assert len(calls) == 2
    assert len(series) == 4
    # currency/MWh in, currency/kWh out.
    assert series.values[0] == pytest.approx(0.4)


async def test_service_source_tolerates_no_data_for_tomorrow(
    hass: HomeAssistant,
) -> None:
    """Tomorrow's prices do not exist for most of the day."""

    async def _handler(call):
        if call.data["date"].endswith(("29", "30", "31", "01")) or len(call.data) > 99:
            raise ValueError("no data")
        return {"SE3": [{"start": "2026-07-28T00:00:00+00:00", "price": 400.0}]}

    hass.services.async_register(
        "nordpool", "get_prices_for_date", _handler, supports_response=SupportsResponse.ONLY
    )

    # Whatever today is, the second (future) call may fail without breaking the run.
    series = await async_resolve_series(
        hass, _builtin("price/nordpool_core"), {"config_entry": "abc", "area": "SE3"}
    )
    assert len(series) >= 1


async def test_unknown_response_path_lists_the_available_keys(
    hass: HomeAssistant,
) -> None:
    async def _handler(call):
        return {"SE3": []}

    hass.services.async_register(
        "nordpool", "get_prices_for_date", _handler, supports_response=SupportsResponse.ONLY
    )

    with pytest.raises(ProfileError, match="SE3"):
        await async_resolve_series(
            hass, _builtin("price/nordpool_core"), {"config_entry": "abc", "area": "NO1"}
        )


# --- template escape hatch ---------------------------------------------------


async def test_template_source_resolves(hass: HomeAssistant) -> None:
    """The escape hatch mirrors what users already write in rest_command."""
    hass.states.async_set(
        "sensor.custom",
        "1",
        {"prices": [{"t": "2026-07-28T10:00:00+00:00", "p": 1.5}]},
    )
    profile = Profile(
        key="price/custom",
        path="custom.yaml",
        kind="price",
        name="Custom",
        document=validate_document(
            {
                "name": "Custom",
                "kind": "price",
                "version": 1,
                "source": {
                    "type": "template",
                    "value": "{{ state_attr('sensor.custom','prices') }}",
                },
                "series": {"time": "t", "value": "p"},
            }
        ),
    )

    series = await async_resolve_series(hass, profile, {})

    assert series.values == (1.5,)


async def test_template_returning_a_scalar_is_reported(hass: HomeAssistant) -> None:
    profile = Profile(
        key="price/bad",
        path="bad.yaml",
        kind="price",
        name="Bad",
        document=validate_document(
            {
                "name": "Bad",
                "kind": "price",
                "version": 1,
                "source": {"type": "template", "value": "{{ 42 }}"},
                "series": {"time": "t", "value": "p"},
            }
        ),
    )

    with pytest.raises(ProfileError, match="expected a list"):
        await async_resolve_series(hass, profile, {})


# --- settings rendering ------------------------------------------------------


def test_render_keeps_non_string_leaves_typed(hass: HomeAssistant) -> None:
    """Numbers must reach EMHASS as numbers, not strings."""
    rendered = render(
        hass,
        {"a": "{{ options.n }}", "b": 5, "c": "plain", "d": ["{{ options.n }}"]},
        {"options": {"n": 42}},
    )
    assert rendered == {"a": 42, "b": 5, "c": "plain", "d": [42]}

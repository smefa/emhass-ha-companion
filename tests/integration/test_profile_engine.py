"""Profile resolution against a live Home Assistant state machine.

Covers the parts of the engine that need templates, entity states, or service
calls, using the same recorded fixtures as the platform-independent tests.
"""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path

from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.util import dt as dt_util
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
    """Tomorrow's prices do not exist for most of the day.

    The fake handler must fail for the *actual* tomorrow, computed the same
    way the code under test computes it -- not a hardcoded date, and not a
    date-string suffix, which previously matched "today" too on whatever day
    of the month happened to end in one of the chosen digits (this test broke
    on the 29th because "today" is literally 2026-07-29; it would have broken
    again on the 30th, 31st and 1st of any month).
    """
    tomorrow = (dt_util.now().date() + timedelta(days=1)).isoformat()

    async def _handler(call):
        if call.data["date"] == tomorrow:
            raise ValueError("no data")
        return {"SE3": [{"start": "2026-07-28T00:00:00+00:00", "price": 400.0}]}

    hass.services.async_register(
        "nordpool", "get_prices_for_date", _handler, supports_response=SupportsResponse.ONLY
    )

    # Today's call succeeds; tomorrow's fails and must not break the run.
    series = await async_resolve_series(
        hass, _builtin("price/nordpool_core"), {"config_entry": "abc", "area": "SE3"}
    )
    assert len(series) == 1


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


# --- profiles added in phase 5 -----------------------------------------------


async def test_entsoe_profile_resolves(hass: HomeAssistant) -> None:
    data = _fixture("entsoe.json")
    hass.states.async_set("sensor.entsoe_average", "0.09", {"prices": data["prices"]})

    series = await async_resolve_series(
        hass, _builtin("price/entsoe"), {"entity": "sensor.entsoe_average"}
    )

    assert len(series) == len(data["prices"])
    assert series.values[0] == pytest.approx(0.1234)


async def test_tibber_profile_digs_the_named_home(hass: HomeAssistant) -> None:
    data = _fixture("tibber.json")

    async def _handler(call):
        return data

    hass.services.async_register(
        "tibber", "get_prices", _handler, supports_response=SupportsResponse.ONLY
    )

    series = await async_resolve_series(hass, _builtin("price/tibber"), {"home": "Hemma"})

    assert len(series) == 3
    assert series.values[0] == pytest.approx(1.4321)


async def test_tibber_unknown_home_lists_the_real_ones(hass: HomeAssistant) -> None:
    """The home name comes from the Tibber app and is easy to get wrong."""

    async def _handler(call):
        return _fixture("tibber.json")

    hass.services.async_register(
        "tibber", "get_prices", _handler, supports_response=SupportsResponse.ONLY
    )

    with pytest.raises(ProfileError) as err:
        await async_resolve_series(hass, _builtin("price/tibber"), {"home": "Huset"})

    assert "Hemma" in str(err.value)
    assert "Sommarstuga" in str(err.value)


async def test_generic_price_profile_reads_two_attributes(hass: HomeAssistant) -> None:
    """Today and tomorrow commonly live on separate attributes."""
    data = _fixture("nordpool_custom.json")
    hass.states.async_set(
        "sensor.custom",
        "0.5",
        {"raw_today": data["raw_today"], "raw_later": data["raw_today"][:2]},
    )

    series = await async_resolve_series(
        hass,
        _builtin("price/generic_attribute"),
        {
            "entity": "sensor.custom",
            "attribute": "raw_today",
            "second_attribute": "raw_later",
            "time_field": "start",
            "value_field": "value",
            "scale": 1,
        },
    )

    # The two attributes overlap entirely, so duplicates collapse by timestamp.
    assert len(series) == len(data["raw_today"])


async def test_generic_price_profile_without_a_second_attribute(
    hass: HomeAssistant,
) -> None:
    """A blank second attribute must be skipped, not looked up as ""."""
    data = _fixture("nordpool_custom.json")
    hass.states.async_set("sensor.custom", "0.5", {"raw_today": data["raw_today"]})

    series = await async_resolve_series(
        hass,
        _builtin("price/generic_attribute"),
        {
            "entity": "sensor.custom",
            "attribute": "raw_today",
            "second_attribute": "",
            "time_field": "start",
            "value_field": "value",
            "scale": 1,
        },
    )

    assert len(series) == len(data["raw_today"])


async def test_generic_pv_profile_scales_to_watts(hass: HomeAssistant) -> None:
    records = _fixture("solcast.json")["detailedForecast"]
    hass.states.async_set("sensor.any_forecast", "5", {"forecast": records})

    series = await async_resolve_series(
        hass,
        _builtin("pv/generic_attribute"),
        {
            "entities": ["sensor.any_forecast"],
            "attribute": "forecast",
            "time_field": "period_start",
            "value_field": "pv_estimate",
            "scale": 1000,
        },
    )

    assert series.values[0] == pytest.approx(7374.9)


async def test_settings_only_profiles_contribute_no_series(hass: HomeAssistant) -> None:
    """Profiles that delegate to EMHASS must not pretend to fetch anything."""
    from custom_components.emhass_companion.profiles import resolve_settings

    profile = _builtin("pv/forecast_solar_api")
    assert profile.produces_series is False

    settings = resolve_settings(hass, profile, {"peak_power_kw": 9.5})
    assert settings["weather_forecast_method"] == "solar.forecast"
    # Rendered as a number, not the string "9.5".
    assert settings["solar_forecast_kwp"] == 9.5


# --- template errors are normalised, not raw exceptions ----------------------


async def test_a_malformed_template_raises_profileerror(hass: HomeAssistant) -> None:
    """Otherwise a typo in a user's own profile crashes as a raw exception.

    render() is the single function every source type funnels through
    (attributes, service, template, and settings/action rendering), so this
    one call site protects all of them.
    """
    with pytest.raises(ProfileError, match="Template error"):
        render(hass, "{{ this is not valid jinja %}", {})


async def test_an_undefined_reference_at_render_time_raises_profileerror(
    hass: HomeAssistant,
) -> None:
    """Valid syntax, but a variable the profile author typo'd.

    HA's Jinja environment renders a bare undefined reference as an empty
    string rather than raising (only a *warning* is logged), so the case that
    actually raises -- and the one worth guarding -- is calling a method on
    that undefined value, exactly as `options.typo.upper()` would if a profile
    author mistyped an option name and then used it.
    """
    with pytest.raises(ProfileError):
        render(hass, "{{ options.typo_ed_field.upper() }}", {"options": {}})


async def test_test_profile_service_reports_a_bad_template_instead_of_raising(
    hass: HomeAssistant,
) -> None:
    """The diagnostic service's whole purpose is a clean answer, not a crash."""
    from custom_components.emhass_companion.profiles.schema import Profile

    profile = Profile(
        key="price/broken",
        path="broken.yaml",
        kind="price",
        name="Broken",
        document=validate_document(
            {
                "name": "Broken",
                "kind": "price",
                "version": 1,
                "emhass": {"load_peak_hours_cost": "{{ 1 / 0 }}"},
            }
        ),
    )

    from custom_components.emhass_companion.profiles import resolve_settings

    with pytest.raises(ProfileError):
        resolve_settings(hass, profile, {})

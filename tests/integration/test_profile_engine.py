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

from custom_components.emhass_companion.config_flow import _suggested_entities
from custom_components.emhass_companion.profiles import BUILTIN_ROOT, Profile
from custom_components.emhass_companion.profiles.engine import (
    action_variables,
    async_resolve_series,
    convert_curtail_power,
    convert_power,
    render,
)
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


# --- typical household load ---------------------------------------------------


async def test_typical_household_profile_resolves_via_its_own_service(
    hass: HomeAssistant,
) -> None:
    """End-to-end through the integration's own registered service, not a mock.

    Exercises the whole path a real optimisation run takes: the profile's
    `average_w` option is rendered into the service call, the service (backed
    by typical_load.typical_day_records) computes two days of records, and the
    engine digs them out via `response_path: forecast`.
    """
    from custom_components.emhass_companion.services import async_register_services

    async_register_services(hass)

    series = await async_resolve_series(hass, _builtin("load/emhass_native"), {"average_w": 1200})

    # for_days: [0, 1] -- today and tomorrow, 48 half-hour records each.
    assert len(series) == 96
    assert series.step().total_seconds() == 30 * 60
    assert all(value >= 0 for value in series.values)


async def test_typical_household_profile_scales_with_average_w(hass: HomeAssistant) -> None:
    """Doubling the declared average must double the whole forecast."""
    from custom_components.emhass_companion.services import async_register_services

    async_register_services(hass)

    low = await async_resolve_series(hass, _builtin("load/emhass_native"), {"average_w": 600})
    high = await async_resolve_series(hass, _builtin("load/emhass_native"), {"average_w": 1200})

    assert sum(high.values) == pytest.approx(2 * sum(low.values), rel=0.01)


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


# --- inverter power conversion -----------------------------------------------
#
# The unit a profile declares is the difference between 1500 W and 1500 % of
# rated. Each case here is a hundred-fold error delivered to a real battery if
# the conversion regresses.


def _control_profile(control: dict) -> Profile:
    document = validate_document(
        {
            "name": "Test inverter",
            "kind": "inverter",
            "version": 2,
            "control": control,
            "actions": {"self_consume": [{"service": "select.select_option"}]},
        }
    )
    return Profile(
        key="inverter/test", path="<test>", kind="inverter", name="Test inverter", document=document
    )


@pytest.mark.parametrize(
    ("control", "action", "power_w", "expected"),
    [
        # Watts, magnitude only: the direction is a mode select's job.
        ({}, "force_charge", -1500, 1500),
        ({}, "force_discharge", 1500, 1500),
        # Signed, EMHASS's convention: positive is discharge.
        ({"signed": True}, "force_charge", -1500, -1500),
        ({"signed": True}, "force_discharge", 1500, 1500),
        # Hardware that calls charging positive instead.
        ({"signed": True, "invert_sign": True}, "force_charge", -1500, 1500),
        # kW, for Sigenergy and Fox ESS.
        ({"power_unit": "kw", "round_to": 0.001}, "force_charge", -1500, 1.5),
        # Percent of rated, for GoodWe's eco_mode_power.
        (
            {"power_unit": "percent_of_rated", "rated_power_w": 10000},
            "force_charge",
            -1500,
            15,
        ),
        # Percent in hundredths, for Fronius SunSpec.
        (
            {"power_unit": "percent_hundredths", "rated_power_w": 10000},
            "force_charge",
            -1500,
            1500,
        ),
        # A charge boost covers conversion loss on the way to the cells.
        ({"charge_boost": 1.03}, "force_charge", -1000, 1030),
        # ...and applies to charging only.
        ({"charge_boost": 1.03}, "force_discharge", 1000, 1000),
        # Idle is zero however the profile is configured.
        ({"signed": True}, "idle", 0, 0),
    ],
)
def test_power_converts_to_the_unit_the_profile_declares(
    hass: HomeAssistant, control: dict, action: str, power_w: float, expected: float
) -> None:
    profile = _control_profile(control)
    assert convert_power(hass, profile, {}, action, power_w) == pytest.approx(expected)


def test_a_percentage_is_clamped_to_the_inverter_rating(hass: HomeAssistant) -> None:
    """A plan that overshoots should be clamped, not rejected by the inverter."""
    profile = _control_profile({"power_unit": "percent_of_rated", "rated_power_w": 5000})
    assert convert_power(hass, profile, {}, "force_charge", -9000) == 100


def test_a_rating_may_be_read_from_the_inverter_itself(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.rated", "8000")
    profile = _control_profile(
        {
            "power_unit": "percent_of_rated",
            "rated_power_w": "{{ states('sensor.rated') | float }}",
        }
    )
    assert convert_power(hass, profile, {}, "force_discharge", 4000) == 50


def test_an_unreadable_rating_is_reported_not_silently_zero(hass: HomeAssistant) -> None:
    profile = _control_profile(
        {"power_unit": "percent_of_rated", "rated_power_w": "{{ states('sensor.absent') }}"}
    )
    with pytest.raises(ProfileError, match="not a number"):
        convert_power(hass, profile, {}, "force_charge", -1000)


def test_actions_receive_both_the_converted_and_the_raw_value(hass: HomeAssistant) -> None:
    """`power_w` stays available for anything the conversion cannot express."""
    profile = _control_profile({"power_unit": "kw", "round_to": 0.001})
    scope = action_variables(hass, profile, {}, "force_charge", power_w=-2500)
    assert scope["power"] == pytest.approx(2.5)
    assert scope["power_w"] == -2500
    assert scope["magnitude_w"] == 2500


# --- curtailment power conversion ---------------------------------------------
#
# Mirrors the power_unit tests above: a curtailment target lives in its own
# `curtail_unit`, which is frequently a different register/unit than the
# battery setpoint on the same inverter.


@pytest.mark.parametrize(
    ("control", "curtail_w", "expected"),
    [
        ({}, 1200, 1200),  # plain watts, the default
        ({"curtail_unit": "kw", "round_to": 0.001}, 1200, 1.2),
        ({"curtail_unit": "percent_of_rated", "rated_power_w": 10000}, 1500, 15),
        ({"curtail_unit": "percent_hundredths", "rated_power_w": 10000}, 1500, 1500),
        # No sign: a curtailment target is always a plain magnitude.
        ({}, -1200, 1200),
    ],
)
def test_curtail_power_converts_to_the_unit_the_profile_declares(
    hass: HomeAssistant, control: dict, curtail_w: float, expected: float
) -> None:
    profile = _control_profile(control)
    assert convert_curtail_power(hass, profile, {}, curtail_w) == pytest.approx(expected)


def test_curtail_power_is_clamped_to_the_inverter_rating(hass: HomeAssistant) -> None:
    profile = _control_profile({"curtail_unit": "percent_of_rated", "rated_power_w": 5000})
    assert convert_curtail_power(hass, profile, {}, 9000) == 100


def test_curtail_w_is_zero_for_actions_curtailment_does_not_apply_to(hass: HomeAssistant) -> None:
    """Computing it unconditionally is how a curtailment profile's
    rated_power_w template failing would break rendering force_charge too."""
    profile = _control_profile({"curtail_unit": "percent_of_rated", "rated_power_w": 5000})
    scope = action_variables(hass, profile, {}, "force_charge", power_w=-1000, curtail_w=99999)
    assert scope["curtail_w"] == 0


def test_curtail_w_is_converted_for_the_curtail_action(hass: HomeAssistant) -> None:
    profile = _control_profile({"curtail_unit": "kw", "round_to": 0.001})
    scope = action_variables(hass, profile, {}, "curtail", curtail_w=1500)
    assert scope["curtail_w"] == pytest.approx(1.5)


# --- setup suggestions -------------------------------------------------------


def test_an_option_is_prefilled_with_a_suggestion_that_exists(hass: HomeAssistant) -> None:
    hass.states.async_set("select.ems_mode", "Self-consumption mode (default)")
    document = validate_document(
        {
            "name": "Test inverter",
            "kind": "inverter",
            "version": 2,
            "options": {
                "mode_select": {
                    "suggest": ["select.not_here", "select.ems_mode"],
                    "selector": {"entity": {"domain": "select"}},
                }
            },
            "actions": {"self_consume": [{"service": "select.select_option"}]},
        }
    )
    profile = Profile(
        key="inverter/test", path="<test>", kind="inverter", name="Test", document=document
    )
    assert _suggested_entities(hass, profile) == {"mode_select": "select.ems_mode"}


def test_an_option_whose_suggestions_all_miss_is_left_blank(hass: HomeAssistant) -> None:
    """A wrong guess must never be smuggled in as a real answer."""
    document = validate_document(
        {
            "name": "Test inverter",
            "kind": "inverter",
            "version": 2,
            "options": {
                "mode_select": {
                    "suggest": ["select.absent"],
                    "selector": {"entity": {"domain": "select"}},
                }
            },
            "actions": {"self_consume": [{"service": "select.select_option"}]},
        }
    )
    profile = Profile(
        key="inverter/test", path="<test>", kind="inverter", name="Test", document=document
    )
    assert _suggested_entities(hass, profile) == {}

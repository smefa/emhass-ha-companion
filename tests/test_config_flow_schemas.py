"""The config flow's schemas must be constructible.

Selectors validate their own configuration on construction, so an out-of-range
`step` or a malformed selector raises only when the step is first rendered --
i.e. in front of a user, halfway through setup. Building every schema here
turns that into a test failure instead.
"""

from __future__ import annotations

from homeassistant.util.yaml import load_yaml
import pytest
import voluptuous as vol

from custom_components.emhass_companion.config_flow import (
    STANDARD_TIME_STEPS,
    _collect_tariff,
    _default_profile_options,
    _load_profile_selector,
    _profile_selector,
    _tariff_side_schema,
    _time_step_options,
    battery_schema,
    grid_schema,
)
from custom_components.emhass_companion.const import (
    CONF_MULTIPLIER,
    CONF_TIME_STEP,
    LOAD_PROFILE_CREATE_SENTINEL,
)
from custom_components.emhass_companion.profiles import BUILTIN_ROOT
from custom_components.emhass_companion.profiles.schema import Profile, validate_document


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_tariff_schema_builds(side):
    assert vol.Schema(_tariff_side_schema(side, {}))


def test_tariff_schema_builds_with_stored_values():
    stored = {"mode": "linear", "multiplier": 1.25, "adder": 0.5375}
    assert vol.Schema(_tariff_side_schema("buy", stored))


# --- export multiplier must never end up at 0 --------------------------------
# (schema-validation coverage for this lives in
# tests/integration/test_config_flow_schemas.py -- validating the full
# _tariff_side_schema dict always touches the template field too, which
# needs a real event loop.)


def test_collect_tariff_coerces_an_explicit_zero_sell_multiplier_to_one():
    """The schema's `min=0` does not reject 0 -- this is the other half of the
    fix, for a 0 actually typed into the field rather than left blank."""
    user_input = {
        "buy_mode": "linear",
        "buy_multiplier": 0,
        "buy_adder": 0.0,
        "sell_mode": "linear",
        "sell_multiplier": 0,
        "sell_adder": 0.0,
    }
    tariff = _collect_tariff(user_input)
    assert tariff["sell"][CONF_MULTIPLIER] == 1.0
    assert tariff["buy"][CONF_MULTIPLIER] == 0  # import is untouched


def test_collect_tariff_leaves_a_nonzero_sell_multiplier_alone():
    user_input = {
        "buy_mode": "linear",
        "sell_mode": "linear",
        "sell_multiplier": 1.25,
    }
    tariff = _collect_tariff(user_input)
    assert tariff["sell"][CONF_MULTIPLIER] == 1.25


def test_collect_tariff_never_persists_a_none_template():
    """A stored `None` (rather than an absent key) reintroduces the bug covered
    in tests/integration/test_config_flow_schemas.py.

    dict.get's fallback only applies when the key is missing entirely; a
    stored None survives every `.get(key, default)` read forever after.
    """
    user_input = {
        "buy_mode": "linear",
        "buy_multiplier": 1.0,
        "buy_adder": 0.0,
        "buy_template": "",  # left blank in the form
        "sell_mode": "linear",
        "sell_multiplier": 1.0,
        "sell_adder": 0.0,
        "sell_template": "",
    }
    tariff = _collect_tariff(user_input)
    assert "template" not in tariff["buy"]
    assert "template" not in tariff["sell"]


def test_battery_schema_builds():
    assert vol.Schema(battery_schema({}))


def test_grid_schema_builds():
    assert vol.Schema(grid_schema({}))


def test_grid_schema_builds_with_a_detected_time_step():
    """The value async_step_grid passes in after detection must be renderable."""
    assert vol.Schema(grid_schema({CONF_TIME_STEP: 15}))


# --- time step: the dropdown offers presets plus whatever was detected -------


def test_time_step_options_include_every_standard_preset():
    options = _time_step_options({})
    assert set(STANDARD_TIME_STEPS) <= set(options)


def test_time_step_options_are_sorted_numerically_not_lexically():
    """Lexical sort would put "5" after "30" -- wrong order in the dropdown."""
    options = _time_step_options({})
    assert options == sorted(options, key=int)


def test_a_detected_value_outside_the_presets_is_added():
    """Nordpool's 15 minutes happens to be a preset; not every source will be."""
    options = _time_step_options({CONF_TIME_STEP: 20})
    assert "20" in options


def test_a_detected_value_already_a_preset_is_not_duplicated():
    options = _time_step_options({CONF_TIME_STEP: 15})
    assert options.count("15") == 1


def test_no_detected_value_still_offers_the_standard_presets():
    assert _time_step_options({}) == sorted(STANDARD_TIME_STEPS, key=int)


# --- time step: the field itself validates and coerces -----------------------


def _validate_time_step(raw):
    schema = vol.Schema(grid_schema({}))
    result = schema(
        {
            "grid_import_max_w": 9000,
            "grid_export_max_w": 9000,
            CONF_TIME_STEP: raw,
            "mpc_interval_minutes": 15,
            "horizon_hours": 24,
            "dayahead_fallback_time": "13:30:00",
        }
    )
    return result[CONF_TIME_STEP]


def test_a_preset_value_is_coerced_to_int():
    assert _validate_time_step("30") == 30
    assert isinstance(_validate_time_step("30"), int)


def test_a_custom_typed_value_is_accepted():
    """The whole point: a resolution not in the preset list must still work."""
    assert _validate_time_step("22") == 22


def test_a_non_numeric_custom_value_is_rejected():
    with pytest.raises(vol.Invalid):
        _validate_time_step("not a number")


def test_an_out_of_range_value_is_rejected():
    with pytest.raises(vol.Invalid):
        _validate_time_step("0")
    with pytest.raises(vol.Invalid):
        _validate_time_step("500")


def test_profile_selector_builds():
    profiles = [
        Profile(key="price/a", path="a.yaml", kind="price", name="A", document={}),
        Profile(key="price/b", path="b.yaml", kind="price", name="B", document={}),
    ]
    assert _profile_selector(profiles)


def test_load_profile_selector_puts_create_first_then_the_fixed_order():
    """See LOAD_PROFILE_ORDER: file-load order is alphabetical, not this."""
    profiles = [
        Profile(
            key="load/forecast_entity", path="a.yaml", kind="load", name="Forecast", document={}
        ),
        Profile(key="load/emhass_native", path="b.yaml", kind="load", name="Typical", document={}),
        Profile(key="load/sensor", path="c.yaml", kind="load", name="Sensor", document={}),
    ]
    options = _load_profile_selector(profiles).config["options"]
    assert [option["value"] for option in options] == [
        LOAD_PROFILE_CREATE_SENTINEL,
        "load/sensor",
        "load/emhass_native",
        "load/forecast_entity",
    ]


def test_load_profile_selector_sorts_unknown_profiles_after_the_fixed_ones():
    """A user-authored load profile has no place in LOAD_PROFILE_ORDER."""
    profiles = [
        Profile(key="load/sensor", path="a.yaml", kind="load", name="Sensor", document={}),
        Profile(key="load/custom", path="b.yaml", kind="load", name="Custom", document={}),
    ]
    options = _load_profile_selector(profiles).config["options"]
    assert [option["value"] for option in options] == [
        LOAD_PROFILE_CREATE_SENTINEL,
        "load/sensor",
        "load/custom",
    ]


def test_default_profile_options_uses_each_options_declared_default():
    profile = Profile(
        key="load/sensor",
        path="sensor.yaml",
        kind="load",
        name="Sensor",
        document={
            "options": {
                "entity": {"name": "Entity", "selector": {"entity": {}}},
                "method": {"name": "Method", "default": "typical", "selector": {"select": {}}},
            }
        },
    )
    assert _default_profile_options(profile, skip={"entity"}) == {"method": "typical"}


@pytest.mark.parametrize(
    "path",
    sorted(BUILTIN_ROOT.glob("*/*.yaml")),
    ids=lambda p: f"{p.parent.name}/{p.stem}",
)
def test_builtin_profile_options_render_as_a_schema(path):
    """Each profile's options must survive being turned into a form."""
    document = validate_document(load_yaml(str(path)))
    profile = Profile(
        key=f"{path.parent.name}/{path.stem}",
        path=str(path),
        kind=document["kind"],
        name=document["name"],
        document=document,
    )
    assert vol.Schema(profile.selector_schema()) is not None

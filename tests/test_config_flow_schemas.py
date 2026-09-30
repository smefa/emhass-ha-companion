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
    BATTERY_ADVANCED_KEYS,
    INVERTER_ADVANCED_KEYS,
    INVERTER_KEYS,
    STANDARD_TIME_STEPS,
    UNTESTED_NOTICE,
    _battery_blob_after_battery_form,
    _battery_blob_after_inverter_form,
    _battery_errors,
    _collect_grid,
    _collect_tariff,
    _default_profile_options,
    _flatten_sections,
    _inverter_errors,
    _inverter_profile_selector,
    _load_profile_selector,
    _nest_suggested,
    _profile_notes,
    _profile_picker_schema,
    _profile_selector,
    _tariff_side_schema,
    _time_step_options,
    _with_advanced,
    battery_schema,
    grid_schema,
    inverter_schema,
)
from custom_components.emhass_companion.const import (
    ADVANCED_SECTION,
    CONF_CAPACITY_COST_PER_KW,
    CONF_CHARGE_POWER_DERATING,
    CONF_COMPUTE_CURTAILMENT,
    CONF_GRID_EXPORT_LIMIT_ENTITY,
    CONF_GRID_IMPORT_LIMIT_ENTITY,
    CONF_HYBRID_INVERTER,
    CONF_INVERTER_AC_OUTPUT_MAX,
    CONF_MULTIPLIER,
    CONF_TIME_STEP,
    LOAD_PROFILE_CREATE_SENTINEL,
    PRICE_ADVANCED_PROFILES,
    PRICE_PROFILE_ORDER,
    PV_ADVANCED_PROFILES,
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


def test_hybrid_on_with_zero_ac_output_is_rejected():
    """The toggle defaults on and the watt field defaults to 0 -- that pair
    would send EMHASS a zero-capacity hybrid and make the plan infeasible."""
    assert _inverter_errors({CONF_HYBRID_INVERTER: True, CONF_INVERTER_AC_OUTPUT_MAX: 0}) == {
        CONF_INVERTER_AC_OUTPUT_MAX: "ac_output_required"
    }
    assert _inverter_errors({CONF_HYBRID_INVERTER: True}) == {
        CONF_INVERTER_AC_OUTPUT_MAX: "ac_output_required"
    }


def test_hybrid_on_with_a_positive_ac_output_is_accepted():
    assert _inverter_errors({CONF_HYBRID_INVERTER: True, CONF_INVERTER_AC_OUTPUT_MAX: 5000}) == {}


def test_hybrid_off_may_leave_ac_output_at_zero():
    """The watt fields are inert when the toggle is off, so 0 is not a plant."""
    assert _inverter_errors({CONF_HYBRID_INVERTER: False, CONF_INVERTER_AC_OUTPUT_MAX: 0}) == {}


def test_battery_schema_builds_with_a_derating_table():
    assert vol.Schema(
        battery_schema({CONF_CHARGE_POWER_DERATING: [[0.5, 0.84], [0.7, 0.42], [0.9, 0.23]]})
    )


def test_a_misordered_derating_table_is_rejected_on_the_form():
    assert _battery_errors(
        {
            CONF_CHARGE_POWER_DERATING: [
                {"soc_pct": 70, "charge_pct": 42},
                {"soc_pct": 50, "charge_pct": 84},
            ],
        }
    ) == {"base": "derating_not_ascending"}


def test_an_empty_derating_table_is_accepted():
    assert _battery_errors({CONF_CHARGE_POWER_DERATING: []}) == {}


def _advanced_keys(schema):
    inner = next(v for k, v in schema.items() if str(k) == ADVANCED_SECTION)
    return inner.schema.schema


def test_grid_schema_keeps_the_limits_and_time_step_basic():
    schema = grid_schema({})
    top = {str(key) for key in schema}
    assert {"grid_import_max_w", "grid_export_max_w", CONF_TIME_STEP} <= top
    assert ADVANCED_SECTION in top
    assert {str(k) for k in _advanced_keys(schema)} == {
        "grid_import_limit_entity",
        "grid_export_limit_entity",
        CONF_COMPUTE_CURTAILMENT,
        "mpc_interval_minutes",
        "horizon_hours",
        "dayahead_fallback_time",
    }


def test_an_unopened_advanced_section_submits_its_defaults():
    result = vol.Schema(grid_schema({}))(
        {"grid_import_max_w": 1, "grid_export_max_w": 1, CONF_TIME_STEP: "15", ADVANCED_SECTION: {}}
    )
    flat = _flatten_sections(result)
    assert ADVANCED_SECTION not in flat
    assert flat["horizon_hours"] and flat["mpc_interval_minutes"]
    assert flat[CONF_COMPUTE_CURTAILMENT] is False


def test_advanced_fields_without_a_default_are_optional():
    """Rule 1: a hidden field must stay valid untouched."""
    for key in _advanced_keys(grid_schema({})):
        assert not isinstance(key, vol.Required) or key.default is not vol.UNDEFINED, key


def test_flatten_sections_merges_and_drops_the_section_key():
    assert _flatten_sections({"a": 1, ADVANCED_SECTION: {"b": 2}}) == {"a": 1, "b": 2}
    assert _flatten_sections({"a": 1}) == {"a": 1}


def test_with_advanced_omits_an_empty_section():
    assert _with_advanced({"a": 1}, {}) == {"a": 1}


def test_nest_suggested_moves_advanced_keys_under_the_section():
    assert _nest_suggested({"a": 1, "b": 2}, {"b"}) == {"a": 1, ADVANCED_SECTION: {"b": 2}}
    assert _nest_suggested({"a": 1}, {"b"}) == {"a": 1}


def test_battery_schema_splits_basic_and_advanced():
    schema = battery_schema({})
    top = {str(k) for k in schema}
    assert {"use_battery", "capacity_wh", "soc_min", "soc_max"} <= top
    assert not top & INVERTER_KEYS
    advanced = {str(k) for k in _advanced_keys(schema)}
    assert advanced == set(BATTERY_ADVANCED_KEYS)
    assert not advanced & top


def test_battery_advanced_fields_are_valid_untouched():
    """Rule 1, and a basic-only save must keep every advanced default."""
    for key in _advanced_keys(battery_schema({})):
        assert not isinstance(key, vol.Required) or key.default is not vol.UNDEFINED, key
    result = vol.Schema(battery_schema({}))({"use_battery": True, ADVANCED_SECTION: {}})
    assert "charge_efficiency" in result[ADVANCED_SECTION]


def test_inverter_schema_splits_basic_and_advanced():
    schema = inverter_schema({})
    top = {str(k) for k in schema}
    assert {CONF_HYBRID_INVERTER, CONF_INVERTER_AC_OUTPUT_MAX, "inverter_ac_input_max_w"} <= top
    advanced = {str(k) for k in _advanced_keys(schema)}
    assert advanced == set(INVERTER_ADVANCED_KEYS)
    assert not advanced & top
    # the live PV sensor is stored outside the battery blob
    assert set(INVERTER_KEYS) == (top - {ADVANCED_SECTION} | advanced) - {"pv_entity"}


def test_inverter_advanced_fields_are_valid_untouched():
    result = vol.Schema(inverter_schema({}))(
        {CONF_INVERTER_AC_OUTPUT_MAX: 5000, ADVANCED_SECTION: {}}
    )
    assert result[ADVANCED_SECTION]["inverter_efficiency_dc_ac"]


def test_the_two_forms_never_claim_the_same_stored_key():
    battery = {str(k) for k in battery_schema({})} | {
        str(k) for k in _advanced_keys(battery_schema({}))
    }
    assert not battery & INVERTER_KEYS


def test_saving_one_form_keeps_the_other_forms_stored_values():
    stored = {"capacity_wh": 10000, CONF_HYBRID_INVERTER: True, CONF_INVERTER_AC_OUTPUT_MAX: 5000}
    after_battery = _battery_blob_after_battery_form(stored, {"capacity_wh": 12000})
    assert after_battery == {
        "capacity_wh": 12000,
        CONF_HYBRID_INVERTER: True,
        CONF_INVERTER_AC_OUTPUT_MAX: 5000,
    }
    after_inverter = _battery_blob_after_inverter_form(
        stored, {CONF_HYBRID_INVERTER: False, CONF_INVERTER_AC_OUTPUT_MAX: 0}
    )
    assert after_inverter == {
        "capacity_wh": 10000,
        CONF_HYBRID_INVERTER: False,
        CONF_INVERTER_AC_OUTPUT_MAX: 0,
    }


def test_a_cleared_field_is_removed_from_the_stored_blob():
    stored = {"capacity_wh": 10000, CONF_CHARGE_POWER_DERATING: [[0.5, 0.8]]}
    assert _battery_blob_after_battery_form(stored, {"capacity_wh": 10000}) == {
        "capacity_wh": 10000
    }


def test_battery_section_opens_on_request():
    schema = battery_schema({}, advanced_open=True)
    section_key = next(k for k in schema if str(k) == ADVANCED_SECTION)
    assert schema[section_key].options["collapsed"] is False


def test_grid_schema_builds():
    assert vol.Schema(grid_schema({}))


def test_grid_schema_builds_with_a_detected_time_step():
    """The value async_step_grid passes in after detection must be renderable."""
    assert vol.Schema(grid_schema({CONF_TIME_STEP: 15}))


def test_the_grid_step_asks_only_about_emhass_curtailment():
    """The Companion's own negative-price rule is gone -- one curtailment
    question, and it is EMHASS's."""
    keys = {str(key) for key in _advanced_keys(grid_schema({}))}
    assert CONF_COMPUTE_CURTAILMENT in keys
    assert "curtail_on_negative_price" not in keys


def test_grid_step_no_longer_asks_for_a_capacity_charge():
    """It moved to the network tariff step ("Flat demand charge (manual)")."""
    assert CONF_CAPACITY_COST_PER_KW not in {str(key) for key in grid_schema({})}
    collected = _collect_grid(
        {"grid_import_max_w": 9000, "grid_export_max_w": 9000, CONF_COMPUTE_CURTAILMENT: True}
    )
    assert CONF_CAPACITY_COST_PER_KW not in collected
    assert collected[CONF_COMPUTE_CURTAILMENT] is True


def test_collect_grid_keeps_the_limit_sensors():
    submitted = {
        "grid_import_max_w": 9000,
        "grid_export_max_w": 9000,
        CONF_COMPUTE_CURTAILMENT: False,
        CONF_GRID_IMPORT_LIMIT_ENTITY: "sensor.phase_balanced_import_limit",
        CONF_GRID_EXPORT_LIMIT_ENTITY: "sensor.export_limit",
    }
    collected = _collect_grid(submitted)
    assert collected[CONF_GRID_IMPORT_LIMIT_ENTITY] == "sensor.phase_balanced_import_limit"
    assert collected[CONF_GRID_EXPORT_LIMIT_ENTITY] == "sensor.export_limit"


def test_a_blank_limit_sensor_is_stored_as_none():
    """_optional_blank drops the key entirely; storing None rather than nothing
    is what makes clearing the field in the options flow actually clear it."""
    collected = _collect_grid(
        {
            "grid_import_max_w": 9000,
            "grid_export_max_w": 9000,
            CONF_COMPUTE_CURTAILMENT: False,
        }
    )
    assert collected[CONF_GRID_IMPORT_LIMIT_ENTITY] is None
    assert collected[CONF_GRID_EXPORT_LIMIT_ENTITY] is None


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
            ADVANCED_SECTION: {},
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


def test_profile_selector_ranks_preferred_profiles_first():
    """See PRICE_PROFILE_ORDER/PV_PROFILE_ORDER: Nord Pool and Solcast lead
    the list rather than sorting alphabetically behind ENTSO-E/Tibber or
    forecast.solar."""
    profiles = [
        Profile(key="price/entsoe", path="a.yaml", kind="price", name="ENTSO-E", document={}),
        Profile(
            key="price/nordpool_custom",
            path="b.yaml",
            kind="price",
            name="Nord Pool (custom)",
            document={},
        ),
        Profile(
            key="price/nordpool_core",
            path="c.yaml",
            kind="price",
            name="Nord Pool (core)",
            document={},
        ),
    ]
    options = _profile_selector(profiles, PRICE_PROFILE_ORDER).config["options"]
    assert [option["value"] for option in options] == [
        "price/nordpool_core",
        "price/nordpool_custom",
        "price/entsoe",
    ]


def test_profile_selector_marks_an_untested_profile_in_its_label():
    """The picker is where the choice is made, so the warning has to be there.

    By the time the profile's notes are on screen the inverter has already
    been picked.
    """
    profiles = [
        Profile(
            key="inverter/tried",
            path="a.yaml",
            kind="inverter",
            name="Validated One",
            document={},
        ),
        Profile(
            key="inverter/untried",
            path="b.yaml",
            kind="inverter",
            name="Unvalidated One",
            document={"untested": True},
        ),
    ]
    labels = [option["label"] for option in _profile_selector(profiles).config["options"]]
    assert labels == ["Validated One", "Unvalidated One — UNTESTED"]


def test_untested_profile_notes_lead_with_the_warning():
    profile = Profile(
        key="inverter/untried",
        path="b.yaml",
        kind="inverter",
        name="Unvalidated One",
        document={"untested": True, "notes": "Pick the EMS mode select."},
    )
    notes = _profile_notes(profile)
    assert notes.startswith(UNTESTED_NOTICE)
    assert notes.endswith("Pick the EMS mode select.")


def test_a_tested_profile_gets_its_notes_unchanged():
    profile = Profile(
        key="inverter/tried",
        path="a.yaml",
        kind="inverter",
        name="Validated One",
        document={"notes": "Pick the EMS mode select."},
    )
    assert _profile_notes(profile) == "Pick the EMS mode select."


def test_inverter_picker_sorts_hardware_alphabetically_with_scripts_last():
    """The script fallback is what you reach for having failed to find your own
    inverter, so it belongs under the list rather than inside it."""
    profiles = [
        Profile(key="inverter/sungrow", path="a", kind="inverter", name="Sungrow", document={}),
        Profile(
            key="inverter/generic_script",
            path="b",
            kind="inverter",
            name="Scripts (works with any inverter)",
            document={},
        ),
        Profile(key="inverter/deye", path="c", kind="inverter", name="Deye", document={}),
    ]
    values = [option["value"] for option in _inverter_profile_selector(profiles).config["options"]]
    assert values == ["inverter/deye", "inverter/sungrow", "inverter/generic_script"]


def test_inverter_picker_still_marks_untested_profiles():
    profiles = [
        Profile(
            key="inverter/deye", path="c", kind="inverter", name="Deye", document={"untested": True}
        ),
    ]
    labels = [option["label"] for option in _inverter_profile_selector(profiles).config["options"]]
    assert labels == ["Deye — UNTESTED"]


def test_profile_selector_with_no_order_keeps_the_given_order():
    profiles = [
        Profile(key="price/b", path="a.yaml", kind="price", name="B", document={}),
        Profile(key="price/a", path="b.yaml", kind="price", name="A", document={}),
    ]
    options = _profile_selector(profiles).config["options"]
    assert [option["value"] for option in options] == ["price/b", "price/a"]


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


def _picker_profiles(kind, *keys):
    return [Profile(key=key, path="x", kind=kind, name=key, document={}) for key in keys]


@pytest.mark.parametrize(
    ("kind", "hidden"),
    [("price", PRICE_ADVANCED_PROFILES), ("pv", PV_ADVANCED_PROFILES)],
)
def test_advanced_picker_profiles_are_real_builtin_profiles(kind, hidden):
    shipped = {f"{kind}/{path.stem}" for path in (BUILTIN_ROOT / kind).glob("*.yaml")}
    assert set(hidden) <= shipped


def test_a_picker_splits_only_while_the_main_list_has_something_in_it():
    everything = _picker_profiles("price", "price/tibber", *PRICE_ADVANCED_PROFILES)
    split = _profile_picker_schema("price", everything)
    assert {str(k) for k in split} == {"profile", ADVANCED_SECTION}

    only_hidden = _picker_profiles("price", *PRICE_ADVANCED_PROFILES)
    flat = _profile_picker_schema("price", only_hidden)
    assert {str(k) for k in flat} == {"profile"}


def test_the_picker_section_opens_for_a_saved_hidden_source():
    profiles = _picker_profiles("pv", "pv/solcast", "pv/none")
    closed = _profile_picker_schema("pv", profiles, current="pv/solcast")
    opened = _profile_picker_schema("pv", profiles, current="pv/none")
    key = next(k for k in closed if str(k) == ADVANCED_SECTION)
    assert closed[key].options["collapsed"] is True
    key = next(k for k in opened if str(k) == ADVANCED_SECTION)
    assert opened[key].options["collapsed"] is False

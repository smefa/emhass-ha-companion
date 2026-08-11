"""Tests for the profile schema and record mapping.

Profiles are data, so they are tested as data: a recorded blob from a real
installation plus the series it should resolve to. That is what lets a profile
for an integration nobody on the project has installed still be verified, and
what makes reviewing a contributed profile mechanical.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

from homeassistant.util.yaml import load_yaml
import pytest

from custom_components.emhass_companion.const import (
    DEFAULT_CONTROL,
    PROFILE_KINDS,
    PROFILE_SCHEMA_VERSION,
)
from custom_components.emhass_companion.profiles import BUILTIN_ROOT, _load_profiles
from custom_components.emhass_companion.profiles.engine import _map_records
from custom_components.emhass_companion.profiles.schema import (
    ProfileError,
    validate_document,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _builtin_paths() -> list[Path]:
    return sorted(BUILTIN_ROOT.glob("*/*.yaml"))


# --- every built-in profile must be valid ------------------------------------


def test_builtin_profiles_exist():
    assert _builtin_paths(), "no built-in profiles were found"


@pytest.mark.parametrize("path", _builtin_paths(), ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_builtin_profile_is_valid(path: Path):
    document = validate_document(load_yaml(str(path)))
    # A profile in the price/ directory declaring kind: pv would load into the
    # wrong list, so the loader rejects it; check the files agree up front.
    assert document["kind"] == path.parent.name
    assert document["kind"] in PROFILE_KINDS


@pytest.mark.parametrize("path", _builtin_paths(), ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_builtin_profile_documents_itself(path: Path):
    """Every built-in carries a description; it is shown during setup."""
    document = validate_document(load_yaml(str(path)))
    assert document.get("description"), f"{path.name} has no description"


# --- schema rejections -------------------------------------------------------


def test_profile_must_be_a_mapping():
    with pytest.raises(ProfileError, match="mapping"):
        validate_document(["not", "a", "mapping"])


def test_unknown_kind_is_rejected():
    with pytest.raises(ProfileError):
        validate_document({"name": "X", "kind": "weather", "version": 1})


def test_future_schema_version_is_rejected():
    """A profile written for a newer schema must not be silently misread."""
    with pytest.raises(ProfileError):
        validate_document({"name": "X", "kind": "price", "version": 99})


def test_profile_contributing_nothing_is_rejected():
    with pytest.raises(ProfileError, match="contributes nothing"):
        validate_document({"name": "X", "kind": "price", "version": 1})


def test_source_missing_required_keys_is_rejected():
    with pytest.raises(ProfileError, match="requires"):
        validate_document(
            {
                "name": "X",
                "kind": "price",
                "version": 1,
                "source": {"type": "attributes", "entity": "sensor.x"},
                "series": {"time": "t", "value": "v"},
            }
        )


def test_non_template_source_requires_a_series_mapping():
    with pytest.raises(ProfileError, match="series"):
        validate_document(
            {
                "name": "X",
                "kind": "price",
                "version": 1,
                "source": {
                    "type": "attributes",
                    "entity": "sensor.x",
                    "attributes": ["raw_today"],
                },
            }
        )


def test_inverter_profile_requires_actions():
    with pytest.raises(ProfileError, match="actions"):
        validate_document({"name": "X", "kind": "inverter", "version": 1})


# --- record mapping against real recorded data -------------------------------


def test_nordpool_custom_records_map_to_prices():
    """The recorded shape is {start, end, value} at 15-minute resolution."""
    data = _fixture("nordpool_custom.json")
    records = data["raw_today"] + data["raw_tomorrow"]

    series = _map_records(records, {"time": "start", "value": "value"})

    assert len(series) == len(records)
    assert series.start == datetime(2026, 7, 28, 8, 0, tzinfo=UTC)  # 10:00 +02:00
    assert series.step().total_seconds() == 15 * 60
    assert all(isinstance(value, float) for value in series.values)


def test_solcast_records_map_to_watts():
    """Solcast reports kW; EMHASS wants W, so the profile scales by 1000."""
    records = _fixture("solcast.json")["detailedForecast"]

    series = _map_records(records, {"time": "period_start", "value": "pv_estimate", "scale": 1000})

    assert len(series) == len(records)
    assert series.step().total_seconds() == 30 * 60
    assert series.values[0] == pytest.approx(7374.9)
    assert max(series.values) > 1000  # watts, not kilowatts


def test_solcast_confidence_level_is_selectable():
    """P10/P50/P90 differ only by which field is read, hence a templated map."""
    records = _fixture("solcast.json")["detailedForecast"]

    p50 = _map_records(records, {"time": "period_start", "value": "pv_estimate"})
    p10 = _map_records(records, {"time": "period_start", "value": "pv_estimate10"})

    assert p10.values[0] < p50.values[0]


def test_missing_time_field_names_the_available_fields():
    """The error has to be actionable: YAML gives no traceback to read."""
    records = _fixture("solcast.json")["detailedForecast"]
    with pytest.raises(ProfileError) as err:
        _map_records(records, {"time": "start", "value": "pv_estimate"})

    assert "period_start" in str(err.value)


def test_non_numeric_value_is_rejected():
    with pytest.raises(ProfileError, match="not numeric"):
        _map_records(
            [{"t": "2026-07-28T10:00:00+00:00", "v": "unavailable"}],
            {"time": "t", "value": "v"},
        )


def test_null_values_are_skipped_not_fatal():
    """A missing settlement period is a gap, not a broken configuration."""
    series = _map_records(
        [
            {"t": "2026-07-28T10:00:00+00:00", "v": 1.0},
            {"t": "2026-07-28T10:15:00+00:00", "v": None},
            {"t": "2026-07-28T10:30:00+00:00", "v": 3.0},
        ],
        {"time": "t", "value": "v"},
    )
    assert series.values == (1.0, 3.0)


def test_overlapping_records_keep_the_later_value():
    """Concatenated "today" and "tomorrow" blocks often share a boundary."""
    series = _map_records(
        [
            {"t": "2026-07-28T10:00:00+00:00", "v": 1.0},
            {"t": "2026-07-28T10:00:00+00:00", "v": 2.0},
        ],
        {"time": "t", "value": "v"},
    )
    assert series.values == (2.0,)


def test_scale_and_offset_apply_in_that_order():
    series = _map_records(
        [{"t": "2026-07-28T10:00:00+00:00", "v": 2.0}],
        {"time": "t", "value": "v", "scale": 10, "offset": 1},
    )
    assert series.values == (21.0,)


# --- the loader --------------------------------------------------------------


def test_loader_finds_every_builtin(tmp_path):
    result = _load_profiles(tmp_path / "nonexistent")
    assert not result.errors
    assert set(result.profiles) == {f"{path.parent.name}/{path.stem}" for path in _builtin_paths()}
    assert all(profile.is_builtin for profile in result.profiles.values())


def test_user_profile_overrides_a_builtin_of_the_same_name(tmp_path):
    """Lets a user fix a broken built-in without waiting for a release."""
    price_dir = tmp_path / "price"
    price_dir.mkdir(parents=True)
    (price_dir / "fixed.yaml").write_text(
        "name: My patched fixed tariff\n"
        "kind: price\n"
        "version: 1\n"
        "description: local override\n"
        "emhass:\n"
        "  load_cost_forecast_method: hp_hc_periods\n",
        encoding="utf-8",
    )

    result = _load_profiles(tmp_path)

    profile = result.profiles["price/fixed"]
    assert profile.name == "My patched fixed tariff"
    assert profile.is_builtin is False


def test_a_malformed_profile_does_not_prevent_the_others_loading(tmp_path):
    """A broken profile costs the user that profile, not the integration."""
    price_dir = tmp_path / "price"
    price_dir.mkdir(parents=True)
    (price_dir / "broken.yaml").write_text("name: Broken\nkind: price\n", encoding="utf-8")

    result = _load_profiles(tmp_path)

    assert "price/broken" not in result.profiles
    assert len(result.errors) == 1
    assert "broken.yaml" in next(iter(result.errors))
    # Everything shipped still loaded.
    assert "price/fixed" in result.profiles


def test_profile_in_the_wrong_directory_is_reported(tmp_path):
    pv_dir = tmp_path / "pv"
    pv_dir.mkdir(parents=True)
    (pv_dir / "confused.yaml").write_text(
        "name: Confused\nkind: price\nversion: 1\nemhass: {}\n", encoding="utf-8"
    )

    result = _load_profiles(tmp_path)

    assert "pv/confused" not in result.profiles
    assert "price" in next(iter(result.errors.values()))


def test_unparseable_yaml_is_reported_not_raised(tmp_path):
    price_dir = tmp_path / "price"
    price_dir.mkdir(parents=True)
    (price_dir / "bad.yaml").write_text("name: [unclosed\n", encoding="utf-8")

    result = _load_profiles(tmp_path)

    assert len(result.errors) == 1
    assert "price/fixed" in result.profiles


# --- the degradation matrix --------------------------------------------------

# "Assume nothing is installed" is the integration's first design principle, so
# it is enforced structurally rather than case by case: for every kind of data
# source there must be a path through setup that needs no other integration and
# no entity to point at.

ENTITY_SELECTORS = {"entity", "device", "config_entry", "target"}


def _needs_another_integration(document) -> bool:
    detect = document.get("detect") or {}
    if not detect or detect.get("always"):
        return False
    return bool(detect.get("integration") or detect.get("any_integration"))


def _needs_an_entity(document) -> bool:
    for option in (document.get("options") or {}).values():
        if not option.get("required", True):
            continue
        if ENTITY_SELECTORS & set(option["selector"]):
            return True
    return False


@pytest.mark.parametrize("kind", ["price", "pv", "load"])
def test_every_source_kind_has_a_path_needing_nothing(kind: str):
    """A user with no price, solar or load integration must still get set up.

    Without this, a house with none of the usual integrations reaches a step in
    the config flow where every option is unusable.
    """
    usable = []
    for path in sorted((BUILTIN_ROOT / kind).glob("*.yaml")):
        document = validate_document(load_yaml(str(path)))
        if not _needs_another_integration(document) and not _needs_an_entity(document):
            usable.append(path.stem)

    assert usable, (
        f"no '{kind}' profile works without another integration or an entity; "
        "a house without one cannot complete setup"
    )


def test_inverter_control_is_optional():
    """Battery control must be skippable; the plan is still worth having."""
    from custom_components.emhass_companion.configuration import EmhassConfig

    config = EmhassConfig(url="http://x")
    assert not config.inverter, "an unset inverter profile must be falsy"


@pytest.mark.parametrize("kind", ["price", "pv", "load", "inverter"])
def test_every_kind_ships_at_least_one_profile(kind: str):
    assert list((BUILTIN_ROOT / kind).glob("*.yaml")), f"no {kind} profiles shipped"


# --- inverter control semantics ----------------------------------------------
#
# Every case here is a wrong number reaching real hardware if it regresses.


def _inverter(control: dict, **overrides) -> dict:
    document = {
        "name": "Test inverter",
        "kind": "inverter",
        "version": 2,
        "control": control,
        "actions": {"self_consume": [{"service": "select.select_option"}]},
    }
    return validate_document({**document, **overrides})


def test_percent_units_require_a_rating_to_be_a_percentage_of():
    with pytest.raises(ProfileError, match="rated_power_w"):
        _inverter({"power_unit": "percent_of_rated"})


def test_restore_required_needs_a_way_to_restore():
    with pytest.raises(ProfileError, match="restore"):
        validate_document(
            {
                "name": "Test inverter",
                "kind": "inverter",
                "version": 2,
                "control": {"restore_required": True},
                "actions": {"idle": [{"service": "select.select_option"}]},
            }
        )


def test_a_restore_action_satisfies_restore_required():
    document = validate_document(
        {
            "name": "Test inverter",
            "kind": "inverter",
            "version": 2,
            "control": {"restore_required": True},
            "actions": {"restore": [{"service": "select.select_option"}]},
        }
    )
    assert "restore" in document["actions"]


def test_unknown_action_names_are_rejected():
    with pytest.raises(ProfileError, match="unknown action"):
        validate_document(
            {
                "name": "Test inverter",
                "kind": "inverter",
                "version": 2,
                "actions": {
                    "self_consume": [{"service": "a.b"}],
                    "frobnicate": [{"service": "a.b"}],
                },
            }
        )


# --- pairing: anything that can be turned on must be definable to turn off ---
#
# An export limit (or a grid-charge permission) left in place because a
# profile defines the "on" half but not the "off" half is a silent, ongoing
# cost -- the same failure mode restore_required guards against for the
# battery, just for money instead of a battery stuck in the wrong mode.


def test_curtail_without_uncurtail_is_rejected():
    with pytest.raises(ProfileError, match="uncurtail"):
        validate_document(
            {
                "name": "Test inverter",
                "kind": "inverter",
                "version": 2,
                "actions": {
                    "self_consume": [{"service": "a.b"}],
                    "curtail": [{"service": "a.b"}],
                },
            }
        )


def test_uncurtail_without_curtail_is_rejected():
    with pytest.raises(ProfileError, match="curtail"):
        validate_document(
            {
                "name": "Test inverter",
                "kind": "inverter",
                "version": 2,
                "actions": {
                    "self_consume": [{"service": "a.b"}],
                    "uncurtail": [{"service": "a.b"}],
                },
            }
        )


def test_curtail_and_uncurtail_defined_together_is_accepted():
    document = validate_document(
        {
            "name": "Test inverter",
            "kind": "inverter",
            "version": 2,
            "actions": {
                "self_consume": [{"service": "a.b"}],
                "curtail": [{"service": "a.b"}],
                "uncurtail": [{"service": "a.b"}],
            },
        }
    )
    assert "curtail" in document["actions"]
    assert "uncurtail" in document["actions"]


def test_neither_curtail_nor_uncurtail_is_accepted():
    """Curtailment is entirely optional -- most installs define neither."""
    document = validate_document(
        {
            "name": "Test inverter",
            "kind": "inverter",
            "version": 2,
            "actions": {"self_consume": [{"service": "a.b"}]},
        }
    )
    assert "curtail" not in document["actions"]


def test_allow_grid_charge_without_block_is_rejected():
    with pytest.raises(ProfileError, match="block_grid_charge"):
        validate_document(
            {
                "name": "Test inverter",
                "kind": "inverter",
                "version": 2,
                "actions": {
                    "self_consume": [{"service": "a.b"}],
                    "allow_grid_charge": [{"service": "a.b"}],
                },
            }
        )


def test_a_version_1_profile_gets_the_pre_control_defaults():
    """An unmigrated profile must behave exactly as it did before `control:`."""
    document = validate_document(
        {
            "name": "Old inverter",
            "kind": "inverter",
            "version": 1,
            "actions": {"self_consume": [{"service": "select.select_option"}]},
        }
    )
    assert document["control"]["power_unit"] == "w"
    assert document["control"]["lifetime"] == "persistent"
    assert document["control"]["signed"] is False


def test_a_block_commented_out_entirely_is_treated_as_empty():
    """`limits:` with every line commented out parses as None, not as {}."""
    document = validate_document(
        {
            "name": "Test inverter",
            "kind": "inverter",
            "version": 2,
            "limits": None,
            "sensors": None,
            "control": None,
            "actions": {"self_consume": [{"service": "select.select_option"}]},
        }
    )
    assert document["limits"] == {}
    assert document["control"]["power_unit"] == "w"


# --- the contributor template ------------------------------------------------


def test_the_inverter_template_is_a_valid_profile():
    """The template is what contributors copy, so it must not rot."""
    path = Path(__file__).parent.parent / "docs" / "inverter_profile_template.yaml"
    document = validate_document(load_yaml(str(path)))
    assert document["kind"] == "inverter"
    assert document["version"] == PROFILE_SCHEMA_VERSION


def test_the_inverter_template_demonstrates_every_control_key():
    """A key missing from the template is a key nobody will know exists."""
    path = Path(__file__).parent.parent / "docs" / "inverter_profile_template.yaml"
    raw = load_yaml(str(path))
    assert set(DEFAULT_CONTROL) <= set(raw["control"]), (
        "the template's control block no longer shows every option"
    )


def test_the_script_profile_only_requires_the_script_it_falls_back_to():
    """Its own notes tell the user to leave a mode's script unset.

    Marking every option required contradicts that: the form would refuse to
    submit, and the documented configuration would be unreachable.
    self_consume stays required because the executor calls it whenever a plan
    goes stale, which is the one thing that must always work.
    """
    document = validate_document(load_yaml(str(BUILTIN_ROOT / "inverter" / "generic_script.yaml")))
    required = {key for key, option in document["options"].items() if option.get("required", True)}
    assert required == {"self_consume_script"}

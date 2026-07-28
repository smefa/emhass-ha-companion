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

from custom_components.emhass_companion.const import PROFILE_KINDS
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

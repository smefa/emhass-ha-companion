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
    _profile_selector,
    _tariff_side_schema,
    battery_schema,
    grid_schema,
)
from custom_components.emhass_companion.profiles import BUILTIN_ROOT
from custom_components.emhass_companion.profiles.schema import Profile, validate_document


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_tariff_schema_builds(side):
    assert vol.Schema(_tariff_side_schema(side, {}))


def test_tariff_schema_builds_with_stored_values():
    stored = {"mode": "linear", "multiplier": 1.25, "adder": 0.5375}
    assert vol.Schema(_tariff_side_schema("buy", stored))


def test_battery_schema_builds():
    assert vol.Schema(battery_schema({}))


def test_grid_schema_builds():
    assert vol.Schema(grid_schema({}))


def test_profile_selector_builds():
    profiles = [
        Profile(key="price/a", path="a.yaml", kind="price", name="A", document={}),
        Profile(key="price/b", path="b.yaml", kind="price", name="B", document={}),
    ]
    assert _profile_selector(profiles)


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

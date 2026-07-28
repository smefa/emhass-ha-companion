"""Packaging and translation consistency.

A missing translation key does not raise -- it renders as a raw slug in the
user interface. These checks make that a test failure instead.
"""

from __future__ import annotations

import json
from pathlib import Path

from homeassistant.util.yaml import load_yaml
import pytest

COMPONENT = Path(__file__).parent.parent / "custom_components" / "emhass_companion"
REPO = Path(__file__).parent.parent


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def strings() -> dict:
    return _json(COMPONENT / "strings.json")


# --- manifest ----------------------------------------------------------------


def test_manifest_has_the_keys_hacs_and_hassfest_require():
    manifest = _json(COMPONENT / "manifest.json")
    for key in (
        "domain",
        "name",
        "codeowners",
        "documentation",
        "issue_tracker",
        "version",
        "iot_class",
        "integration_type",
        "config_flow",
    ):
        assert key in manifest, f"manifest.json is missing '{key}'"
    assert manifest["domain"] == COMPONENT.name


def test_hacs_manifest_is_valid():
    hacs = _json(REPO / "hacs.json")
    assert hacs["name"]
    # `persistent_directory` preserves a directory *inside* the integration
    # across updates. Using it for `profiles` would freeze the shipped
    # built-ins at whatever version was first installed. User profiles live in
    # <config>/emhass_companion/profiles/ instead, which HACS never touches.
    assert "persistent_directory" not in hacs


def test_english_translations_match_strings():
    """en.json is a copy of strings.json; drift means untranslated English."""
    assert _json(COMPONENT / "strings.json") == _json(COMPONENT / "translations" / "en.json")


# --- entity translation keys -------------------------------------------------

# (platform, translation_key) pairs declared in the entity modules.
EXPECTED_ENTITY_KEYS = {
    "sensor": {
        "pv_forecast",
        "load_forecast",
        "grid_forecast",
        "battery_power",
        "battery_soc",
        "buy_price",
        "sell_price",
        "optimization_status",
        "plan_cost",
        "last_payload",
    },
    "binary_sensor": {"plan_stale"},
    "switch": {"control_enabled"},
    "select": {"system_mode"},
    "button": {"run_dayahead", "run_mpc"},
}


@pytest.mark.parametrize(
    ("platform", "keys"), EXPECTED_ENTITY_KEYS.items(), ids=EXPECTED_ENTITY_KEYS.keys()
)
def test_every_entity_has_a_translated_name(strings, platform, keys):
    translated = strings["entity"].get(platform, {})
    for key in keys:
        assert key in translated, f"strings.json has no entity.{platform}.{key}"
        assert translated[key].get("name"), f"entity.{platform}.{key} has no name"


def test_select_options_are_translated(strings):
    from custom_components.emhass_companion.const import SYSTEM_MODES

    states = strings["entity"]["select"]["system_mode"]["state"]
    for mode in SYSTEM_MODES:
        assert mode in states, f"mode '{mode}' has no translation"


# --- services ----------------------------------------------------------------


def test_services_yaml_matches_registered_services(strings):
    from custom_components.emhass_companion.services import (
        SERVICE_RUN_DAYAHEAD,
        SERVICE_RUN_MPC,
        SERVICE_TEST_PROFILE,
    )

    registered = {SERVICE_RUN_DAYAHEAD, SERVICE_RUN_MPC, SERVICE_TEST_PROFILE}
    declared = set(load_yaml(str(COMPONENT / "services.yaml")))
    described = set(strings["services"])

    assert declared == registered, "services.yaml does not match the registered services"
    assert described == registered, "strings.json does not describe every service"


# --- issues ------------------------------------------------------------------


def test_every_repair_issue_has_translations(strings):
    from custom_components.emhass_companion.const import (
        ISSUE_BAD_PROFILE,
        ISSUE_EMHASS_VERSION,
    )

    for issue in (ISSUE_BAD_PROFILE, ISSUE_EMHASS_VERSION):
        assert issue in strings["issues"], f"issue '{issue}' has no translation"
        assert strings["issues"][issue].get("title")
        assert strings["issues"][issue].get("description")


# --- config flow -------------------------------------------------------------


def test_every_config_flow_error_is_translated(strings):
    """An untranslated error renders as a slug mid-setup."""
    for key in ("cannot_connect", "unknown_version", "version_too_old"):
        assert key in strings["config"]["error"]


def test_config_flow_steps_are_translated(strings):
    steps = strings["config"]["step"]
    for step in (
        "user",
        "price",
        "price_options",
        "tariff",
        "pv",
        "pv_options",
        "load",
        "load_options",
        "battery",
        "grid",
    ):
        assert step in steps, f"config step '{step}' has no translation"
        assert steps[step].get("title"), f"config step '{step}' has no title"


def test_options_flow_menu_is_translated(strings):
    menu = strings["options"]["step"]["init"]["menu_options"]
    for option in ("battery", "grid", "tariff"):
        assert option in menu
        assert option in strings["options"]["step"]

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


def test_manifest_keys_are_sorted():
    """hassfest requires domain, name, then strict alphabetical order.

    A hard error in CI, and invisible locally, so it is asserted here.
    """
    manifest = _json(COMPONENT / "manifest.json")
    keys = list(manifest)
    assert keys[:2] == ["domain", "name"], "domain and name must come first"
    assert keys[2:] == sorted(keys[2:]), f"remaining keys are not alphabetical: {keys[2:]}"


def test_subentry_translations_have_no_removed_title_key():
    """`title` was moved out of flow blocks; hassfest flags it as removed."""
    strings = _json(COMPONENT / "strings.json")
    for name, subentry in strings["config_subentries"].items():
        assert "title" not in subentry, f"config_subentries.{name} still has a title"
    assert "title" not in strings["config"]


def test_subentry_translations_have_the_required_keys():
    """hassfest requires entry_type and initiate_flow.user for subentry flows."""
    strings = _json(COMPONENT / "strings.json")
    for name, subentry in strings["config_subentries"].items():
        assert subentry.get("entry_type"), f"config_subentries.{name} has no entry_type"
        assert subentry.get("initiate_flow", {}).get("user"), (
            f"config_subentries.{name} has no initiate_flow.user"
        )


def test_data_descriptions_only_describe_existing_fields():
    """hassfest rejects a data_description key with no matching data key."""
    strings = _json(COMPONENT / "strings.json")
    blocks = [strings.get("config", {}), strings.get("options", {})]
    blocks += list(strings.get("config_subentries", {}).values())

    for block in blocks:
        for step_name, step in block.get("step", {}).items():
            described = set(step.get("data_description", {}))
            labelled = set(step.get("data", {}))
            assert described <= labelled, (
                f"step '{step_name}' describes fields it does not label: {described - labelled}"
            )


def test_imported_home_assistant_components_are_declared():
    """hassfest fails if a component is imported but not declared.

    Only visible in CI otherwise, and the failure names the component rather
    than the file, so it is worth catching here with the import in view.
    """
    import re

    manifest = _json(COMPONENT / "manifest.json")
    declared = set(manifest.get("dependencies", [])) | set(manifest.get("after_dependencies", []))
    # `hassio` is imported lazily inside a try/except, which the regex below
    # would otherwise miss.
    pattern = re.compile(r"from homeassistant\.components\.(\w+)")
    used: set[str] = set()
    for path in COMPONENT.rglob("*.py"):
        used |= set(pattern.findall(path.read_text(encoding="utf-8")))

    # Entity platforms are provided by the integration itself, not depended on.
    platforms = {
        "sensor",
        "binary_sensor",
        "switch",
        "select",
        "button",
        "number",
        "time",
    }
    missing = used - declared - platforms
    assert not missing, f"imported but not declared in manifest.json: {missing}"


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
        # per deferrable load
        "scheduled_power",
        "next_start",
        "runtime_today",
        "battery_action",
    },
    "binary_sensor": {"plan_stale", "should_run"},
    "switch": {"control_enabled", "load_enabled", "use_time_window"},
    "select": {"system_mode", "load_mode"},
    "button": {"run_dayahead", "run_mpc"},
    "number": {"nominal_power", "operating_hours"},
    "time": {"earliest_start", "latest_end"},
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
    from custom_components.emhass_companion.const import LOAD_MODES, SYSTEM_MODES

    for key, modes in (("system_mode", SYSTEM_MODES), ("load_mode", LOAD_MODES)):
        states = strings["entity"]["select"][key]["state"]
        for mode in modes:
            assert mode in states, f"{key} option '{mode}' has no translation"


def test_deferrable_subentry_is_translated(strings):
    """A subentry with no translations shows raw field names when adding a load."""
    from custom_components.emhass_companion.config_flow import deferrable_schema
    from custom_components.emhass_companion.const import SUBENTRY_TYPE_DEFERRABLE

    subentry = strings["config_subentries"][SUBENTRY_TYPE_DEFERRABLE]
    assert subentry["entry_type"]

    fields = {str(marker.schema) for marker in deferrable_schema({})}
    for step in ("user", "reconfigure"):
        labelled = set(subentry["step"][step]["data"])
        assert fields <= labelled, f"{step} is missing labels for {fields - labelled}"


def test_every_platform_is_forwarded(strings):
    """A platform module that is never forwarded creates no entities at all."""
    from custom_components.emhass_companion import PLATFORMS

    forwarded = {platform.value for platform in PLATFORMS}
    assert set(EXPECTED_ENTITY_KEYS) <= forwarded, (
        f"not forwarded: {set(EXPECTED_ENTITY_KEYS) - forwarded}"
    )


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
    for option in ("battery", "grid", "tariff", "inverter"):
        assert option in menu
        assert option in strings["options"]["step"]


def test_battery_action_states_are_translated(strings):
    from custom_components.emhass_companion.const import BATTERY_ACTIONS

    states = strings["entity"]["sensor"]["battery_action"]["state"]
    for action in BATTERY_ACTIONS:
        assert action in states, f"battery action '{action}' has no translation"


def test_every_inverter_profile_defines_the_fallback_action(strings):
    """The executor falls back to self-consumption whenever a plan goes stale.

    A profile missing that action leaves the watchdog with nothing to do.
    """
    from homeassistant.util.yaml import load_yaml

    from custom_components.emhass_companion.const import MODE_SELF_CONSUME
    from custom_components.emhass_companion.profiles import BUILTIN_ROOT

    profiles = sorted((BUILTIN_ROOT / "inverter").glob("*.yaml"))
    assert profiles, "no built-in inverter profiles found"
    for path in profiles:
        actions = load_yaml(str(path))["actions"]
        assert MODE_SELF_CONSUME in actions, f"{path.name} has no self_consume action"


# --- dashboard cards ---------------------------------------------------------


def test_card_bundle_parses_as_a_javascript_module():
    """Syntax-check the card bundle without needing Node.

    The bundle is plain ES2017-compatible JavaScript on purpose: it can then be
    parsed by a pure-Python parser, which is the only way this file gets any
    automated checking at all. Optional chaining and nullish coalescing are
    deliberately avoided for the same reason.
    """
    esprima = pytest.importorskip("esprima")

    bundle = COMPONENT / "frontend" / "emhass-cards.js"
    assert bundle.is_file(), f"card bundle missing at {bundle}"
    esprima.parseModule(bundle.read_text(encoding="utf-8"))


def test_card_bundle_avoids_syntax_the_parser_cannot_check():
    """Guard the constraint above, since a lapse silently disables the check."""
    source = (COMPONENT / "frontend" / "emhass-cards.js").read_text(encoding="utf-8")
    assert "?." not in source, "optional chaining defeats the syntax check"
    assert "??" not in source, "nullish coalescing defeats the syntax check"


def test_cards_are_registered_with_the_picker():
    """Without window.customCards the cards never appear in the UI picker."""
    source = (COMPONENT / "frontend" / "emhass-cards.js").read_text(encoding="utf-8")
    assert "window.customCards" in source
    for card in ("emhass-plan-card", "emhass-deferrable-card"):
        assert f'customElements.define("{card}"' in source


# --- additional translations -------------------------------------------------


def _leaf_paths(node, prefix=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _leaf_paths(value, f"{prefix}.{key}")
    elif isinstance(node, str):
        yield prefix


@pytest.mark.parametrize(
    "language",
    [path.stem for path in sorted((COMPONENT / "translations").glob("*.json"))],
)
def test_translation_has_the_same_keys_as_strings(language):
    """A translation with extra or missing keys is rejected by hassfest.

    Missing keys also fall back to English silently, so a partial translation
    is easy to ship without noticing.
    """
    strings = _json(COMPONENT / "strings.json")
    translated = _json(COMPONENT / "translations" / f"{language}.json")

    expected = set(_leaf_paths(strings))
    actual = set(_leaf_paths(translated))

    assert not expected - actual, f"{language}.json is missing: {expected - actual}"
    assert not actual - expected, f"{language}.json has extra keys: {actual - expected}"


def test_translations_keep_their_placeholders():
    """A dropped placeholder renders as a literal brace to the user."""
    import re

    strings = _json(COMPONENT / "strings.json")

    def leaves(node, prefix=""):
        if isinstance(node, dict):
            for key, value in node.items():
                yield from leaves(value, f"{prefix}.{key}")
        elif isinstance(node, str):
            yield prefix, node

    english = dict(leaves(strings))
    pattern = re.compile(r"\{(\w+)\}")

    for path in sorted((COMPONENT / "translations").glob("*.json")):
        translated = dict(leaves(_json(path)))
        for key, value in english.items():
            expected = set(pattern.findall(value))
            actual = set(pattern.findall(translated.get(key, "")))
            assert expected == actual, f"{path.name}{key}: placeholders {expected} became {actual}"


# --- brand icon ---------------------------------------------------------------

# Since Home Assistant 2026.3.0, a custom integration ships its own icon
# directly rather than depending on a PR being merged into the separate
# home-assistant/brands repository: https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/
BRAND_SPEC = {
    "icon.png": (256, 256),
    "icon@2x.png": (512, 512),
}
BRAND_LOGO_SHORTEST_SIDE = {
    "logo.png": 256,
    "logo@2x.png": 512,
}


@pytest.mark.parametrize(("filename", "size"), BRAND_SPEC.items())
def test_brand_icon_matches_the_required_square_size(filename, size):
    """The proxy serves whatever is on disk verbatim -- a wrong size ships as-is."""
    from PIL import Image

    path = COMPONENT / "brand" / filename
    assert path.is_file(), f"missing {path}"
    with Image.open(path) as img:
        assert img.size == size, f"{filename} is {img.size}, expected {size}"
        assert img.mode == "RGBA", f"{filename} has no alpha channel"


@pytest.mark.parametrize(("filename", "shortest_side"), BRAND_LOGO_SHORTEST_SIDE.items())
def test_brand_logo_is_landscape_with_the_right_height(filename, shortest_side):
    from PIL import Image

    path = COMPONENT / "brand" / filename
    assert path.is_file(), f"missing {path}"
    with Image.open(path) as img:
        width, height = img.size
        assert height == shortest_side, f"{filename} height is {height}, expected {shortest_side}"
        assert width >= height, f"{filename} is not landscape: {img.size}"


def test_brand_icon_has_a_transparent_background():
    """Guidelines: "images with transparency are preferred"."""
    from PIL import Image

    with Image.open(COMPONENT / "brand" / "icon.png") as img:
        assert img.getpixel((0, 0))[3] == 0, "top-left corner is not transparent"

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
        "solar_surplus",
        "solar_surplus_energy",
        "solar_surplus_start",
        "solar_surplus_end",
        # per deferrable load
        "scheduled_power",
        "next_start",
        "runtime_today",
        "battery_action",
        "surplus_budget",
    },
    "binary_sensor": {"plan_stale", "should_run", "running", "solar_surplus"},
    "switch": {
        "control_enabled",
        "load_enabled",
        "use_time_window",
        "semi_continuous",
        "single_constant",
        "load_requested",
        "start_asap",
    },
    "select": {"system_mode", "recurrence", "cost_fun"},
    "button": {"run_dayahead", "run_mpc", "run_forecast_fit", "run_now"},
    "number": {
        "nominal_power",
        "minimum_power",
        "operating_hours",
        "startup_penalty",
        "max_startups",
        "energy_needed",
        "surplus_headroom",
        "surplus_threshold",
    },
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
    from custom_components.emhass_companion.const import COST_FUNS, RECURRENCES, SYSTEM_MODES

    for key, modes in (
        ("system_mode", SYSTEM_MODES),
        ("recurrence", RECURRENCES),
        ("cost_fun", COST_FUNS),
    ):
        states = strings["entity"]["select"][key]["state"]
        for mode in modes:
            assert mode in states, f"{key} option '{mode}' has no translation"


def test_deferrable_subentry_is_translated(strings):
    """A subentry with no translations shows raw field names when adding a load."""
    from custom_components.emhass_companion.config_flow import (
        deferrable_kind_schema,
        deferrable_schema,
    )
    from custom_components.emhass_companion.const import RECURRENCES, SUBENTRY_TYPE_DEFERRABLE

    subentry = strings["config_subentries"][SUBENTRY_TYPE_DEFERRABLE]
    assert subentry["entry_type"]

    # Each step deliberately shows different fields: the first asks only what
    # decides the rest, and reconfigure offers only what the subentry still
    # owns, because everything else became an entity the moment the load was
    # created.
    for step, schema in (
        ("user", deferrable_kind_schema({})),
        ("reconfigure", deferrable_schema({}, initial=False)),
    ):
        fields = {str(marker.schema) for marker in schema}
        labelled = set(subentry["step"][step]["data"])
        assert fields <= labelled, f"{step} is missing labels for {fields - labelled}"
        assert labelled <= fields, f"{step} labels fields it does not show: {labelled - fields}"

    # The settings step shows a different subset per recurrence, so its one set
    # of labels has to cover every branch and label nothing that no branch shows.
    shown: set[str] = set()
    for recurrence in RECURRENCES:
        shown |= {str(marker.schema) for marker in deferrable_schema({}, recurrence=recurrence)}
    labelled = set(subentry["step"]["settings"]["data"])
    assert shown <= labelled, f"settings is missing labels for {shown - labelled}"
    assert labelled <= shown, f"settings labels fields no recurrence shows: {labelled - shown}"


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
        SERVICE_TYPICAL_LOAD_FORECAST,
    )

    registered = {
        SERVICE_RUN_DAYAHEAD,
        SERVICE_RUN_MPC,
        SERVICE_TEST_PROFILE,
        SERVICE_TYPICAL_LOAD_FORECAST,
    }
    declared = set(load_yaml(str(COMPONENT / "services.yaml")))
    described = set(strings["services"])

    assert declared == registered, "services.yaml does not match the registered services"
    assert described == registered, "strings.json does not describe every service"


# --- issues ------------------------------------------------------------------


def test_every_repair_issue_has_translations(strings):
    """Every ISSUE_* constant, not a hand-kept list -- an untranslated repair
    renders as a slug in the one place the user goes when something is wrong."""
    from custom_components.emhass_companion import const

    issues = [value for name, value in vars(const).items() if name.startswith("ISSUE_")]
    assert issues

    for issue in issues:
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


BUNDLE = COMPONENT / "frontend" / "emhass-cards.js"

# Every card in the bundle, which is also the list the picker must offer.
CARDS = (
    "emhass-plan-card",
    "emhass-deferrable-card",
    "emhass-deferrable-swipe-card",
    "emhass-deferrable-strip-card",
    "emhass-health-card",
    "emhass-status-card",
    "emhass-overview-card",
)


@pytest.fixture(scope="module")
def bundle() -> str:
    assert BUNDLE.is_file(), f"card bundle missing at {BUNDLE}"
    return BUNDLE.read_text(encoding="utf-8")


def test_card_bundle_parses_as_a_javascript_module(bundle):
    """Syntax-check the card bundle without needing Node.

    The bundle is plain ES2017-compatible JavaScript on purpose: it can then be
    parsed by a pure-Python parser, which is the only way this file gets any
    automated checking at all. Optional chaining and nullish coalescing are
    deliberately avoided for the same reason.
    """
    esprima = pytest.importorskip("esprima")
    esprima.parseModule(bundle)


def test_card_bundle_avoids_syntax_the_parser_cannot_check(bundle):
    """Guard the constraint above, since a lapse silently disables the check."""
    assert "?." not in bundle, "optional chaining defeats the syntax check"
    assert "??" not in bundle, "nullish coalescing defeats the syntax check"


def test_cards_are_registered_with_the_picker(bundle):
    """Without window.customCards a card never appears in the UI picker.

    An unregistered card can only be added by hand-writing YAML, which is
    exactly what shipping cards with the integration exists to avoid.
    """
    assert "window.customCards" in bundle
    for card in CARDS:
        assert f'customElements.define("{card}"' in bundle
        assert f'type: "{card}"' in bundle


def test_no_card_still_advertises_itself_as_experimental(bundle):
    """The lab bundle graduated into this one; the labels came with it."""
    assert "lab)" not in bundle, "a card is still named as an experiment"
    assert "lab:" not in bundle, "a card is still named as an experiment"


def test_cards_look_up_hub_entities_by_their_real_translation_keys(bundle):
    """A key no entity publishes finds nothing, quietly.

    The later cards read a good deal more of the integration than the first two
    did -- decisions, stage timings, surplus windows -- so a typo here surfaces
    as one empty field rather than an empty card.
    """
    import re

    strings = _json(COMPONENT / "strings.json")
    entity = strings.get("entity", {})
    published = {f"{domain}.{key}" for domain in entity for key in entity.get(domain, {})}
    load_keys = {key.split(".", 1)[1] for key in published}

    # Hub entities are looked up domain-qualified, since two of them share a
    # translation key across domains.
    for key in re.findall(r'hub\["([a-z_]+\.[a-z_]+)"\]', bundle):
        assert key in published, f'a card asks the hub for "{key}", which nothing publishes'
    for key in re.findall(r'_entity\("([a-z_]+)"\)', bundle):
        assert key in load_keys, f'a card asks a load for "{key}", which nothing publishes'
    # An info box names its entity in a table rather than at the lookup, so the
    # same typo would slip past the patterns above.
    for key in re.findall(r'entity: "([a-z_]+\.[a-z_]+)"', bundle):
        assert key in published, f'an info box is pointed at "{key}", which nothing publishes'
    for key in re.findall(r'entity: "([a-z_]+)",', bundle):
        assert key in load_keys, f'a load info box is pointed at "{key}", which nothing publishes'


def test_every_card_editor_is_reachable_from_the_card_it_edits(bundle):
    """A defined editor element nothing returns is an editor nobody can open.

    getConfigElement is the only route the Lovelace dialog takes: without it
    the card falls back to the YAML box, which is exactly what these editors
    exist to avoid.
    """
    for card in CARDS:
        assert f'customElements.define("{card}-editor"' in bundle, f"{card} has no editor"
        assert f'document.createElement("{card}-editor")' in bundle, (
            f"{card} never returns its editor"
        )


@pytest.mark.parametrize(
    ("table", "order", "defaults"),
    [
        ("HEALTH_METRICS", "HEALTH_METRIC_ORDER", "HEALTH_DEFAULTS"),
        ("TILE_METRICS", "TILE_ORDER", "TILE_DEFAULTS"),
        ("LOAD_METRICS", "LOAD_METRIC_ORDER", "LOAD_BOX_DEFAULTS"),
    ],
)
def test_selectable_boxes_only_offer_metrics_that_exist(bundle, table, order, defaults):
    """A dropdown option with no entry behind it renders an undefined label.

    And a default naming a metric that does not exist is worse: the box is not
    chosen by anyone, so it is the *shipped* layout that breaks.
    """
    import re

    body = bundle.split(f"const {table} = {{", 1)[1].split("\n};", 1)[0]
    defined = set(re.findall(r"^  ([a-z_]+): \{", body, re.MULTILINE))
    offered = set(
        re.findall(r'"([a-z_]+)"', bundle.split(f"const {order} = [", 1)[1].split("];", 1)[0])
    )
    assert offered == defined, f"{order} and {table} disagree: {offered ^ defined}"

    written = bundle.split(f"const {defaults} = [", 1)[1].split("];", 1)[0]
    for key in re.findall(r'"([a-z_]+)"', written):
        assert key in defined, f'{defaults} names "{key}", which is not a metric'


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


def test_cards_match_entities_by_translation_key():
    """Entity ids are translated and renameable; unique_id never reaches the card.

    Both cards used to locate a load's entities by English substrings of the
    entity id (`id.includes("should_run")`). An entity id is built from the
    entity's translated name, so on a Swedish install -- a translation this
    repo ships itself -- `should_run` is `binary_sensor.<load>_ska_kora` and
    the match found nothing at all, leaving every per-load card empty. A
    rename broke it the same way.

    Keying on unique_id instead looked like the fix and was worse: it failed
    on *every* install, in every language. `hass.entities` is the registry's
    display collection (`config/entity_registry/list_for_display`), whose
    entries carry `ei`/`di`/`pl`/`tk` and a few display fields -- and no
    unique_id whatsoever. Both cards silently found zero entities, so the plan
    card read "No plan yet" and every deferrable card read "No deferrable
    load". translation_key is the only identifier that is stable across
    renames and languages *and* actually delivered to the frontend.
    """
    source = (COMPONENT / "frontend" / "emhass-cards.js").read_text(encoding="utf-8")
    assert "id.includes(" not in source, "entity ids are not a stable key"
    # Reading it, not the prose above explaining why nothing may.
    assert ".unique_id" not in source, "unique_id is not in the display registry"
    assert '["unique_id"]' not in source, "unique_id is not in the display registry"
    assert "entry.translation_key" in source
    assert '"should_run" in load.entities' in source


def test_cards_look_up_load_entities_by_their_real_translation_keys():
    """A load's key is what strings.json calls it, not what Python calls it.

    Two of a load's entities are namespaced away from the hub's in
    strings.json: `key="enabled"` is `translation_key="load_enabled"`, and
    `key="requested"` is `translation_key="load_requested"`. Since the cards
    now key on translation_key, asking for "enabled" or "requested" finds
    nothing -- which loses the whole controls row of every deferrable card.
    """
    import re

    source = (COMPONENT / "frontend" / "emhass-cards.js").read_text(encoding="utf-8")

    # Every translation key the cards ask a load for must be one a load
    # actually publishes, per strings.json.
    strings = _json(COMPONENT / "strings.json")
    published = {
        key
        for domain in ("binary_sensor", "button", "number", "select", "sensor", "switch", "time")
        for key in strings.get("entity", {}).get(domain, {})
    }
    assert {"load_enabled", "load_requested"} <= published, "strings.json changed shape"

    for key in re.findall(r'find\("([a-z_]+)"\)', source):
        assert key in published, f'the card asks a load for "{key}", which no entity publishes'
    for match in re.findall(r"wanted = \[([^\]]+)\]", source):
        for key in re.findall(r'"([a-z_]+)"', match):
            assert key in published, f'the card asks a load for "{key}", which no entity publishes'


def test_cards_attach_their_shadow_root_at_most_once():
    """`setConfig` runs repeatedly on one element, `attachShadow` may not.

    The card editor calls `setConfig` on the same element for every option
    the user changes, and `setConfig` clears `_root` so the markup is rebuilt.
    An unguarded `attachShadow` in that rebuild throws `NotSupportedError`,
    which red-cards the element the moment anyone edits the card.
    """
    source = (COMPONENT / "frontend" / "emhass-cards.js").read_text(encoding="utf-8")
    assert source.count("this.attachShadow(") == source.count(
        "if (!this.shadowRoot) this.attachShadow("
    )

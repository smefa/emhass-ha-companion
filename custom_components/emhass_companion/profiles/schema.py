"""The profile document schema.

This schema is a **public API**. Contributors write profiles against it, and users
drop their own into ``/config/emhass_companion/profiles/``. Every profile declares
``version: 1``; changing the meaning of an existing key requires a version bump and
a documented migration, not a silent redefinition.

A profile is data, never code. It describes *where* to read from and *how* to map
the result -- it does not describe control flow. When a source cannot be expressed
declaratively, the escape hatch is a ``template`` source, not a new keyword.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector
import voluptuous as vol

from ..const import (
    PROFILE_KIND_INVERTER,
    PROFILE_KINDS,
    PROFILE_SCHEMA_VERSION,
    SOURCE_TYPE_ATTRIBUTES,
    SOURCE_TYPE_SERVICE,
    SOURCE_TYPE_TEMPLATE,
    SOURCE_TYPES,
    UNIT_CURRENCY_PER_KWH,
    UNIT_WATTS,
)


class ProfileError(Exception):
    """A profile document is invalid or cannot be used."""


def _validate_selector(config: Any) -> dict[str, Any]:
    """Validate a Home Assistant selector definition, keeping the raw dict.

    The dict form is kept rather than an instantiated Selector so that a profile
    stays serialisable for diagnostics; the instance is built when a config flow
    step actually renders the field.
    """
    if not isinstance(config, dict):
        raise vol.Invalid("selector must be a mapping")
    selector.validate_selector(config)
    return config


OPTION_SCHEMA = vol.Schema(
    {
        vol.Optional("name"): cv.string,
        vol.Optional("description"): cv.string,
        vol.Optional("required", default=True): cv.boolean,
        vol.Optional("default"): vol.Any(cv.string, int, float, bool, list, dict, None),
        vol.Required("selector"): _validate_selector,
    }
)

# `detect` gates whether the config flow offers this profile at all. Showing a
# Solcast profile to someone without Solcast installed is the exact kind of
# "which of these thirty knobs applies to me" problem this integration exists
# to remove.
DETECT_SCHEMA = vol.Schema(
    {
        vol.Optional("integration"): cv.string,
        vol.Optional("any_integration"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("always", default=False): cv.boolean,
    }
)

# Every field here may be a template, so that a profile can expose the choice of
# field or unit scaling as a user option instead of shipping one variant per
# combination. Values are coerced to float after rendering.
SERIES_SCHEMA = vol.Schema(
    {
        vol.Required("time"): cv.string,
        vol.Required("value"): cv.string,
        vol.Optional("scale", default=1.0): vol.Any(vol.Coerce(float), cv.string),
        vol.Optional("offset", default=0.0): vol.Any(vol.Coerce(float), cv.string),
    }
)

SOURCE_SCHEMA = vol.Schema(
    {
        vol.Required("type"): vol.In(SOURCE_TYPES),
        # attributes
        vol.Optional("entity"): cv.string,
        vol.Optional("attributes"): vol.All(cv.ensure_list, [cv.string]),
        # service
        vol.Optional("service"): cv.string,
        vol.Optional("data"): dict,
        vol.Optional("for_days"): vol.All(cv.ensure_list, [vol.Coerce(int)]),
        vol.Optional("response_path"): cv.string,
        # template
        vol.Optional("value"): cv.string,
    }
)

ACTION_STEP_SCHEMA = vol.Schema(
    {
        vol.Required("service"): cv.string,
        vol.Optional("target"): dict,
        vol.Optional("data"): dict,
    }
)


def _validate_source(source: dict[str, Any]) -> dict[str, Any]:
    """Enforce the per-type required keys.

    voluptuous cannot express "required only when type is X", and a profile that
    silently resolves to an empty series is far worse than one that refuses to
    load: an empty price series produces a plan, just a meaningless one.
    """
    source_type = source["type"]
    required: tuple[str, ...] = {
        SOURCE_TYPE_ATTRIBUTES: ("entity", "attributes"),
        SOURCE_TYPE_SERVICE: ("service",),
        SOURCE_TYPE_TEMPLATE: ("value",),
    }[source_type]
    if missing := [key for key in required if not source.get(key)]:
        raise vol.Invalid(f"source type '{source_type}' requires: {', '.join(missing)}")
    return source


def _validate_profile(document: dict[str, Any]) -> dict[str, Any]:
    """Cross-field rules that the flat schema cannot express."""
    kind = document["kind"]

    if kind == PROFILE_KIND_INVERTER:
        if not document.get("actions"):
            raise vol.Invalid("inverter profiles require an 'actions' block")
        return document

    # Source profiles either fetch a series themselves, or delegate the forecast
    # to EMHASS's own methods via `emhass:` settings (that is how "user has no
    # price integration at all" stays a first-class configuration). Doing
    # neither means the profile contributes nothing.
    if not document.get("source") and not document.get("emhass"):
        raise vol.Invalid(
            f"'{kind}' profile must define either a 'source' block or an "
            "'emhass' block; it currently contributes nothing"
        )

    if document.get("source"):
        _validate_source(document["source"])
        if document["source"]["type"] != SOURCE_TYPE_TEMPLATE and not document.get("series"):
            raise vol.Invalid("a 'series' mapping is required unless the source type is 'template'")
    return document


PROFILE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required("name"): cv.string,
            vol.Required("kind"): vol.In(PROFILE_KINDS),
            vol.Required("version"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=PROFILE_SCHEMA_VERSION)
            ),
            vol.Optional("description"): cv.string,
            vol.Optional("notes"): cv.string,
            vol.Optional("detect", default={}): DETECT_SCHEMA,
            vol.Optional("options", default={}): vol.Schema({cv.string: OPTION_SCHEMA}),
            # source profiles
            vol.Optional("source"): SOURCE_SCHEMA,
            vol.Optional("series"): SERIES_SCHEMA,
            vol.Optional("unit"): vol.In([UNIT_WATTS, UNIT_CURRENCY_PER_KWH]),
            vol.Optional("emhass", default={}): dict,
            # inverter profiles
            vol.Optional("sensors", default={}): vol.Schema({cv.string: cv.string}),
            vol.Optional("limits", default={}): dict,
            vol.Optional("actions", default={}): vol.Schema(
                {cv.string: vol.All(cv.ensure_list, [ACTION_STEP_SCHEMA])}
            ),
        }
    ),
    _validate_profile,
)


@dataclass(slots=True)
class Profile:
    """A validated profile document."""

    key: str
    """Filename stem. Stable identifier stored in the config entry."""

    path: str
    kind: str
    name: str
    document: dict[str, Any]
    is_builtin: bool = True

    # -- metadata -------------------------------------------------------------

    @property
    def description(self) -> str | None:
        return self.document.get("description")

    @property
    def notes(self) -> str | None:
        return self.document.get("notes")

    @property
    def options(self) -> dict[str, dict[str, Any]]:
        return self.document.get("options", {})

    @property
    def detect(self) -> dict[str, Any]:
        return self.document.get("detect", {})

    @property
    def source(self) -> dict[str, Any] | None:
        return self.document.get("source")

    @property
    def series_map(self) -> dict[str, Any] | None:
        return self.document.get("series")

    @property
    def unit(self) -> str | None:
        return self.document.get("unit")

    @property
    def emhass_settings(self) -> dict[str, Any]:
        return self.document.get("emhass", {})

    @property
    def sensors(self) -> dict[str, str]:
        return self.document.get("sensors", {})

    @property
    def limits(self) -> dict[str, Any]:
        return self.document.get("limits", {})

    @property
    def actions(self) -> dict[str, list[dict[str, Any]]]:
        return self.document.get("actions", {})

    @property
    def produces_series(self) -> bool:
        return self.source is not None

    def selector_schema(self) -> dict[Any, Any]:
        """Build a voluptuous schema fragment for this profile's options."""
        schema: dict[Any, Any] = {}
        for key, option in self.options.items():
            marker = vol.Required if option.get("required", True) else vol.Optional
            if "default" in option:
                field_key = marker(key, default=option["default"])
            else:
                field_key = marker(key)
            schema[field_key] = selector.selector(option["selector"])
        return schema


@dataclass(slots=True)
class ProfileLoadResult:
    """Outcome of scanning the profile directories."""

    profiles: dict[str, Profile] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    """Path -> human-readable reason. Surfaced as a repair, never swallowed."""


def validate_document(document: Any) -> dict[str, Any]:
    """Validate a raw parsed YAML document, raising ProfileError on failure."""
    if not isinstance(document, dict):
        raise ProfileError("profile must be a YAML mapping")
    try:
        return PROFILE_SCHEMA(document)
    except vol.Invalid as err:
        raise ProfileError(humanize_error(err)) from err


def humanize_error(err: vol.Invalid) -> str:
    """Render a voluptuous error in terms a profile author can act on."""
    path = "".join(f"[{part!r}]" for part in err.path)
    location = f" at {path}" if path else ""
    return f"{err.msg}{location}"

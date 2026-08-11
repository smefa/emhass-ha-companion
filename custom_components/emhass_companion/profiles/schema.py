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
    ACTION_ALLOW_GRID_CHARGE,
    ACTION_BLOCK_GRID_CHARGE,
    ACTION_CURTAIL,
    ACTION_UNCURTAIL,
    CONTROL_ARCHETYPES,
    CONTROL_LIFETIMES,
    CURTAIL_MODES,
    DEFAULT_CONTROL,
    INVERTER_ACTIONS,
    MODE_SELF_CONSUME,
    PERCENT_UNITS,
    POWER_UNITS,
    PROFILE_KIND_INVERTER,
    PROFILE_KINDS,
    PROFILE_SCHEMA_VERSION,
    SOURCE_TYPE_ATTRIBUTES,
    SOURCE_TYPE_SERVICE,
    SOURCE_TYPE_TEMPLATE,
    SOURCE_TYPES,
    UNIT_CELSIUS,
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


def _empty_block(schema: Any) -> Any:
    """Treat a null block as an empty one.

    ``limits:`` with every line commented out parses as ``None``, not as an
    empty mapping. That is an ordinary thing to do while writing a profile, and
    being told "expected dict" for it is a poor way to find out.
    """
    return vol.All(lambda value: {} if value is None else value, schema)


OPTION_SCHEMA = vol.Schema(
    {
        vol.Optional("name"): cv.string,
        vol.Optional("description"): cv.string,
        vol.Optional("required", default=True): cv.boolean,
        vol.Optional("default"): vol.Any(cv.string, int, float, bool, list, dict, None),
        # Candidate entity ids, best first. The config flow pre-fills the field
        # with the first one that actually exists, so a user who has already
        # told us which inverter they own normally reads a filled-in form
        # rather than typing entity ids.
        #
        # This is deliberately *not* detection: it runs after the profile has
        # been chosen, so a wrong guess is a visibly wrong entity id in a form
        # field, never a silently wrong control model.
        vol.Optional("suggest", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Required("selector"): _validate_selector,
    }
)

# The semantics of the command, as opposed to its content. `actions:` says what
# to write; this says what the number means and how long the write survives --
# which is what the executor needs in order to decide *when* to write.
CONTROL_SCHEMA = vol.Schema(
    {
        vol.Optional("archetype", default=DEFAULT_CONTROL["archetype"]): vol.In(CONTROL_ARCHETYPES),
        vol.Optional("power_unit", default=DEFAULT_CONTROL["power_unit"]): vol.In(POWER_UNITS),
        # Rated power, required to express a setpoint as a percentage of it.
        # A template, so it can read the inverter's own reported rating.
        vol.Optional("rated_power_w"): vol.Any(vol.Coerce(float), cv.string),
        # True when one value carries direction as well as magnitude. The sign
        # follows EMHASS's convention (positive = discharge) unless inverted.
        vol.Optional("signed", default=DEFAULT_CONTROL["signed"]): cv.boolean,
        # For hardware that calls charging positive (SolarEdge's battery power,
        # Victron's grid setpoint) rather than EMHASS's discharge-positive.
        vol.Optional("invert_sign", default=DEFAULT_CONTROL["invert_sign"]): cv.boolean,
        # Multiplier applied to charge magnitude only, to cover conversion loss
        # between what is asked for and what reaches the cells.
        vol.Optional("charge_boost", default=DEFAULT_CONTROL["charge_boost"]): vol.All(
            vol.Coerce(float), vol.Range(min=0.5, max=2.0)
        ),
        vol.Optional("round_to", default=DEFAULT_CONTROL["round_to"]): vol.All(
            vol.Coerce(float), vol.Range(min=0)
        ),
        vol.Optional("lifetime", default=DEFAULT_CONTROL["lifetime"]): vol.In(CONTROL_LIFETIMES),
        vol.Optional("duration_min", default=DEFAULT_CONTROL["duration_min"]): vol.All(
            vol.Coerce(float), vol.Range(min=1)
        ),
        vol.Optional("deadband_w", default=DEFAULT_CONTROL["deadband_w"]): vol.All(
            vol.Coerce(float), vol.Range(min=0)
        ),
        # A floor on how often this profile may write at all, for buses that do
        # not tolerate being hammered.
        vol.Optional(
            "min_write_interval_s", default=DEFAULT_CONTROL["min_write_interval_s"]
        ): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional("restore_required", default=DEFAULT_CONTROL["restore_required"]): cv.boolean,
        # What a `curtail`/`uncurtail` write targets, and in what unit --
        # separate from `power_unit` because the register a curtailment limit
        # lives in is frequently not the same one the battery setpoint does.
        vol.Optional("curtail_mode", default=DEFAULT_CONTROL["curtail_mode"]): vol.In(
            CURTAIL_MODES
        ),
        vol.Optional("curtail_unit", default=DEFAULT_CONTROL["curtail_unit"]): vol.In(POWER_UNITS),
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


def _validate_inverter(document: dict[str, Any]) -> dict[str, Any]:
    """Cross-field rules for an inverter profile.

    Every rule here exists because breaking it produces a write to real
    hardware that is wrong rather than absent -- a percentage sent as watts, a
    forced charge that outlives its plan, a battery left in forced mode because
    nothing could hand control back.
    """
    if not document.get("actions"):
        raise vol.Invalid("inverter profiles require an 'actions' block")

    if unknown := sorted(set(document["actions"]) - set(INVERTER_ACTIONS)):
        raise vol.Invalid(
            f"unknown action(s): {', '.join(unknown)}; "
            f"the defined set is: {', '.join(sorted(INVERTER_ACTIONS))}"
        )

    # A version 1 profile predates `control:` entirely. Giving it the defaults
    # reproduces the behaviour it had before the block existed, rather than
    # forcing a migration on profiles that already work.
    control = {**DEFAULT_CONTROL, **(document.get("control") or {})}
    document["control"] = control

    if control["power_unit"] in PERCENT_UNITS and not control.get("rated_power_w"):
        raise vol.Invalid(
            f"power_unit '{control['power_unit']}' expresses the setpoint as a "
            "proportion of the inverter's rating, so 'rated_power_w' is required "
            "-- without it there is nothing to take a percentage of"
        )

    # Restoring is the only thing standing between a persistent-register
    # inverter and being left in forced mode indefinitely.
    actions = document["actions"]
    if control["restore_required"] and not (
        actions.get("restore") or actions.get(MODE_SELF_CONSUME)
    ):
        raise vol.Invalid(
            "restore_required is set, so this profile must define either a "
            "'restore' or a 'self_consume' action; without one nothing can hand "
            "control back to the inverter on shutdown"
        )

    # Anything that can be switched on must be switchable off. An export limit
    # left in place because a profile defines `curtail` but not `uncurtail` is
    # the same failure mode as a battery with no `restore` -- silent, and it
    # costs money instead of just looking wrong.
    for on_action, off_action in (
        (ACTION_CURTAIL, ACTION_UNCURTAIL),
        (ACTION_ALLOW_GRID_CHARGE, ACTION_BLOCK_GRID_CHARGE),
    ):
        has_on, has_off = bool(actions.get(on_action)), bool(actions.get(off_action))
        if has_on != has_off:
            defined, missing = (on_action, off_action) if has_on else (off_action, on_action)
            raise vol.Invalid(
                f"profile defines '{defined}' but not '{missing}' -- anything this "
                "profile can turn on must be definable to turn back off"
            )

    return document


def _validate_profile(document: dict[str, Any]) -> dict[str, Any]:
    """Cross-field rules that the flat schema cannot express."""
    kind = document["kind"]

    if kind == PROFILE_KIND_INVERTER:
        return _validate_inverter(document)

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
            # Inverter metadata. Display only -- `brand` groups the picker and
            # `requires` names the integration the profile needs, which is the
            # only defence against someone choosing their brand when what they
            # have installed is the read-only cloud integration of the same name.
            vol.Optional("brand"): cv.string,
            vol.Optional("models", default=[]): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional("requires"): cv.string,
            vol.Optional("docs"): cv.string,
            # Set on a profile written from the integration's source and the
            # inverter's register map, but never run against the hardware. It
            # is carried into the picker label and the top of the setup form,
            # because the alternative -- a profile that looks exactly as
            # trustworthy as a validated one -- makes the first person to try
            # it an unwitting tester of writes to their own battery.
            vol.Optional("untested", default=False): cv.boolean,
            # Inverter profiles are always offered; the user picks their own
            # hardware. `detect` remains for source profiles, where "is Solcast
            # installed" is a question the integration domain genuinely answers.
            vol.Optional("detect", default={}): DETECT_SCHEMA,
            vol.Optional("options", default={}): vol.Schema({cv.string: OPTION_SCHEMA}),
            # source profiles
            vol.Optional("source"): SOURCE_SCHEMA,
            vol.Optional("series"): SERIES_SCHEMA,
            vol.Optional("unit"): vol.In([UNIT_WATTS, UNIT_CURRENCY_PER_KWH, UNIT_CELSIUS]),
            # `_empty_block` rather than a bare `dict` throughout: commenting
            # out every line of a block leaves the key present with a null
            # value, which is a likely thing for a profile author to do and a
            # baffling thing to be told is "expected dict".
            vol.Optional("emhass", default={}): _empty_block(dict),
            # inverter profiles
            vol.Optional("control", default={}): _empty_block(CONTROL_SCHEMA),
            vol.Optional("sensors", default={}): _empty_block(vol.Schema({cv.string: cv.string})),
            vol.Optional("limits", default={}): _empty_block(dict),
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
    def brand(self) -> str | None:
        return self.document.get("brand")

    @property
    def models(self) -> list[str]:
        return self.document.get("models", [])

    @property
    def requires(self) -> str | None:
        """The integration this profile needs, shown next to its name."""
        return self.document.get("requires")

    @property
    def untested(self) -> bool:
        """Whether this profile has never been run against real hardware.

        Written from the integration's source and the register map, but not
        confirmed by anyone who owns the inverter. Surfaced in the picker and
        on the setup form so that trying it is a choice rather than a surprise.
        """
        return bool(self.document.get("untested"))

    @property
    def control(self) -> dict[str, Any]:
        """Command semantics, with every default filled in."""
        return {**DEFAULT_CONTROL, **self.document.get("control", {})}

    def defines(self, action: str) -> bool:
        """Whether this profile can perform ``action``.

        The set of actions a profile defines is its capability list -- a
        profile with no ``force_charge`` is saying its hardware cannot be
        commanded to charge at a given power, which the executor reports rather
        than papers over.
        """
        return bool(self.document.get("actions", {}).get(action))

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

    def selector_schema(self, *, skip: set[str] = frozenset()) -> dict[Any, Any]:
        """Build a voluptuous schema fragment for this profile's options.

        ``skip`` omits options resolved elsewhere in the flow -- for example a
        "load/sensor" entity that a create-a-sensor flow builds dynamically
        rather than asking the user to pick directly.
        """
        schema: dict[Any, Any] = {}
        for key, option in self.options.items():
            if key in skip:
                continue
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

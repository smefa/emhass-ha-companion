"""Config-flow form helpers shared by the schema builders and the profile engine.

Every form keeps its rarely-touched settings in one collapsed section, under the
same key, so that translations and the helpers below are uniform.
"""

from __future__ import annotations

from typing import Any

from homeassistant.data_entry_flow import section
import voluptuous as vol

from .const import ADVANCED_SECTION


def with_advanced(
    basic: dict[Any, Any], advanced: dict[Any, Any], *, collapsed: bool = True
) -> dict[Any, Any]:
    """`basic` plus one collapsed Advanced section holding `advanced`.

    Returns `basic` unchanged when there is nothing to put in the section, so
    a form never shows an empty one. Every field in `advanced` must still be
    valid untouched (an optional field, or one with a default): a collapsed
    section the user never opens submits only its defaults.
    """
    if not advanced:
        return basic
    return {
        **basic,
        vol.Required(ADVANCED_SECTION): section(vol.Schema(advanced), {"collapsed": collapsed}),
    }


def flatten_sections(user_input: dict[str, Any]) -> dict[str, Any]:
    """Merge the Advanced section's fields into the top level.

    Call first in every handler whose form has one, so everything downstream
    sees the flat dict it always did and stored options keep their shape.
    """
    flat = {key: value for key, value in user_input.items() if key != ADVANCED_SECTION}
    flat.update(user_input.get(ADVANCED_SECTION) or {})
    return flat


def nest_suggested(values: dict[str, Any], advanced_keys: set[str]) -> dict[str, Any]:
    """Suggested values for a form with an Advanced section: move its keys under it."""
    nested = {key: value for key, value in values.items() if key not in advanced_keys}
    inner = {key: value for key, value in values.items() if key in advanced_keys}
    if inner:
        nested[ADVANCED_SECTION] = inner
    return nested

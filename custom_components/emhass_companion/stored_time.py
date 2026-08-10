"""Tolerant parsing of the times of day held in the config entry store.

``time.fromisoformat`` raises on anything it does not recognise, and both places
that call it -- the day-ahead fallback time in :mod:`configuration`, and a
load's window and comfort band in :mod:`deferrable` -- run inside
``async_setup_entry``. A hand-edited ``.storage``, a restored backup or a future
schema change would therefore take the whole integration down with a traceback
naming neither the field nor the load, which is the one failure mode nothing
else in this integration has: everything else degrades to a default and raises a
repair the user can act on. This module is that treatment for stored times.
"""

from __future__ import annotations

from datetime import time
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, ISSUE_BAD_STORED_TIME

_LOGGER = logging.getLogger(__name__)

DATA_BAD_STORED_TIMES = f"{DOMAIN}_bad_stored_times"
"""``hass.data`` key: label -> the raw value that would not parse."""


@callback
def parse_stored_time(
    hass: HomeAssistant,
    raw: Any,
    *,
    label: str,
    default: time | None = None,
) -> time | None:
    """``time.fromisoformat``, degrading to ``default`` instead of raising.

    ``label`` names the field in the repair issue -- "Day-ahead fallback time",
    or "Dishwasher: earliest start" -- since the stored value alone ("25:00")
    tells the user nothing about where to go and fix it.

    An absent value (None or empty) is not corruption: it means "not set", and
    returns ``default`` without a repair.
    """
    if raw is None or raw == "":
        _forget(hass, label)
        return default
    if isinstance(raw, time):
        _forget(hass, label)
        return raw
    try:
        parsed = time.fromisoformat(str(raw))
    except (TypeError, ValueError):
        _LOGGER.warning(
            "Ignoring unreadable stored time %r for %s; using %s instead",
            raw,
            label,
            default.isoformat() if default else "no value",
        )
        _remember(hass, label, raw)
        return default
    _forget(hass, label)
    return parsed


@callback
def _remember(hass: HomeAssistant, label: str, raw: Any) -> None:
    offenders: dict[str, str] = hass.data.setdefault(DATA_BAD_STORED_TIMES, {})
    offenders[label] = str(raw)
    _refresh_issue(hass, offenders)


@callback
def _forget(hass: HomeAssistant, label: str) -> None:
    """Drop ``label`` from the repair once it parses again.

    Keyed by label rather than appended to, so re-reading the same field after
    the user fixes it clears that entry and leaves any other offender in place.
    """
    offenders: dict[str, str] | None = hass.data.get(DATA_BAD_STORED_TIMES)
    if not offenders or offenders.pop(label, None) is None:
        return
    _refresh_issue(hass, offenders)


@callback
def _refresh_issue(hass: HomeAssistant, offenders: dict[str, str]) -> None:
    if not offenders:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_BAD_STORED_TIME)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_BAD_STORED_TIME,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_BAD_STORED_TIME,
        translation_placeholders={
            "count": str(len(offenders)),
            "details": "\n".join(
                f"- **{label}**: `{value}`" for label, value in sorted(offenders.items())
            ),
        },
    )

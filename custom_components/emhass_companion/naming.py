"""EMHASS-standard entity ids for the plan sensors.

Copyright (C) 2026 Tomas Smedberg.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. It is distributed without any warranty; see the LICENSE file at
the repository root for the full terms.

Everything here works on the entity registry rather than on the entity
classes. Overriding ``Entity.suggested_object_id`` looks like the obvious lever
and is the wrong one: ``entity_platform._async_derive_object_ids`` only treats
an object id as a true suggestion when it arrives via
``internal_integration_suggested_object_id``, and otherwise feeds it through as
an *object id base*, which the registry then prefixes with the device name --
producing ``sensor.emhass_companion_p_pv_forecast``, not the id EMHASS uses.
Renaming the registry entry after the fact sidesteps that entirely, works the
same on a first install as on a later toggle, and is the only one of the two
that can be undone.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_EMHASS_STANDARD_NAMES,
    CONF_ENTITY_IDS_BEFORE_STANDARD,
    DEFAULT_STANDARD_SERIES_DECIMALS,
    DOMAIN,
    EMHASS_STANDARD_OBJECT_IDS,
    EMHASS_STANDARD_SERIES_ATTRIBUTES,
    EMHASS_STANDARD_SERIES_DECIMALS,
    ISSUE_STANDARD_NAMES_TAKEN,
)

_LOGGER = logging.getLogger(__name__)


@callback
def standard_names_enabled(entry: ConfigEntry) -> bool:
    """Whether this entry publishes under EMHASS's own entity ids."""
    return bool(entry.options.get(CONF_EMHASS_STANDARD_NAMES))


@callback
def async_taken_standard_ids(hass: HomeAssistant, entry: ConfigEntry | None = None) -> list[str]:
    """EMHASS-standard ids that already belong to something else.

    Used twice: by the config flow, to offer the option only to someone who
    demonstrably has EMHASS entities already (and to warn them when those are
    the add-on's own live sensors), and by :func:`async_apply_standard_names`,
    to skip a rename the registry would refuse anyway.

    Entities belonging to *this* entry are not conflicts -- they are the very
    ones about to be renamed.
    """
    registry = er.async_get(hass)
    ours = {
        registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
        for key in EMHASS_STANDARD_OBJECT_IDS
        if entry is not None
    }
    taken = []
    for object_id in EMHASS_STANDARD_OBJECT_IDS.values():
        entity_id = f"sensor.{object_id}"
        if entity_id in ours:
            continue
        if registry.async_get(entity_id) is not None or hass.states.get(entity_id) is not None:
            # States as well as the registry: EMHASS's publish-data posts
            # straight to the state machine over the REST API, so its sensors
            # exist as states with no registry entry at all. Those still
            # collide -- two writers on one entity id, each overwriting the
            # other -- and are in fact the likeliest conflict of the two.
            taken.append(entity_id)
    return taken


async def async_apply_standard_names(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Bring the plan sensors' entity ids in line with the current option.

    Called from ``async_setup_entry`` once the platforms are up, so that every
    sensor this configuration actually has is already registered and can be
    found by unique id. Doing it earlier would mean reserving ids blind, and
    reserving one for a sensor that this house never gets -- the battery ones,
    on a house with no battery -- leaves an orphan registry entry behind.

    Both directions are handled: turning the option off restores the ids
    recorded when it was turned on.
    """
    registry = er.async_get(hass)
    wanted = standard_names_enabled(entry)
    originals: dict[str, str] = dict(entry.options.get(CONF_ENTITY_IDS_BEFORE_STANDARD) or {})
    before = dict(originals)
    conflicts: list[str] = []
    occupied = set(async_taken_standard_ids(hass, entry))

    for key, object_id in EMHASS_STANDARD_OBJECT_IDS.items():
        standard_id = f"sensor.{object_id}"
        current = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")

        if current is None:
            # Not every sensor exists in every configuration -- the battery
            # ones are only added when there is a battery. Reserving the id
            # anyway would leave an orphan registry entry showing as an
            # unavailable entity forever, so a sensor that is not there is
            # simply not renamed.
            continue

        target = standard_id if wanted else originals.get(key)
        if not target or target == current:
            continue
        if wanted and standard_id in occupied:
            conflicts.append(standard_id)
            continue
        if not wanted and registry.async_get(target) is not None:
            # Something claimed the old id while the option was on. Leave the
            # sensor where it is rather than fail setup; the user keeps a
            # working entity, just not the id they started with.
            _LOGGER.warning(
                "Cannot restore %s to %s: that entity id is taken; leaving it as %s",
                key,
                target,
                current,
            )
            continue

        registry.async_update_entity(current, new_entity_id=target)
        _LOGGER.debug("Renamed %s to %s", current, target)
        if wanted:
            # Recorded only on the way in, and only once: a second pass with
            # the option still on must not overwrite the original with the
            # standard id it was already renamed to.
            originals.setdefault(key, current)
        else:
            originals.pop(key, None)

    if originals != before:
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, CONF_ENTITY_IDS_BEFORE_STANDARD: originals},
        )

    _report_conflicts(hass, conflicts)


@callback
def _report_conflicts(hass: HomeAssistant, conflicts: list[str]) -> None:
    """Name the ids that could not be taken, or clear the issue once they can."""
    if not conflicts:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_STANDARD_NAMES_TAKEN)
        return

    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_STANDARD_NAMES_TAKEN,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_STANDARD_NAMES_TAKEN,
        translation_placeholders={
            "count": str(len(conflicts)),
            "details": "\n".join(f"- `{entity_id}`" for entity_id in sorted(conflicts)),
        },
    )


@callback
def standard_series_attribute(key: str) -> tuple[str, str, int] | None:
    """EMHASS's attribute name, value key and rounding for a sensor key.

    Returns None for the sensors EMHASS publishes without a series
    (``optim_status``, ``total_cost_fun_value``).
    """
    attribute = EMHASS_STANDARD_SERIES_ATTRIBUTES.get(key)
    if attribute is None:
        return None
    return (
        attribute,
        EMHASS_STANDARD_OBJECT_IDS[key],
        EMHASS_STANDARD_SERIES_DECIMALS.get(key, DEFAULT_STANDARD_SERIES_DECIMALS),
    )


def emhass_series_payload(points: list[tuple[str, float]], value_key: str, decimals: int) -> Any:
    """Serialise a series the way EMHASS does.

    Values are strings, not floats: EMHASS builds the list with
    ``str(np.round(...))`` and consumers written against it parse accordingly.
    """
    return [
        {"date": timestamp, value_key: str(round(value, decimals))} for timestamp, value in points
    ]

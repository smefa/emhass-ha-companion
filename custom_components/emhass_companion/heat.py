"""EMHASS's heat topology, as far as the Companion needs to know about it.

``heat_topology`` describes a heating plant (heat pumps, boilers, tanks, a house
zone) as a graph that EMHASS compiles into deferrable loads. By default the
compiler *replaces* the whole deferrable-load set, which would silently delete
every load the Companion sends. Its ``extend_deferrable_loads`` flag appends the
topology's loads after the caller's instead.
"""

from __future__ import annotations

import json
from typing import Any

from .const import EMHASS_CONF_HEAT_TOPOLOGY

EXTEND_FLAG = "extend_deferrable_loads"


def _as_topology(raw: Any) -> dict[str, Any] | None:
    """The topology as a dict, or None when none is configured.

    EMHASS's configuration page stores the field as a JSON object, but accepts
    ``null``, an empty string and the string ``"null"`` as "disabled"; a JSON
    string is read here too, so a value that arrived quoted is not missed.
    """
    if isinstance(raw, str):
        text = raw.strip()
        if text in ("", "null"):
            return None
        try:
            raw = json.loads(text)
        except ValueError:
            return None
    if isinstance(raw, dict) and raw:
        return raw
    return None


def topology_replaces_loads(config: dict[str, Any], *, extend_supported: bool = True) -> bool:
    """Whether the stored EMHASS config holds a topology in replace mode.

    Replace mode overwrites ``number_of_deferrable_loads``, ``def_load_config``
    and every per-load array with the compiled ones, whatever a run sends.
    ``extend_supported`` is False for a backend known to predate the extend
    flag: it does not read it, so the flag changes nothing there.
    """
    topology = _as_topology(config.get(EMHASS_CONF_HEAT_TOPOLOGY))
    if topology is None:
        return False
    return not (extend_supported and topology.get(EXTEND_FLAG, False))

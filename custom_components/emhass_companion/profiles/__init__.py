"""Profile discovery, validation and lookup.

Profiles are loaded from two places:

``custom_components/emhass_companion/profiles/builtin/<kind>/``
    Shipped with the integration. Replaced wholesale on every update.

``<config>/emhass_companion/profiles/<kind>/``
    The user's own. Deliberately outside the integration directory, because a
    HACS update overwrites that directory -- anything a user put there would be
    silently deleted. A user file whose filename matches a built-in *replaces*
    it, so a broken built-in can be patched locally without waiting for a
    release.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.yaml import load_yaml

from ..const import PROFILE_KINDS, USER_PROFILE_DIR
from .engine import (
    async_execute_steps,
    async_resolve_series,
    render,
    render_action,
    resolve_limits,
    resolve_sensors,
    resolve_settings,
)
from .schema import Profile, ProfileError, ProfileLoadResult, validate_document

__all__ = [
    "Profile",
    "ProfileError",
    "ProfileLoadResult",
    "async_execute_steps",
    "async_load_profiles",
    "async_resolve_series",
    "available_profiles",
    "render",
    "render_action",
    "resolve_limits",
    "resolve_sensors",
    "resolve_settings",
]

_LOGGER = logging.getLogger(__name__)

BUILTIN_ROOT = Path(__file__).parent / "builtin"


async def async_load_profiles(hass: HomeAssistant) -> ProfileLoadResult:
    """Load every profile from disk.

    A malformed profile never blocks setup: it is collected into
    ``result.errors`` and surfaced as a repair issue naming the file and the
    offending key, while the remaining profiles load normally.
    """
    user_root = Path(hass.config.path(USER_PROFILE_DIR))
    return await hass.async_add_executor_job(_load_profiles, user_root)


def _load_profiles(user_root: Path) -> ProfileLoadResult:
    result = ProfileLoadResult()

    for kind in PROFILE_KINDS:
        # Built-ins first, then user files, so a matching filename overrides.
        for root, is_builtin in ((BUILTIN_ROOT / kind, True), (user_root / kind, False)):
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*.yaml")):
                _load_one(path, kind, is_builtin, result)

    return result


def _load_one(path: Path, kind: str, is_builtin: bool, result: ProfileLoadResult) -> None:
    try:
        raw = load_yaml(str(path))
    except (HomeAssistantError, OSError) as err:
        result.errors[str(path)] = f"could not be read: {err}"
        return

    try:
        document = validate_document(raw)
    except ProfileError as err:
        result.errors[str(path)] = str(err)
        return

    if document["kind"] != kind:
        result.errors[str(path)] = (
            f"declares kind '{document['kind']}' but lives in the '{kind}' directory"
        )
        return

    key = f"{kind}/{path.stem}"
    result.profiles[key] = Profile(
        key=key,
        path=str(path),
        kind=kind,
        name=document["name"],
        document=document,
        is_builtin=is_builtin,
    )


def available_profiles(
    hass: HomeAssistant, profiles: dict[str, Profile], kind: str
) -> list[Profile]:
    """Profiles of ``kind`` whose backing integration is actually present.

    This filtering is the point of the ``detect`` block: a user is only ever
    offered sources they can actually use.
    """
    return [
        profile
        for profile in profiles.values()
        if profile.kind == kind and _is_detected(hass, profile)
    ]


def _is_detected(hass: HomeAssistant, profile: Profile) -> bool:
    detect: dict[str, Any] = profile.detect
    if not detect or detect.get("always"):
        return True

    candidates: list[str] = []
    if single := detect.get("integration"):
        candidates.append(single)
    candidates.extend(detect.get("any_integration") or [])

    if not candidates:
        return True
    return any(_integration_loaded(hass, domain) for domain in candidates)


def _integration_loaded(hass: HomeAssistant, domain: str) -> bool:
    if domain in hass.config.components:
        return True
    return bool(hass.config_entries.async_entries(domain))

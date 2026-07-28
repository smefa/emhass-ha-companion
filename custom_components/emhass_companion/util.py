"""Small shared helpers."""

from __future__ import annotations

from awesomeversion import AwesomeVersion, AwesomeVersionException


def version_at_least(candidate: str, minimum: str) -> bool:
    """Whether ``candidate`` is at least ``minimum``.

    EMHASS reports its version from package metadata, which occasionally carries
    a development or local suffix. An unparseable version is treated as
    acceptable: refusing to start because a developer build has an odd version
    string would be worse than trying and reporting a real error later.
    """
    try:
        return AwesomeVersion(candidate) >= AwesomeVersion(minimum)
    except AwesomeVersionException:
        return True


def schema_major(version: str | None) -> int | None:
    """Major component of an EMHASS plan schema version, if parseable."""
    if not version:
        return None
    try:
        return int(str(version).split(".")[0])
    except (ValueError, IndexError):
        return None

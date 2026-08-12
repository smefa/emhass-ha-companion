"""Diagnostics for support requests.

The bundle produced here is meant to answer, in one download, the questions a
maintainer would otherwise spend several round-trips on with a stranger who
just opened an issue: is the add-on even reachable, does every entity id the
config points at actually exist, and what did the integration itself log
while deciding what it decided. Every new section added for that is
independently guarded -- a down add-on or a missing entity is exactly the
kind of thing this bundle exists to report, so it must show up as a finding
in the download rather than as a stack trace that produces no download at
all. See docs/support_bundle_plan.md for the reasoning behind each section.

``last_payload`` and every other key that existed before this file grew a
``triage`` section stay exactly where they were: ``scripts/check_infeasibility.py``
and its tests read ``last_payload`` off the top level of whatever this
returns, and that script is already sitting on disks this repo does not
control.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
import re
import sys
from typing import Any, Final

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_integration
from yarl import URL

from .const import CONF_URL, DOMAIN, ML_MIN_HISTORY_DAYS, USER_PROFILE_DIR
from .coordinator import EmhassCoordinator

# A down add-on must produce a finding, not a 30s hang -- api.py's own
# READ_TIMEOUT is sized for a slow solve, not for a diagnostics download
# someone is waiting on in a browser tab.
_PROBE_TIMEOUT = 5

# EMHASS's config carries latitude/longitude/altitude, and nothing in this
# integration's own options schema currently stores a credential -- but a
# user-authored profile or a future EMHASS release could add one, so the key
# is matched by pattern rather than enumerated by name.
_REDACT_EXACT_KEYS: Final = {"latitude", "longitude", "alt", "altitude"}
_REDACT_KEY_PATTERN: Final = re.compile(r"password|token|api_key|secret|authorization", re.I)

# `key: value` in a profile YAML, matched textually rather than through the
# parser -- see _redact_secret_lines for why. The key is captured separately
# so the same pattern used everywhere else decides whether it is a credential.
_SECRET_LINE_RE: Final = re.compile(
    r"^(?P<prefix>\s*(?:-\s+)?[\"']?(?P<key>[^\s:\"']+)[\"']?\s*:\s*)(?P<value>\S.*)?$"
)

# `key: |`, `key: >-`, ... -- the value is not on this line but in the more
# indented block beneath it, which has to be redacted too.
_BLOCK_SCALAR_RE: Final = re.compile(r"^[|>][0-9+-]*\s*$")

# Deliberately the same simple shape the plan calls out rather than
# homeassistant.helpers.config_validation's entity-id validator: this walks
# arbitrary profile_options blobs whose schema this module does not know, and
# a local pattern is robust to that in a way importing a stricter validator
# would not be.
_ENTITY_ID_RE = re.compile(r"[a-z_]+\.[a-z0-9_]+")

# Large enough for any hand-written profile, small enough that one runaway
# file cannot balloon the bundle.
_MAX_PROFILE_BYTES = 64 * 1024


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: EmhassCoordinator = entry.runtime_data.coordinator
    data = coordinator.data
    config = coordinator.config

    entities_section = _entities_section(hass, entry)
    environment_section = await _async_environment_section(hass)
    backend_section = await _async_backend_section(hass, entry)
    subentries_section = _subentries_section(entry)
    custom_profiles_section = await _async_custom_profiles_section(hass)
    logs_section = _logs_section(entry)
    triage_section = _triage_section(coordinator, backend_section, entities_section)

    return {
        "entry": {
            "options": _redact_config(entry.options),
            "subentry_count": len(entry.subentries),
        },
        "profiles": {
            "loaded": {
                key: {"name": profile.name, "builtin": profile.is_builtin}
                for key, profile in sorted(coordinator.profiles.items())
            },
            "errors": coordinator.profile_errors,
            "selected": {
                "price": config.price.key,
                "pv": config.pv.key,
                "load": config.load.key,
            },
        },
        "last_run": {
            "status": data.last_run.status if data.last_run else None,
            "action": data.last_run.action if data.last_run else None,
            "infeasible": data.last_run.infeasible if data.last_run else None,
            "emhass_version": data.last_run.emhass_version if data.last_run else None,
            "schema_version": data.last_run.schema_version if data.last_run else None,
            "error_message": data.last_run.error_message if data.last_run else None,
        },
        "plan": {
            "rows": len(data.plan.rows) if data.plan else 0,
            "generated_at": (data.plan.generated_at.isoformat() if data.plan else None),
            "schema_version": data.plan.schema_version if data.plan else None,
        },
        "series": {
            name: {"points": len(series), "end": series.end.isoformat() if series else None}
            for name, series in (
                ("buy_price", data.buy_price),
                ("sell_price", data.sell_price),
                ("pv_forecast", data.pv_forecast),
                ("load_forecast", data.load_forecast),
            )
        },
        # The whole day's ledger, not just the published headline. Almost every
        # "this savings number looks wrong" report is answered by one of the
        # inputs -- an unpriced hour after a restart, a meter that resolved to
        # a power sensor rather than a counter, a balance residual saying two
        # of the user's own sensors disagree -- and none of those are visible
        # from the sensor's state alone.
        "savings": _savings_section(entry),
        "end_soc": (
            {
                "soc": data.end_soc.soc,
                "reason": data.end_soc.reason,
                "details": data.end_soc.details,
            }
            if data.end_soc
            else None
        ),
        # The exact request is the single most useful artefact when a plan looks
        # wrong, since EMHASS's own configuration screen does not reflect it.
        # Redacted despite that: build_payload ends by merging in the `emhass:`
        # block of every selected profile, which is an unconstrained mapping, so
        # a credential a profile sets for EMHASS (`solcast_api_key`, say) lands
        # here -- and would otherwise survive in plain text a few lines above
        # the same key being redacted out of `backend.config`.
        "last_payload": _redact_config(data.payload or {}),
        "warnings": data.warnings,
        "deferrable_order": data.load_order,
        "environment": environment_section,
        "backend": backend_section,
        "subentries": subentries_section,
        "entities": entities_section,
        "custom_profiles": custom_profiles_section,
        "logs": logs_section,
        "triage": triage_section,
    }


# -- redaction ------------------------------------------------------------


def _savings_section(entry: ConfigEntry) -> dict[str, Any]:
    """The day's ledger, its sources, and the forecast built from the plan.

    Entity ids only, never redacted: they are the user's own sensor names and
    are already all over the rest of this bundle, and which meter answered for
    which quantity is the single most useful thing here.
    """
    tracker = getattr(entry.runtime_data, "tracker", None)
    if tracker is None:
        return {"configured": False}

    forecast = tracker.forecast()
    return {
        "configured": tracker.meters.usable,
        "sources": tracker.meters.describe(),
        "has_solar": tracker.meters.has_solar,
        "has_battery": tracker.meters.has_battery,
        "ledger": tracker.ledger.as_dict(),
        "derived": {
            "solar_savings": tracker.ledger.solar_savings,
            "battery_savings": tracker.ledger.battery_savings,
            "total_savings": tracker.ledger.total_savings,
            "storage_carry": tracker.ledger.storage_carry,
            "average_import_price": tracker.ledger.average_import_price,
            "average_export_price": tracker.ledger.average_export_price,
            "average_charge_price": tracker.ledger.average_charge_price,
            "average_discharge_price": tracker.ledger.average_discharge_price,
        },
        "forecast_24h": None
        if forecast is None
        else {
            "hours": forecast.hours,
            "complete": forecast.complete,
            "actual_cost": forecast.actual_cost,
            "solar_only_cost": forecast.solar_only_cost,
            "grid_only_cost": forecast.grid_only_cost,
            "storage_carry": forecast.storage_carry,
            "total_savings": forecast.total_savings,
        },
    }


def _keys_matching_pattern(value: Any) -> set[str]:
    """Every key anywhere in ``value`` that looks like a credential.

    ``async_redact_data`` only matches an exact set of keys, not a pattern --
    so the set handed to it below is assembled by walking the data first,
    then redacted through the same helper every other integration's
    diagnostics uses.
    """
    keys: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and _REDACT_KEY_PATTERN.search(key):
                keys.add(key)
            keys |= _keys_matching_pattern(item)
    elif isinstance(value, list):
        for item in value:
            keys |= _keys_matching_pattern(item)
    return keys


def _redact_config(data: Mapping[str, Any]) -> dict[str, Any]:
    """Redact coordinates and anything that looks like a credential.

    Deliberately not applied to entity ids, tariff prices, battery capacity or
    load names -- redacting those would defeat the feature. The payload *is*
    run through this, unlike when this module was written: it is assembled
    partly from profile-contributed EMHASS settings, so "nothing in it is a
    secret" stopped being true once profiles could name any key they liked.
    """
    return async_redact_data(data, _REDACT_EXACT_KEYS | _keys_matching_pattern(data))


def _scrub_url(url: str) -> str:
    """Scheme, host and port only.

    EMHASS itself exposes no authentication of its own, but a reverse proxy
    put in front of it can add userinfo or a query-string token, and this is
    meant to be safe to attach to a public issue -- so neither survives here.
    The port does: "wrong port" is a real, common misconfiguration and needs
    to stay visible.
    """
    parsed = URL(url)
    return str(parsed.with_user(None).with_password(None).with_query(None).with_fragment(None))


# -- environment ------------------------------------------------------------


async def _async_environment_section(hass: HomeAssistant) -> dict[str, Any]:
    try:
        integration = await async_get_integration(hass, DOMAIN)
        integration_version = str(integration.version) if integration.version else None
    except Exception as err:  # noqa: BLE001 - a lookup failure is itself a finding
        integration_version = f"error: {err}"

    return {
        "integration_version": integration_version,
        "home_assistant_version": HA_VERSION,
        "python_version": sys.version,
        "time_zone": hass.config.time_zone,
        # UnitSystem exposes no public name accessor ("metric"/"us_customary");
        # the private field is the same one Config.as_dict() would have to
        # reach for too, just without the verbose per-quantity detail
        # units.as_dict() would otherwise add for a fact this only needs one
        # word to state.
        "unit_system": getattr(hass.config.units, "_name", None),
        "hacs": "hacs" in hass.config.components,
    }


# -- backend ------------------------------------------------------------


async def _async_backend_source(hass: HomeAssistant, configured_url: str) -> str:
    """Best-effort guess at how the URL got here: discovered, or typed in.

    Nothing persists this choice -- config_flow only ever *suggests* the
    discovered add-on URL as the form's default, it never records whether a
    user accepted it or overwrote it. Re-running that same discovery at dump
    time and comparing it against what is actually configured is the closest
    approximation available without adding a new stored field to every
    existing config entry.
    """
    try:
        from .config_flow import _async_addon_url

        discovered = await asyncio.wait_for(_async_addon_url(hass), timeout=_PROBE_TIMEOUT)
    except Exception:  # noqa: BLE001 - best-effort only, never a finding of its own
        return "typed by hand"
    if discovered and discovered.rstrip("/") == configured_url.rstrip("/"):
        return "add-on discovery"
    return "typed by hand"


async def _async_backend_section(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator: EmhassCoordinator = entry.runtime_data.coordinator
    configured_url = entry.data.get(CONF_URL, "")

    try:
        url = _scrub_url(configured_url) if configured_url else None
    except Exception as err:  # noqa: BLE001 - a malformed stored URL is itself a finding
        url = f"error: {err}"

    section: dict[str, Any] = {
        "url": url,
        "source": await _async_backend_source(hass, configured_url),
        "version": None,
        "config": None,
    }

    try:
        section["version"] = await asyncio.wait_for(
            coordinator.client.async_get_version(), timeout=_PROBE_TIMEOUT
        )
    except Exception as err:  # noqa: BLE001 - a down add-on must produce a finding, not a hang
        section["version_error"] = str(err)

    try:
        config = await asyncio.wait_for(
            coordinator.client.async_get_config(), timeout=_PROBE_TIMEOUT
        )
        section["config"] = _redact_config(config)
    except Exception as err:  # noqa: BLE001
        section["config_error"] = str(err)

    return section


# -- subentries ------------------------------------------------------------


def _subentries_section(entry: ConfigEntry) -> list[dict[str, Any]]:
    try:
        items = [
            {
                "subentry_id": subentry_id,
                "subentry_type": subentry.subentry_type,
                "title": subentry.title,
                # Entity ids stay in plain text here -- they are not secrets
                # and they are the whole point of a support bundle.
                "data": _redact_config(subentry.data),
            }
            for subentry_id, subentry in entry.subentries.items()
        ]
    except Exception as err:  # noqa: BLE001
        return [{"error": str(err)}]
    return sorted(items, key=lambda item: (item["subentry_type"], item["title"]))


# -- entity health ------------------------------------------------------------


def _walk_entity_candidates(value: Any, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        if _ENTITY_ID_RE.fullmatch(value):
            yield path, value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk_entity_candidates(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk_entity_candidates(item, f"{path}[{index}]")


def _collect_entity_references(entry: ConfigEntry) -> dict[str, list[str]]:
    """Every entity id referenced anywhere in the config, and where from.

    Walks ``entry.data``, ``entry.options`` and every subentry's ``data`` --
    not just the fields this module knows the shape of -- because a profile's
    own options (a Solcast entity list, a template's referenced sensors) are
    exactly where the two most common remote failures ("wrong entity id" and
    "kW where the code expects W") actually show up.
    """
    references: dict[str, list[str]] = {}
    roots: list[tuple[str, Any]] = [("data", entry.data), ("options", entry.options)]
    for subentry in entry.subentries.values():
        roots.append((f"subentries[{subentry.subentry_type}:{subentry.title}]", subentry.data))
    for root_name, root_value in roots:
        for path, entity_id in _walk_entity_candidates(root_value, root_name):
            references.setdefault(entity_id, []).append(path)
    return references


def _entity_health(
    hass: HomeAssistant, registry: er.EntityRegistry, entity_id: str, referenced_by: list[str]
) -> dict[str, Any]:
    state = hass.states.get(entity_id)
    registered = registry.async_get(entity_id)
    attributes = state.attributes if state else {}
    return {
        "entity_id": entity_id,
        "referenced_by": sorted(referenced_by),
        "exists": state is not None,
        "state": state.state if state else None,
        "unit_of_measurement": attributes.get("unit_of_measurement"),
        "device_class": attributes.get("device_class"),
        "state_class": attributes.get("state_class"),
        "last_changed": state.last_changed.isoformat() if state else None,
        "registered": registered is not None,
        "disabled": bool(registered and registered.disabled),
    }


def _entities_section(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    try:
        references = _collect_entity_references(entry)
        registry = er.async_get(hass)
    except Exception as err:  # noqa: BLE001
        return [{"error": str(err)}]

    section: list[dict[str, Any]] = []
    for entity_id in sorted(references):
        try:
            section.append(_entity_health(hass, registry, entity_id, references[entity_id]))
        except Exception as err:  # noqa: BLE001 - one bad lookup must not blank the rest
            section.append({"entity_id": entity_id, "error": str(err)})
    return section


# -- custom profiles ------------------------------------------------------------


def _redact_secret_lines(content: str) -> str:
    """Blank the value of any ``key: value`` whose key looks like a credential.

    A line scrub rather than parse-redact-reserialise on purpose. This section
    exists so a *broken* profile can be read without SSH access to the machine,
    and a broken profile is exactly the one ``load_yaml`` refuses -- redacting
    through the parser would blank the file in the one case it is most needed.
    Round-tripping would also drop the comments and formatting, which is much
    of what makes somebody else's profile readable.

    Line count is preserved, including inside a redacted block scalar, so line
    numbers in a "failed to load" error still point where they did on disk.

    A credential inside an inline flow mapping (``{api_key: hunter2}``) is not
    caught. No profile this repo ships or documents is written that way, and
    catching it means parsing, which costs the broken-file case above.
    """
    out: list[str] = []
    block_indent: int | None = None

    for line in content.splitlines(keepends=True):
        text = line.rstrip("\r\n")
        newline = line[len(text) :]
        indent = len(text) - len(text.lstrip())

        # Inside the indented block beneath a redacted `key: |`.
        if block_indent is not None:
            if not text.strip():
                out.append(line)
                continue
            if indent > block_indent:
                out.append(" " * indent + REDACTED + newline)
                continue
            block_indent = None

        match = _SECRET_LINE_RE.match(text)
        if match and _REDACT_KEY_PATTERN.search(match["key"]):
            value = match["value"] or ""
            out.append(match["prefix"] + REDACTED + newline)
            # Nothing on this line means the value is the indented block under
            # it -- either a block scalar or a whole mapping. Both go, which is
            # what `_redact_config` does with a matching key elsewhere: it
            # replaces the entire subtree, not just a scalar leaf.
            if not value or _BLOCK_SCALAR_RE.match(value):
                block_indent = indent
            continue

        out.append(line)

    return "".join(out)


def _read_custom_profiles(root: Path) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    if not root.is_dir():
        return profiles
    for path in sorted(root.rglob("*.yaml")):
        try:
            stat = path.stat()
            raw = path.read_bytes()
            content = _redact_secret_lines(
                raw[:_MAX_PROFILE_BYTES].decode("utf-8", errors="replace")
            )
            profiles.append(
                {
                    "path": str(path.relative_to(root)),
                    "size": stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                    "content": content,
                    "truncated": len(raw) > _MAX_PROFILE_BYTES,
                }
            )
        except OSError as err:
            profiles.append({"path": str(path), "error": str(err)})
    return profiles


async def _async_custom_profiles_section(hass: HomeAssistant) -> list[dict[str, Any]]:
    try:
        root = Path(hass.config.path(USER_PROFILE_DIR))
        return await hass.async_add_executor_job(_read_custom_profiles, root)
    except Exception as err:  # noqa: BLE001
        return [{"error": str(err)}]


# -- logs ------------------------------------------------------------


def _logs_section(entry: ConfigEntry) -> list[dict[str, Any]]:
    try:
        return entry.runtime_data.log_handler.snapshot()
    except Exception as err:  # noqa: BLE001
        return [{"error": str(err)}]


# -- triage ------------------------------------------------------------


def _triage_section(
    coordinator: EmhassCoordinator, backend: dict[str, Any], entities: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Cheap, pre-computed verdicts, so a maintainer sees the summary before
    scrolling past several thousand lines of payload.

    Deliberately not shared with ``scripts/inspect_bundle.py``: that script
    must stay dependency-free and importable outside this integration, and
    the coupling that sharing an implementation would create is not worth it
    for checks this cheap.
    """
    try:
        findings: list[dict[str, str]] = []

        backend_error = backend.get("version_error") or backend.get("config_error")
        if backend_error:
            findings.append(
                {"severity": "error", "message": f"EMHASS backend unreachable: {backend_error}"}
            )

        for record in entities:
            if "error" in record:
                continue
            referenced_by = ", ".join(record["referenced_by"])
            if not record.get("exists"):
                findings.append(
                    {
                        "severity": "error",
                        "message": f"{record['entity_id']} does not exist "
                        f"(referenced by {referenced_by})",
                    }
                )
            elif record.get("state") in ("unavailable", "unknown"):
                findings.append(
                    {
                        "severity": "warning",
                        "message": f"{record['entity_id']} is {record['state']} "
                        f"(referenced by {referenced_by})",
                    }
                )

        data = coordinator.data
        if data is None or data.last_run is None:
            findings.append({"severity": "warning", "message": "No optimisation has ever run"})
        elif not data.last_run.ok:
            detail = data.last_run.error_message or data.last_run.status
            findings.append({"severity": "error", "message": f"Last run did not succeed: {detail}"})
        elif data.last_run.infeasible:
            findings.append(
                {
                    "severity": "warning",
                    "message": "Last run reported the optimisation problem as infeasible -- "
                    "see scripts/check_infeasibility.py",
                }
            )

        if coordinator.plan_is_stale:
            findings.append({"severity": "warning", "message": "The current plan is stale"})

        if data is not None:
            payload = data.payload or {}
            # Not "is the series empty": several profiles deliberately
            # contribute settings instead of points and let EMHASS build the
            # forecast server-side, so an empty series is the ordinary shape
            # for those. What is worth reporting is an input EMHASS was given
            # no way to obtain from either side of the wire, which leaves it
            # falling back to its own stored configuration.
            for name, series, array_key, method_key in (
                ("buy price", data.buy_price, "load_cost_forecast", "load_cost_forecast_method"),
                (
                    "sell price",
                    data.sell_price,
                    "prod_price_forecast",
                    "production_price_forecast_method",
                ),
                ("PV forecast", data.pv_forecast, "pv_power_forecast", "weather_forecast_method"),
                (
                    "load forecast",
                    data.load_forecast,
                    "load_power_forecast",
                    "load_forecast_method",
                ),
            ):
                if len(series) or payload.get(array_key) or payload.get(method_key):
                    continue
                if name == "PV forecast" and payload.get("set_use_pv") is False:
                    continue
                findings.append(
                    {
                        "severity": "error",
                        "message": f"EMHASS was given no {name} -- no {array_key} was sent and "
                        f"no {method_key} was named",
                    }
                )

            # mlforecaster is negotiated rather than obeyed: it reverts to a
            # fallback until a fit has succeeded against enough history of the
            # load sensor. Informational because that wait is by design and
            # self-resolving -- but it is the only outward sign of the
            # downgrade, and it explains a forecast that looks unlike the
            # method the options name.
            requested = coordinator.config.load.options.get("method")
            sent = payload.get("load_forecast_method")
            if requested and sent and requested != sent:
                findings.append(
                    {
                        "severity": "info",
                        "message": f"Load forecast method '{requested}' was configured but the "
                        f"last run asked EMHASS for '{sent}' -- expected while mlforecaster "
                        f"waits for {ML_MIN_HISTORY_DAYS} days of history on its load sensor",
                    }
                )

        if coordinator.profile_errors:
            findings.append(
                {
                    "severity": "error",
                    "message": f"{len(coordinator.profile_errors)} custom profile(s) failed to "
                    "load: " + ", ".join(sorted(coordinator.profile_errors)),
                }
            )

        return findings
    except Exception as err:  # noqa: BLE001
        return [{"severity": "error", "message": f"Could not compute triage: {err}"}]

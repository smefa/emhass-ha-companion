"""Resolve a profile document into concrete values.

The engine is deliberately small. It knows how to read attributes, call a
service, and render a template -- and how to map the resulting records onto a
:class:`~..models.Series`. It knows nothing about Nordpool, Solcast, or any
other specific integration; that knowledge lives in the profile documents.
"""

from __future__ import annotations

from datetime import date, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import template as template_helper
from homeassistant.util import dt as dt_util

from ..const import (
    SOURCE_TYPE_ATTRIBUTES,
    SOURCE_TYPE_SERVICE,
    SOURCE_TYPE_TEMPLATE,
)
from ..models import Point, Series, parse_utc
from .schema import Profile, ProfileError

_LOGGER = logging.getLogger(__name__)


def render(hass: HomeAssistant, value: Any, variables: dict[str, Any]) -> Any:
    """Recursively render templates inside a profile fragment.

    Non-string leaves pass through untouched, so numbers stay numbers rather
    than being stringified on the way to EMHASS.
    """
    if isinstance(value, str):
        if "{{" not in value and "{%" not in value:
            return value
        return template_helper.Template(value, hass).async_render(variables, parse_result=True)
    if isinstance(value, dict):
        return {key: render(hass, item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [render(hass, item, variables) for item in value]
    return value


def _variables(profile: Profile, options: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"options": options, "profile": profile.key, **extra}


async def async_resolve_series(
    hass: HomeAssistant,
    profile: Profile,
    options: dict[str, Any],
) -> Series:
    """Resolve a source profile into a time series."""
    source = profile.source
    if source is None:
        return Series.empty()

    variables = _variables(profile, options)
    source_type = source["type"]

    if source_type == SOURCE_TYPE_ATTRIBUTES:
        records = _read_attributes(hass, source, variables)
    elif source_type == SOURCE_TYPE_SERVICE:
        records = await _call_service(hass, source, variables)
    elif source_type == SOURCE_TYPE_TEMPLATE:
        records = _render_template_records(hass, source, variables)
    else:  # pragma: no cover - schema restricts the set
        raise ProfileError(f"Unknown source type: {source_type}")

    # The field mapping is rendered too, so a profile can expose the choice of
    # field as a user option -- Solcast's P10/P50/P90 estimates, for instance,
    # differ only in which key is read.
    return _map_records(records, render(hass, profile.series_map, variables))


# --- source types ------------------------------------------------------------


def _read_attributes(
    hass: HomeAssistant, source: dict[str, Any], variables: dict[str, Any]
) -> list[dict[str, Any]]:
    """Read one or more list-valued attributes from one or more entities."""
    resolved = render(hass, source["entity"], variables)
    entity_ids = resolved if isinstance(resolved, list) else [resolved]
    attributes = render(hass, source["attributes"], variables)

    records: list[dict[str, Any]] = []
    missing: list[str] = []
    for entity_id in entity_ids:
        if not entity_id:
            continue
        state = hass.states.get(entity_id)
        if state is None:
            missing.append(entity_id)
            continue
        for attribute in attributes:
            value = state.attributes.get(attribute)
            if value is None:
                # Routinely absent rather than broken: `raw_tomorrow` is empty
                # until the market publishes, typically early afternoon.
                _LOGGER.debug("Attribute %s missing on %s", attribute, entity_id)
                continue
            if not isinstance(value, list):
                raise ProfileError(
                    f"Attribute '{attribute}' on {entity_id} is "
                    f"{type(value).__name__}, expected a list"
                )
            records.extend(value)

    if missing:
        raise ProfileError(f"Entity not found: {', '.join(missing)}")
    return records


async def _call_service(
    hass: HomeAssistant, source: dict[str, Any], variables: dict[str, Any]
) -> list[dict[str, Any]]:
    """Call a service that returns response data, once per requested day."""
    domain, _, service = str(source["service"]).partition(".")
    if not domain or not service:
        raise ProfileError(f"Invalid service name: {source['service']!r}")

    day_offsets: list[int] = source.get("for_days") or [0]
    today: date = dt_util.now().date()

    records: list[dict[str, Any]] = []
    for offset in day_offsets:
        day = today + timedelta(days=offset)
        scoped = {**variables, "day": day.isoformat(), "day_offset": offset}
        data = render(hass, source.get("data") or {}, scoped)

        try:
            response = await hass.services.async_call(
                domain, service, data, blocking=True, return_response=True
            )
        except Exception as err:
            # Tomorrow's data legitimately does not exist for most of the day;
            # a hard failure here would take down an otherwise fine run.
            if offset > 0:
                _LOGGER.debug(
                    "Service %s.%s returned no data for %s: %s",
                    domain,
                    service,
                    day,
                    err,
                )
                continue
            raise ProfileError(f"Service {domain}.{service} failed for {day}: {err}") from err

        extracted = _dig(response, render(hass, source.get("response_path"), scoped))
        if extracted is None:
            continue
        if not isinstance(extracted, list):
            raise ProfileError(
                f"Service {domain}.{service} response path yielded "
                f"{type(extracted).__name__}, expected a list"
            )
        records.extend(extracted)
    return records


def _render_template_records(
    hass: HomeAssistant, source: dict[str, Any], variables: dict[str, Any]
) -> list[dict[str, Any]]:
    """Render the escape-hatch template, which must produce a list of records."""
    result = render(hass, source["value"], variables)
    if isinstance(result, str):
        raise ProfileError(
            "template source rendered to a string; it must produce a list of "
            "records (append `| tojson` if you are building one manually)"
        )
    if not isinstance(result, list):
        raise ProfileError(f"template source rendered to {type(result).__name__}, expected a list")
    return result


# --- mapping -----------------------------------------------------------------


def _map_records(records: list[Any], series_map: dict[str, Any] | None) -> Series:
    """Map raw records onto a Series using the profile's field mapping."""
    mapping = series_map or {}
    time_key = mapping.get("time", "time")
    value_key = mapping.get("value", "value")
    scale = float(mapping.get("scale", 1.0))
    offset = float(mapping.get("offset", 0.0))

    points: list[Point] = []
    skipped = 0
    for record in records:
        if not isinstance(record, dict):
            raise ProfileError(f"Expected record mappings, got {type(record).__name__}")
        if time_key not in record:
            raise ProfileError(
                f"Record has no '{time_key}' field; available fields: "
                f"{', '.join(sorted(map(str, record))) or '(none)'}"
            )
        raw_value = record.get(value_key)
        if raw_value is None:
            # A genuine gap (a missing settlement period) rather than a
            # misconfiguration; dropping it lets EMHASS interpolate.
            skipped += 1
            continue
        try:
            numeric = float(raw_value)
        except (TypeError, ValueError):
            raise ProfileError(f"Field '{value_key}' is not numeric: {raw_value!r}") from None
        points.append(Point(parse_utc(record[time_key]), numeric * scale + offset))

    if skipped:
        _LOGGER.debug("Skipped %d record(s) with no value", skipped)
    return Series(points)


def _dig(data: Any, path: str | None) -> Any:
    """Follow a dotted path into a service response."""
    if not path:
        return data
    current = data
    for part in str(path).split("."):
        if isinstance(current, dict):
            if part not in current:
                available = ", ".join(sorted(map(str, current))) or "(empty)"
                raise ProfileError(f"Response path '{path}' not found; available keys: {available}")
            current = current[part]
        else:
            raise ProfileError(
                f"Response path '{path}' expected a mapping, got {type(current).__name__}"
            )
    return current


# --- settings ----------------------------------------------------------------


def resolve_settings(
    hass: HomeAssistant, profile: Profile, options: dict[str, Any]
) -> dict[str, Any]:
    """Render the EMHASS settings a profile contributes to the payload."""
    return render(hass, profile.emhass_settings, _variables(profile, options))

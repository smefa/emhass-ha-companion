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
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import template as template_helper
from homeassistant.util import dt as dt_util

from ..const import (
    ACTION_CURTAIL,
    ACTION_UNCURTAIL,
    MODE_FORCE_CHARGE,
    MODE_FORCE_DISCHARGE,
    PERCENT_UNITS,
    POWER_UNIT_KW,
    POWER_UNIT_PERCENT,
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
        try:
            return template_helper.Template(value, hass).async_render(variables, parse_result=True)
        except TemplateError as err:
            # HA wraps both a syntax error and a render-time error (an
            # undefined variable, a bad filter) in this one exception type, so
            # catching it here is enough to cover every render() call site.
            # Left uncaught, a malformed profile -- most likely one the user
            # wrote themselves -- would blow up test_profile, the diagnostic
            # service that exists specifically to surface a broken profile
            # cleanly, with an unrelated traceback instead of an answer.
            raise ProfileError(f"Template error in {value!r}: {err}") from err
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


# --- inverter actions --------------------------------------------------------


def _rated_power_w(hass: HomeAssistant, profile: Profile, options: dict[str, Any]) -> float:
    """The inverter's rating, needed to express a setpoint as a percentage."""
    control = profile.control
    raw = render(hass, control.get("rated_power_w"), _variables(profile, options))
    try:
        rated = float(raw)
    except (TypeError, ValueError):
        raise ProfileError(
            f"Profile '{profile.key}' has power_unit '{control['power_unit']}' but "
            f"rated_power_w resolved to {raw!r}, which is not a number"
        ) from None
    if rated <= 0:
        raise ProfileError(
            f"Profile '{profile.key}' resolved rated_power_w to {rated}; a "
            "percentage of zero rated power is meaningless"
        )
    return rated


def convert_power(
    hass: HomeAssistant,
    profile: Profile,
    options: dict[str, Any],
    action: str,
    power_w: float,
) -> float:
    """Turn EMHASS's signed watts into the number this profile's action writes.

    This lives in the engine rather than in every profile's templates on
    purpose. A profile author hand-rolling ``{{ power_w / rated * 10000 }}`` is
    how a hundred-fold error reaches somebody's battery, and the unit is
    something the hardware has rather than something the author should compute.

    EMHASS's convention on the way in is positive = discharge.
    """
    control = profile.control
    magnitude = abs(float(power_w))

    # Charging loses something between the meter and the cells; a profile can
    # ask for proportionally more so that what arrives matches the plan.
    if action == MODE_FORCE_CHARGE:
        magnitude *= float(control["charge_boost"])

    if control["signed"]:
        if action == MODE_FORCE_CHARGE:
            value = -magnitude
        elif action == MODE_FORCE_DISCHARGE:
            value = magnitude
        else:
            value = 0.0
        if control["invert_sign"]:
            value = -value
    else:
        # Direction is carried by a mode select or by which service is called,
        # so the number is a magnitude and a negative one would be rejected.
        value = magnitude

    unit = control["power_unit"]
    if unit == POWER_UNIT_KW:
        value /= 1000.0
    elif unit in PERCENT_UNITS:
        rated = _rated_power_w(hass, profile, options)
        span = 100.0 if unit == POWER_UNIT_PERCENT else 10000.0
        value = value / rated * span
        # Asking for more than the inverter is rated for is a rejected write at
        # best; clamping keeps a plan that overshoots from failing outright.
        value = max(-span, min(span, value))

    step = float(control["round_to"])
    if step > 0:
        value = round(value / step) * step
    return int(value) if step >= 1 and value == int(value) else value


def convert_curtail_power(
    hass: HomeAssistant,
    profile: Profile,
    options: dict[str, Any],
    curtail_w: float,
) -> float:
    """Turn a curtailment target in watts into this profile's curtail_unit.

    Mirrors convert_power's unit handling. No sign or charge_boost, though --
    a curtailment target is always a plain non-negative magnitude, never a
    direction.
    """
    control = profile.control
    magnitude = abs(float(curtail_w))

    unit = control["curtail_unit"]
    if unit == POWER_UNIT_KW:
        magnitude /= 1000.0
    elif unit in PERCENT_UNITS:
        rated = _rated_power_w(hass, profile, options)
        span = 100.0 if unit == POWER_UNIT_PERCENT else 10000.0
        magnitude = magnitude / rated * span
        magnitude = max(0.0, min(span, magnitude))

    step = float(control["round_to"])
    if step > 0:
        magnitude = round(magnitude / step) * step
    return int(magnitude) if step >= 1 and magnitude == int(magnitude) else magnitude


def action_variables(
    hass: HomeAssistant,
    profile: Profile,
    options: dict[str, Any],
    action: str,
    *,
    power_w: float = 0.0,
    curtail_w: float = 0.0,
    **extra: Any,
) -> dict[str, Any]:
    """The template scope one inverter action is rendered against."""
    control = profile.control
    return {
        "action": action,
        # Ready to write: converted to this profile's unit, signed if the
        # hardware wants a signed value, boosted and rounded. Use this one.
        "power": convert_power(hass, profile, options, action, power_w),
        # The raw plan value, EMHASS's convention, always watts and always
        # signed. For anything the conversion above cannot express.
        "power_w": round(float(power_w)),
        "magnitude_w": round(abs(float(power_w))),
        # How long to ask for, when the command carries its own lifetime.
        "duration_min": control["duration_min"],
        # Ready to write, converted to curtail_unit -- only for the actions it
        # means anything to. Computed unconditionally otherwise is how a
        # curtailment profile's rated_power_w template failing would break
        # rendering force_charge/force_discharge too.
        "curtail_w": (
            convert_curtail_power(hass, profile, options, curtail_w)
            if action in (ACTION_CURTAIL, ACTION_UNCURTAIL)
            else 0
        ),
        **extra,
    }


def render_action(
    hass: HomeAssistant,
    profile: Profile,
    options: dict[str, Any],
    action: str,
    **variables: Any,
) -> list[dict[str, Any]]:
    """Render one inverter action into concrete service calls.

    Rendering is deliberately separate from executing. The dry-run mode needs
    to know exactly what *would* be called without calling it, and the only way
    to be sure of that is for both paths to produce the calls the same way.
    """
    steps = profile.actions.get(action)
    if steps is None:
        raise ProfileError(
            f"Profile '{profile.key}' has no '{action}' action; it defines: "
            f"{', '.join(sorted(profile.actions)) or '(none)'}"
        )
    scope = _variables(
        profile,
        options,
        **action_variables(hass, profile, options, action, **variables),
    )
    return [render(hass, step, scope) for step in steps]


async def async_execute_steps(hass: HomeAssistant, steps: list[dict[str, Any]]) -> None:
    """Execute rendered service calls in order."""
    for step in steps:
        domain, _, service = str(step["service"]).partition(".")
        if not domain or not service:
            raise ProfileError(f"Invalid service name: {step['service']!r}")
        await hass.services.async_call(
            domain,
            service,
            step.get("data") or {},
            blocking=True,
            target=step.get("target") or {},
        )


def resolve_sensors(
    hass: HomeAssistant, profile: Profile, options: dict[str, Any]
) -> dict[str, str]:
    """Render the profile's role-to-entity mapping."""
    return render(hass, profile.sensors, _variables(profile, options))


def resolve_limits(
    hass: HomeAssistant, profile: Profile, options: dict[str, Any]
) -> dict[str, Any]:
    return render(hass, profile.limits, _variables(profile, options))

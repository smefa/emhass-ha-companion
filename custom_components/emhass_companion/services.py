"""Integration services."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import ACTION_DAYAHEAD, ACTION_MPC, DOMAIN, PROFILE_KIND_NETWORK
from .network_calendar import HolidayCache, NetworkCalendar, NetworkCalendarError
from .profiles import ProfileError, async_resolve_series, resolve_network, resolve_settings
from .profiles.schema import humanize_error
from .typical_load import typical_day_records

SERVICE_RUN_DAYAHEAD = "run_dayahead"
SERVICE_RUN_MPC = "run_mpc"
SERVICE_TEST_PROFILE = "test_profile"
SERVICE_TYPICAL_LOAD_FORECAST = "typical_load_forecast"

TEST_PROFILE_SCHEMA = vol.Schema(
    {
        vol.Required("profile"): str,
        vol.Optional("options", default=dict): dict,
        vol.Optional("limit", default=10): vol.All(int, vol.Range(min=1, max=500)),
    }
)

TYPICAL_LOAD_FORECAST_SCHEMA = vol.Schema(
    {
        vol.Required("day"): cv.date,
        vol.Required("average_w"): vol.All(vol.Coerce(float), vol.Range(min=0)),
    }
)


async def _require_admin(hass: HomeAssistant, call: ServiceCall) -> None:
    """Reject a non-admin caller of a diagnostic action.

    ``context.user_id`` is None for anything Home Assistant itself triggered --
    an automation, a script, another integration -- and those stay allowed;
    only a real, non-admin user is turned away.
    """
    if call.context.user_id is None:
        return
    user = await hass.auth.async_get_user(call.context.user_id)
    if user is not None and not user.is_admin:
        raise Unauthorized(context=call.context)


def _coordinator(hass: HomeAssistant):
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("EMHASS Companion is not set up")
    return entries[0].runtime_data.coordinator


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register services once, shared across entries."""
    if hass.services.has_service(DOMAIN, SERVICE_RUN_DAYAHEAD):
        return

    async def _run_dayahead(call: ServiceCall) -> None:
        await _coordinator(hass).async_run(ACTION_DAYAHEAD)

    async def _run_mpc(call: ServiceCall) -> None:
        await _coordinator(hass).async_run(ACTION_MPC)

    async def _test_profile(call: ServiceCall) -> ServiceResponse:
        """Resolve a profile and return what it produced.

        A YAML profile that silently yields nothing is far harder to debug than
        a Python one that raises. This turns "my prices are wrong" into a single
        call that shows exactly which records came back.

        Admin-only, unlike the run actions: the response renders the profile's
        ``emhass:`` block, which for a user profile is where a forecast API key
        lives. Home Assistant does not restrict action calls by user, so the
        check has to be here. A call with no user (an automation, a script,
        anything internal) keeps working.
        """
        await _require_admin(hass, call)

        coordinator = _coordinator(hass)
        key = call.data["profile"]

        if (profile := coordinator.profiles.get(key)) is None:
            available = ", ".join(sorted(coordinator.profiles)) or "(none loaded)"
            raise ServiceValidationError(f"Unknown profile '{key}'. Available: {available}")

        options: dict[str, Any] = dict(call.data["options"])
        if options:
            # Caller-supplied options are rendered into the profile's own
            # templates, so they are held to the schema the config flow would
            # have shown rather than accepted as an arbitrary mapping -- with
            # the Solcast profile, an unchecked ``entities`` list is a way to
            # read the attributes of any entity by name.
            try:
                options = vol.Schema(profile.selector_schema())(options)
            except vol.Invalid as err:
                raise ServiceValidationError(
                    f"Invalid options for profile '{key}': {humanize_error(err)}"
                ) from err
        else:
            # Fall back to whatever this profile is configured with, so the
            # common case needs no arguments at all. Not re-validated: these
            # came from the config flow, and a profile whose options have since
            # changed shape should still be diagnosable.
            for selection in (
                coordinator.config.price,
                coordinator.config.pv,
                coordinator.config.load,
                coordinator.config.temperature,
                coordinator.config.inverter,
            ):
                if selection.key == key:
                    options = dict(selection.options)
                    break

        result: dict[str, Any] = {
            "profile": key,
            "name": profile.name,
            "kind": profile.kind,
            # Filename only: which file this is remains obvious, without
            # handing out the layout of the config directory.
            "file": Path(profile.path).name,
            "is_builtin": profile.is_builtin,
            "options_used": options,
        }

        if profile.kind == PROFILE_KIND_NETWORK:
            # A network profile fetches nothing and delegates nothing to
            # EMHASS -- it has no ``emhass:`` settings worth reporting and no
            # series to resolve, so the generic branches below (written for
            # every other kind) would report "contributes nothing" for a
            # profile that in fact defines bands and a demand charge. The
            # useful diagnostic here is what actually matches *now*.
            try:
                resolved = resolve_network(hass, profile, options)
                calendar = NetworkCalendar.from_resolved(resolved)
            except (ProfileError, NetworkCalendarError) as err:
                result["error"] = str(err)
                return result
            now = dt_util.utcnow()
            holidays = HolidayCache()
            if entity_id := next(iter(calendar.holiday_entities()), None):
                await holidays.async_ensure(hass, entity_id, {dt_util.as_local(now).date()})
            band = calendar.current_band(now, holidays)
            result.update(
                {
                    "bands": [b.name for b in calendar.bands],
                    "current_band": band.name if band else None,
                    "current_adjustment": (
                        {"multiplier": band.buy.multiplier, "adder": band.buy.adder}
                        if band
                        else None
                    ),
                    "has_demand_charge": calendar.demand_charge is not None,
                }
            )
            return result

        try:
            settings = resolve_settings(hass, profile, options)
        except Exception as err:  # noqa: BLE001 - a template can raise anything;
            # this action exists to diagnose broken profiles, so the failure is
            # returned in the response rather than raised out of the service.
            result["settings_error"] = str(err)
        else:
            result["emhass_settings"] = settings

        if not profile.produces_series:
            result["series"] = None
            result["note"] = (
                "This profile contributes EMHASS settings and does not fetch a series of its own."
            )
            return result

        try:
            series = await async_resolve_series(hass, profile, options)
        except ProfileError as err:
            result["error"] = str(err)
            return result

        limit = call.data["limit"]
        result.update(
            {
                "count": len(series),
                "unit": profile.unit,
                "start": dt_util.as_local(series.start).isoformat() if series else None,
                "end": dt_util.as_local(series.end).isoformat() if series else None,
                "step_minutes": (step.total_seconds() / 60 if (step := series.step()) else None),
                "series": [
                    {
                        "time": dt_util.as_local(point.time).isoformat(),
                        "value": point.value,
                    }
                    for point in list(series)[:limit]
                ],
                "truncated": len(series) > limit,
            }
        )
        return result

    async def _typical_load_forecast(call: ServiceCall) -> ServiceResponse:
        """Back the "Typical household" load profile's ``source: service`` block.

        Kept as a service rather than a fourth engine source type: the engine
        deliberately offers only attributes/service/template, and a service is
        the existing, sanctioned way for a profile to fetch code-computed
        records -- this one is just backed by our own domain instead of a
        third-party integration's.
        """
        records = await hass.async_add_executor_job(
            typical_day_records, call.data["day"], call.data["average_w"]
        )
        return {"forecast": records}

    hass.services.async_register(DOMAIN, SERVICE_RUN_DAYAHEAD, _run_dayahead)
    hass.services.async_register(DOMAIN, SERVICE_RUN_MPC, _run_mpc)
    hass.services.async_register(
        DOMAIN,
        SERVICE_TEST_PROFILE,
        _test_profile,
        schema=TEST_PROFILE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_TYPICAL_LOAD_FORECAST,
        _typical_load_forecast,
        schema=TYPICAL_LOAD_FORECAST_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


@callback
def async_unregister_services(hass: HomeAssistant) -> None:
    for service in (
        SERVICE_RUN_DAYAHEAD,
        SERVICE_RUN_MPC,
        SERVICE_TEST_PROFILE,
        SERVICE_TYPICAL_LOAD_FORECAST,
    ):
        hass.services.async_remove(DOMAIN, service)

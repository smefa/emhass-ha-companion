"""Integration services."""

from __future__ import annotations

from typing import Any

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import ACTION_DAYAHEAD, ACTION_MPC, DOMAIN
from .profiles import ProfileError, async_resolve_series, resolve_settings
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
        """
        coordinator = _coordinator(hass)
        key = call.data["profile"]

        if (profile := coordinator.profiles.get(key)) is None:
            available = ", ".join(sorted(coordinator.profiles)) or "(none loaded)"
            raise ServiceValidationError(f"Unknown profile '{key}'. Available: {available}")

        options: dict[str, Any] = dict(call.data["options"])
        if not options:
            # Fall back to whatever this profile is configured with, so the
            # common case needs no arguments at all.
            for selection in (
                coordinator.config.price,
                coordinator.config.pv,
                coordinator.config.load,
            ):
                if selection.key == key:
                    options = dict(selection.options)
                    break

        result: dict[str, Any] = {
            "profile": key,
            "name": profile.name,
            "kind": profile.kind,
            "path": profile.path,
            "is_builtin": profile.is_builtin,
            "options_used": options,
        }

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

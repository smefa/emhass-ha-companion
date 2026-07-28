"""Diagnostics for support requests."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import EmhassCoordinator


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: EmhassCoordinator = entry.runtime_data.coordinator
    data = coordinator.data
    config = coordinator.config

    return {
        "entry": {
            "options": dict(entry.options),
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
        # The exact request is the single most useful artefact when a plan looks
        # wrong, since EMHASS's own configuration screen does not reflect it.
        "last_payload": data.payload,
        "warnings": data.warnings,
        "deferrable_order": data.load_order,
    }

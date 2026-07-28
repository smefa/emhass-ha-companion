"""The EMHASS Companion integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EmhassClient, EmhassError
from .const import (
    CONF_URL,
    DOMAIN,
    ISSUE_BAD_PROFILE,
    ISSUE_EMHASS_VERSION,
    MIN_EMHASS_VERSION,
)
from .coordinator import EmhassCoordinator
from .deferrable import DeferrableRegistry
from .schedule import Scheduler
from .services import async_register_services, async_unregister_services
from .util import version_at_least

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]

type EmhassConfigEntry = ConfigEntry[EmhassRuntimeData]


class EmhassRuntimeData:
    """Objects that live for the lifetime of a config entry."""

    def __init__(
        self,
        coordinator: EmhassCoordinator,
        scheduler: Scheduler,
        loads: DeferrableRegistry,
    ) -> None:
        self.coordinator = coordinator
        self.scheduler = scheduler
        self.loads = loads


async def async_setup_entry(hass: HomeAssistant, entry: EmhassConfigEntry) -> bool:
    """Set up EMHASS Companion from a config entry."""
    client = EmhassClient(async_get_clientsession(hass), entry.data[CONF_URL])

    try:
        version = await client.async_get_version()
    except EmhassError as err:
        raise ConfigEntryNotReady(f"Cannot reach EMHASS: {err}") from err

    _check_version(hass, version)

    loads = DeferrableRegistry(hass, entry)
    loads.sync()

    coordinator = EmhassCoordinator(hass, entry, client, loads)
    await coordinator.async_load_profiles()
    _report_profile_errors(hass, coordinator)

    scheduler = Scheduler(hass, coordinator)
    entry.runtime_data = EmhassRuntimeData(coordinator, scheduler, loads)

    # Entities are created before the first optimisation so that a failed or
    # slow first run leaves a diagnosable integration rather than none at all.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_on_unload(scheduler.async_stop)
    entry.async_on_unload(loads.async_stop)

    loads.async_start()
    scheduler.async_start()
    entry.async_create_background_task(hass, scheduler.async_run_initial(), "emhass_initial_run")

    return True


async def async_unload_entry(hass: HomeAssistant, entry: EmhassConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and len(hass.config_entries.async_entries(DOMAIN)) == 1:
        async_unregister_services(hass)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: EmhassConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _check_version(hass: HomeAssistant, version: str | None) -> None:
    """Raise a repair if EMHASS predates the JSON plan API this relies on."""
    if version and not version_at_least(version, MIN_EMHASS_VERSION):
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_EMHASS_VERSION,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_EMHASS_VERSION,
            translation_placeholders={
                "installed": version,
                "required": MIN_EMHASS_VERSION,
            },
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_EMHASS_VERSION)


def _report_profile_errors(hass: HomeAssistant, coordinator: EmhassCoordinator) -> None:
    """Surface malformed profiles without blocking setup.

    A broken profile -- most likely one the user wrote -- should cost them that
    profile and a clear message, not the whole integration.
    """
    if not coordinator.profile_errors:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_BAD_PROFILE)
        return

    details = "\n".join(
        f"- `{path}`: {reason}" for path, reason in sorted(coordinator.profile_errors.items())
    )
    for path, reason in coordinator.profile_errors.items():
        _LOGGER.error("Ignoring invalid profile %s: %s", path, reason)

    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_BAD_PROFILE,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_BAD_PROFILE,
        translation_placeholders={
            "count": str(len(coordinator.profile_errors)),
            "details": details,
        },
    )

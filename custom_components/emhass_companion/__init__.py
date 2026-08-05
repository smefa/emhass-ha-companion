"""The EMHASS Companion integration.

Copyright (C) 2026 Tomas Smedberg.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. It is distributed without any warranty; see the LICENSE file at
the repository root for the full terms.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .api import EmhassClient, EmhassError
from .const import (
    CONF_HOUSE_LOAD_TOTAL_ENTITY,
    CONF_URL,
    DOMAIN,
    ISSUE_BAD_PROFILE,
    ISSUE_EMHASS_VERSION,
    LOAD_SUBENTRY_TYPES,
    MIN_EMHASS_VERSION,
    NET_HOUSE_LOAD_KEY,
)
from .coordinator import EmhassCoordinator
from .deferrable import DeferrableRegistry
from .executor import Executor
from .frontend import async_setup_frontend
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
        executor: Executor,
    ) -> None:
        self.coordinator = coordinator
        self.scheduler = scheduler
        self.loads = loads
        self.executor = executor


async def async_setup_entry(hass: HomeAssistant, entry: EmhassConfigEntry) -> bool:
    """Set up EMHASS Companion from a config entry."""
    client = EmhassClient(async_get_clientsession(hass), entry.data[CONF_URL])

    try:
        version = await client.async_get_version()
    except EmhassError as err:
        raise ConfigEntryNotReady(f"Cannot reach EMHASS: {err}") from err

    _check_version(hass, version)
    await _async_setup_cards(hass, entry)

    loads = DeferrableRegistry(hass, entry)
    loads.sync()

    if entry.options.get(CONF_HOUSE_LOAD_TOTAL_ENTITY):
        # Registered here, ahead of both the config sync below and the sensor
        # platform forwarded later, so EmhassConfig.from_entry can resolve
        # this entity's id on the very first setup -- not just from the
        # second one onward, once the sensor platform has run at least once.
        er.async_get(hass).async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_{NET_HOUSE_LOAD_KEY}",
            config_entry=entry,
            suggested_object_id=NET_HOUSE_LOAD_KEY,
        )

    coordinator = EmhassCoordinator(hass, entry, client, loads)
    await coordinator.async_load_profiles()
    _report_profile_errors(hass, coordinator)
    await coordinator.async_sync_emhass_config()

    scheduler = Scheduler(hass, coordinator)
    executor = Executor(hass, coordinator)
    entry.runtime_data = EmhassRuntimeData(coordinator, scheduler, loads, executor)

    # Entities are created before the first optimisation so that a failed or
    # slow first run leaves a diagnosable integration rather than none at all.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)

    entry.async_on_unload(scheduler.async_stop)
    entry.async_on_unload(loads.async_stop)
    entry.async_on_unload(coordinator.async_stop_clock)
    entry.async_on_unload(coordinator.async_stop_pv_live_tracking)

    async def _async_restore_on_unload() -> None:
        await executor.async_restore("integration unloaded")

    @callback
    def _async_restore_on_stop(_event: Event) -> None:
        entry.async_create_background_task(
            hass, executor.async_restore("Home Assistant stopping"), "emhass_restore"
        )

    # An inverter whose registers persist until changed has no idea Home
    # Assistant went away. Without these two, a restart during a forced charge
    # leaves the battery charging until somebody notices the bill.
    entry.async_on_unload(_async_restore_on_unload)
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_restore_on_stop)
    )

    @callback
    def _async_entry_changed(change_type: ConfigEntryChange, changed_entry: ConfigEntry) -> None:
        """Reload when a deferrable load subentry is added or removed.

        Entities for a load are built exactly once, above, from
        ``loads.all()`` -- adding or removing a subentry (Settings > Devices &
        Services > this integration > Add/remove deferrable load) does not by
        itself create or clean up anything. This signal is the correct place
        to catch that: it fires from ``ConfigEntries._async_save_and_notify``,
        strictly *after* the subentry is actually persisted, unlike scheduling
        a reload from inside the subentry flow step itself (which races
        ``ConfigSubentryFlowManager.async_finish_flow``'s own
        ``async_add_subentry`` call -- sometimes running before the subentry
        it is meant to pick up actually exists).

        ``entry.update_listeners`` is not an option here: this entry's options
        flow uses ``OptionsFlowWithReload``, which core refuses to combine with
        any update listener. This dispatcher signal is a separate mechanism
        the options flow does not use, so the two cannot conflict. Filtered to
        deferrable-load membership specifically, not every UPDATED event --
        that also fires on every state transition (including this reload's
        own) and on options changes, and reloading on those too would just
        spin forever reacting to its own side effects.
        """
        if changed_entry.entry_id != entry.entry_id:
            return
        if change_type != ConfigEntryChange.UPDATED or entry.runtime_data is None:
            return
        current = {
            subentry_id
            for subentry_id, subentry in entry.subentries.items()
            if subentry.subentry_type in LOAD_SUBENTRY_TYPES
        }
        already_loaded = {load.subentry_id for load in entry.runtime_data.loads.all()}
        if current != already_loaded:
            hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _async_entry_changed)
    )

    @callback
    def _async_plan_updated() -> None:
        """Act on every new plan.

        Registered after the platforms are set up, so the control switch has
        already restored its value and a restart cannot briefly act while the
        gate still reads as off-by-default.
        """
        entry.async_create_background_task(
            hass, executor.async_apply(), "emhass_apply", eager_start=False
        )

    entry.async_on_unload(coordinator.async_add_listener(_async_plan_updated))

    loads.async_start()
    coordinator.async_start_clock()
    coordinator.async_start_pv_live_tracking()
    scheduler.async_start()
    entry.async_create_background_task(hass, scheduler.async_run_initial(), "emhass_initial_run")

    return True


async def async_unload_entry(hass: HomeAssistant, entry: EmhassConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and len(hass.config_entries.async_entries(DOMAIN)) == 1:
        async_unregister_services(hass)
    return unloaded


async def _async_setup_cards(hass: HomeAssistant, entry: EmhassConfigEntry) -> None:
    """Serve and register the dashboard cards.

    A failure here costs the user the cards, never the integration -- the
    entities and the optimisation are what actually matter.
    """
    try:
        await async_setup_frontend(hass, _integration_version(hass))
    except Exception as err:  # noqa: BLE001 - cards are cosmetic; setup is not
        _LOGGER.warning("Could not register the dashboard cards: %s", err)


def _integration_version(hass: HomeAssistant) -> str:
    """Version string used to cache-bust the card bundle."""
    from homeassistant.loader import async_get_loaded_integration

    try:
        return async_get_loaded_integration(hass, DOMAIN).version or "0"
    except Exception:  # noqa: BLE001 - only affects cache busting
        return "0"


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

"""Shared entity base classes."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EmhassCoordinator


class EmhassEntity(CoordinatorEntity[EmhassCoordinator]):
    """Base for entities on the main EMHASS Companion device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EmhassCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="EMHASS Companion",
            manufacturer="EMHASS",
            configuration_url=coordinator.client.base_url,
        )

    @property
    def available(self) -> bool:
        # Deliberately not gated on coordinator.last_update_success: a failed
        # optimisation should leave the previous plan visible and the
        # diagnostic entities readable, which is exactly when they are needed.
        return True

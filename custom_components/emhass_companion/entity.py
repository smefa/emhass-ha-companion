"""Shared entity base classes."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EmhassCoordinator
from .deferrable import DeferrableRuntime


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


class EmhassLoadEntity(CoordinatorEntity[EmhassCoordinator]):
    """Base for entities belonging to one deferrable load.

    Each load is a config subentry with its own device, hung off the main
    EMHASS Companion device.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: EmhassCoordinator, load: DeferrableRuntime, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self.load = load
        self._attr_unique_id = f"{entry.entry_id}_{load.subentry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{load.subentry_id}")},
            name=load.name,
            manufacturer="EMHASS",
            model="Deferrable load",
            via_device=(DOMAIN, entry.entry_id),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Registry changes (a power sensor moving, the midnight reset) are not
        # coordinator updates, so entities subscribe to them separately.
        self.async_on_remove(self.load.add_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return True

    @property
    def plan_index(self) -> int | None:
        """This load's position in EMHASS's ``P_deferrable{k}`` columns.

        Not a fixed number: it shifts as loads are enabled and disabled, so it
        is resolved from the order recorded when the payload was built.
        """
        if not self.coordinator.data:
            return None
        return self.coordinator.data.deferrable_index(self.load.subentry_id)

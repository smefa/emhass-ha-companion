"""Buttons to trigger optimisations on demand."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EmhassCoordinator
from .deferrable import DeferrableRuntime
from .entity import EmhassEntity, EmhassLoadEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class EmhassButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[EmhassCoordinator], Coroutine[Any, Any, None]]


BUTTONS: tuple[EmhassButtonDescription, ...] = (
    EmhassButtonDescription(
        key="run_dayahead",
        translation_key="run_dayahead",
        press_fn=lambda coordinator: coordinator.async_run_dayahead(),
    ),
    EmhassButtonDescription(
        key="run_mpc",
        translation_key="run_mpc",
        press_fn=lambda coordinator: coordinator.async_run_mpc(),
    ),
)

FORECAST_FIT_BUTTON = EmhassButtonDescription(
    key="run_forecast_fit",
    translation_key="run_forecast_fit",
    press_fn=lambda coordinator: coordinator.async_run_forecast_fit(),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: EmhassCoordinator = entry.runtime_data.coordinator

    descriptions = list(BUTTONS)
    # Only meaningful when the load profile trains EMHASS's own mlforecaster --
    # otherwise there is no model to fit and pressing it would just fail.
    if coordinator.uses_mlforecaster:
        descriptions.append(FORECAST_FIT_BUTTON)

    async_add_entities(EmhassButton(coordinator, description) for description in descriptions)

    for load in entry.runtime_data.loads.all():
        # A thermal load's target is its comfort band, not a run-time total,
        # so there is no operating_hours for a forced run to disarm against.
        if not load.is_thermal:
            async_add_entities(
                [LoadRunNowButton(coordinator, load)], config_subentry_id=load.subentry_id
            )


class EmhassButton(EmhassEntity, ButtonEntity):
    entity_description: EmhassButtonDescription
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: EmhassCoordinator, description: EmhassButtonDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self.coordinator)


class LoadRunNowButton(EmhassLoadEntity, ButtonEntity):
    """Run this load immediately, regardless of recurrence.

    Distinct from the on-demand ``requested`` switch: that arms the load for
    the optimiser to place somewhere in its window/deadline, still choosing
    *when*. This skips that decision and turns the load on right now. It
    clears itself once the load's operating_hours target for the day is met
    -- see DeferrableRuntime.check_auto_disarm -- so pressing it again after
    that starts a fresh day's worth of forced running.
    """

    _attr_translation_key = "run_now"

    def __init__(self, coordinator: EmhassCoordinator, load: DeferrableRuntime) -> None:
        super().__init__(coordinator, load, "run_now")

    @property
    def available(self) -> bool:
        """Never on a surplus load.

        ``force_run`` means "run regardless of the plan", which on a load whose
        entire premise is spare solar means charging the car off the grid at
        midnight. It cannot disarm itself there either: the rule is
        ``elapsed_today >= operating_hours``, and a surplus load's
        operating_hours is structurally zero -- its run time comes from
        ``surplus.allocate`` instead -- so the target is either met before the
        load has done anything or never measured at all.

        The honest version of "run it now" for a surplus load is *Start as
        early as possible*, which keeps the surplus gate and only gives up the
        optimiser's freedom to place the run later in the day. See
        docs/surplus_loads.md.

        Unavailable rather than never created, because ``recurrence`` is a live
        select: a load switched to spare solar this afternoon has to lose the
        button then, not only on the next restart.
        """
        return super().available and not self.load.on_surplus

    async def async_press(self) -> None:
        self.load.force_run()
        self.load.notify()

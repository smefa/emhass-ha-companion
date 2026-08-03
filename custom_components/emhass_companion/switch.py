"""The control-authority switch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import ATTR_DEADLINE_AT, ATTR_REQUEST_RUNTIME_SECONDS, ATTR_REQUESTED_AT
from .coordinator import EmhassCoordinator
from .deferrable import DeferrableRuntime
from .entity import EmhassEntity, EmhassLoadEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class LoadSwitchDescription(SwitchEntityDescription):
    """A switch backed by one boolean of a deferrable load's live state."""

    get_fn: Callable[[DeferrableRuntime], bool]
    set_fn: Callable[[DeferrableRuntime, bool], None]
    default: bool

    available_fn: Callable[[DeferrableRuntime], bool] | None = None
    """When this setting is actually consulted; see the matching field on
    ``LoadNumberDescription``."""

    applies_fn: Callable[[DeferrableRuntime], bool] | None = None
    """Whether this load has the setting at all; see the matching field on
    ``LoadNumberDescription``."""

    attrs_fn: Callable[[DeferrableRuntime], dict[str, Any]] | None = None
    """Extra state attributes. Also how state that is not itself an entity
    survives a restart -- ``async_get_last_state`` restores attributes too."""

    restore_fn: Callable[[DeferrableRuntime, dict[str, Any]], None] | None = None
    """Rebuild that extra state from the restored attributes."""


def _set_enabled(load: DeferrableRuntime, value: bool) -> None:
    load.enabled = value


def _set_use_window(load: DeferrableRuntime, value: bool) -> None:
    load.use_time_window = value


def _set_semi_continuous(load: DeferrableRuntime, value: bool) -> None:
    load.semi_continuous = value


def _set_single_constant(load: DeferrableRuntime, value: bool) -> None:
    load.single_constant = value


def _set_requested(load: DeferrableRuntime, value: bool) -> None:
    # Never a bare assignment: a request without its anchor is a deadline with
    # nothing to count from.
    if value:
        load.request(dt_util.utcnow())
    else:
        load.cancel()


def _requested_attrs(load: DeferrableRuntime) -> dict[str, Any]:
    """Expose the request's anchor, deadline and progress.

    All three are read back by the dashboard card, and ``requested_at`` /
    ``request_runtime_seconds`` are how the anchor and the run made towards it
    survive a restart without being entities of their own. Without the latter,
    a restart mid-run (or even after the run already met its target but before
    the next refresh auto-disarmed it) would forget everything done since
    ``requested_at`` while keeping that same, now stale, anchor -- making an
    already-progressed request look freshly armed and already overdue.
    """
    deadline = load.deadline_at
    return {
        ATTR_REQUESTED_AT: load.requested_at.isoformat() if load.requested_at else None,
        ATTR_DEADLINE_AT: deadline.isoformat() if deadline else None,
        ATTR_REQUEST_RUNTIME_SECONDS: load.request_runtime.total_seconds(),
    }


def _restore_requested(load: DeferrableRuntime, attributes: dict[str, Any]) -> None:
    if not load.requested:
        return
    if (raw := attributes.get(ATTR_REQUESTED_AT)) and (parsed := dt_util.parse_datetime(raw)):
        load.requested_at = parsed
    else:
        # Armed, but with no anchor to be found -- a request restored from
        # before this attribute existed, or a corrupted one. Anchoring at
        # startup keeps any deadline generous rather than instantly expired.
        load.requested_at = dt_util.utcnow()

    seconds = attributes.get(ATTR_REQUEST_RUNTIME_SECONDS)
    if isinstance(seconds, (int, float)) and seconds > 0:
        load.request_runtime = timedelta(seconds=seconds)


LOAD_SWITCHES: tuple[LoadSwitchDescription, ...] = (
    LoadSwitchDescription(
        key="enabled",
        translation_key="load_enabled",
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.enabled,
        set_fn=_set_enabled,
        default=True,
    ),
    LoadSwitchDescription(
        key="use_time_window",
        translation_key="use_time_window",
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.use_time_window,
        set_fn=_set_use_window,
        default=False,
        # A surplus load's window is the sun's, derived fresh from every plan.
        # A standing wall-clock one is not narrowed by it, it is simply never
        # read, so offering the switch would be a lie.
        available_fn=lambda load: not load.on_surplus,
    ),
    # Both of these change what EMHASS is asked to solve rather than how the
    # answer is applied, so they belong to the load's live state: a car that
    # charges at a variable rate on a sunny afternoon and at full power
    # overnight is one load whose model changes, not two loads.
    LoadSwitchDescription(
        key="semi_continuous",
        translation_key="semi_continuous",
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.semi_continuous,
        set_fn=_set_semi_continuous,
        default=True,
    ),
    LoadSwitchDescription(
        key="single_constant",
        translation_key="single_constant",
        entity_category=EntityCategory.CONFIG,
        get_fn=lambda load: load.single_constant,
        set_fn=_set_single_constant,
        default=False,
        # "One unbroken block" is a constraint on a run-time target, which a
        # thermal load does not have.
        applies_fn=lambda load: not load.is_thermal,
    ),
    # Not a config switch: arming a load is a day-to-day action, not a
    # setup-time setting, so it belongs with the load's primary entities
    # rather than tucked into Configuration. Meaningful for both
    # request-driven recurrences; see docs/on_demand_loads.md and
    # docs/surplus_loads.md.
    #
    # Deliberately one switch rather than one per recurrence. "This load is
    # asking to run" is the same statement either way -- what differs is only
    # how the request ends, which is the registry's business and not something
    # a second entity would make clearer. A per-recurrence switch would also
    # sit permanently unavailable in whichever mode was not selected.
    LoadSwitchDescription(
        key="requested",
        translation_key="load_requested",
        get_fn=lambda load: load.requested,
        set_fn=_set_requested,
        default=False,
        # A daily load already has standing demand, so arming one does nothing
        # (see DeferrableRuntime.participates). Reporting unavailable says so
        # instead of letting the switch look like it did something.
        available_fn=lambda load: load.armable,
        # A thermal load's demand is its comfort band; there is nothing to arm.
        applies_fn=lambda load: not load.is_thermal,
        attrs_fn=_requested_attrs,
        restore_fn=_restore_requested,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: EmhassCoordinator = entry.runtime_data.coordinator
    async_add_entities([EmhassControlSwitch(coordinator)])

    for load in entry.runtime_data.loads.all():
        async_add_entities(
            (
                LoadSwitch(coordinator, load, description)
                for description in LOAD_SWITCHES
                if description.applies_fn is None or description.applies_fn(load)
            ),
            config_subentry_id=load.subentry_id,
        )


class EmhassControlSwitch(EmhassEntity, SwitchEntity, RestoreEntity):
    """Master gate on whether the integration may act on the plan.

    Ships **off**. Anyone installing this already has working automations, and
    handing control to a newly configured optimiser before they have seen it
    make sensible decisions is how people end up charging a battery at the
    day's peak price. While off, the executor still computes and records what
    it *would* have done, so its judgement can be compared against the existing
    setup before anything is handed over.
    """

    _attr_translation_key = "control_enabled"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: EmhassCoordinator) -> None:
        super().__init__(coordinator, "control_enabled")
        self._is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # A switch's value *is* its state, so plain RestoreEntity is correct
        # here; number and select entities need their typed Restore* variants
        # because those restore a formatted string rather than a native value.
        if (last_state := await self.async_get_last_state()) is not None:
            self._is_on = last_state.state == STATE_ON
        self._publish()

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._set(False)

    def _set(self, value: bool) -> None:
        self._is_on = value
        self._publish()
        self.async_write_ha_state()

    def _publish(self) -> None:
        # The executor reads this from the coordinator rather than parsing the
        # entity state, so the gate is never ambiguous while the entity is
        # still restoring.
        self.coordinator.control_enabled = self._is_on


class LoadSwitch(EmhassLoadEntity, SwitchEntity, RestoreEntity):
    """One boolean setting of one deferrable load."""

    entity_description: LoadSwitchDescription

    def __init__(
        self,
        coordinator: EmhassCoordinator,
        load: DeferrableRuntime,
        description: LoadSwitchDescription,
    ) -> None:
        super().__init__(coordinator, load, description.key)
        self.entity_description = description

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Only a genuine on/off restores. An unknown/unavailable last state
        # would otherwise read as "off" and silently flip every switch whose
        # default is on -- turning a load off, or turning a semi-continuous
        # load into a variable-power one, on the first restart after adding it.
        if (last := await self.async_get_last_state()) is not None and last.state in (
            STATE_ON,
            STATE_OFF,
        ):
            self.entity_description.set_fn(self.load, last.state == STATE_ON)
            # After set_fn, which for a request installs a fresh anchor that
            # the restored one must overwrite.
            if (restore_fn := self.entity_description.restore_fn) is not None:
                restore_fn(self.load, dict(last.attributes))

    @property
    def available(self) -> bool:
        if (available_fn := self.entity_description.available_fn) is None:
            return super().available
        return super().available and available_fn(self.load)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if (attrs_fn := self.entity_description.attrs_fn) is None:
            return None
        return attrs_fn(self.load)

    @property
    def is_on(self) -> bool:
        return self.entity_description.get_fn(self.load)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, value: bool) -> None:
        self.entity_description.set_fn(self.load, value)
        self.load.notify()
        await self.coordinator.async_request_refresh()

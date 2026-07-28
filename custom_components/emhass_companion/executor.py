"""Turn the current plan into actions.

This is the only module that writes anything. Everything it does is gated on
``switch.emhass_control_enabled``, which ships off: anyone installing this
already has working automations, and handing control to a newly configured
optimiser before watching it make sensible decisions is how a battery ends up
charging at the day's peak price.

While the gate is off the executor still computes and records exactly what it
*would* have done. That is the migration path -- run it alongside an existing
setup, compare the decisions, then hand over.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .configuration import EmhassConfig
from .const import (
    DEFAULT_POWER_DEADBAND_W,
    MODE_AUTO,
    MODE_IDLE,
    MODE_SELF_CONSUME,
)
from .coordinator import EmhassCoordinator
from .deferrable import DeferrableRuntime, resolve_should_run
from .profiles import (
    Profile,
    ProfileError,
    async_execute_steps,
    render_action,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Decision:
    """What the executor concluded, and why."""

    action: str
    power_w: float = 0.0
    reason: str = ""
    loads: dict[str, bool] = field(default_factory=dict)
    """Subentry id -> whether the load should be running."""

    applied: bool = False
    """False when the control gate is off, or nothing needed changing."""

    steps: list[dict[str, Any]] = field(default_factory=list)
    """The service calls this decision resolved to."""

    error: str | None = None
    at: datetime | None = None

    def as_attributes(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "power_w": round(self.power_w),
            "reason": self.reason,
            "applied": self.applied,
            "loads": self.loads,
            "steps": self.steps,
            "error": self.error,
            "at": self.at.isoformat() if self.at else None,
        }


class Executor:
    """Applies the plan to the user's own integrations."""

    def __init__(self, hass: HomeAssistant, coordinator: EmhassCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.last_decision: Decision | None = None
        self._last_applied: tuple[str, float] | None = None
        self._listeners: list = []

    # -- gates ----------------------------------------------------------------

    @property
    def control_enabled(self) -> bool:
        """Whether the executor may actually issue service calls."""
        return self.coordinator.control_enabled

    @property
    def system_mode(self) -> str:
        return self.coordinator.system_mode

    # -- main entry point -----------------------------------------------------

    async def async_apply(self) -> Decision:
        """Decide what should happen now, and do it if permitted."""
        decision = self._decide()
        decision.at = dt_util.utcnow()

        profile = self._inverter_profile()
        if profile is not None:
            try:
                decision.steps = render_action(
                    self.hass,
                    profile,
                    self.coordinator.config.inverter.options,
                    decision.action,
                    power_w=round(decision.power_w),
                    soc=self.coordinator.soc_percent,
                    soc_target=self.coordinator.config.battery.soc_target * 100,
                )
            except ProfileError as err:
                decision.error = str(err)
                _LOGGER.error("Cannot resolve inverter action: %s", err)

        if not self.control_enabled:
            decision.reason = f"{decision.reason} (control disabled, not applied)"
            self.last_decision = decision
            _LOGGER.debug("Would apply %s: %s", decision.action, decision.reason)
            return decision

        await self._async_execute(decision)
        self.last_decision = decision
        return decision

    # -- decision -------------------------------------------------------------

    def _decide(self) -> Decision:
        config = self.coordinator.config
        mode = self.system_mode

        if mode != MODE_AUTO:
            # A manual mode suspends the optimiser entirely rather than
            # competing with it.
            return Decision(
                action=mode,
                power_w=self._manual_power(mode, config),
                reason="manual override",
                loads=self._decide_loads(use_plan=False),
            )

        if self.coordinator.plan_is_stale or not self.coordinator.data.plan:
            # A plan that stopped being refreshed describes a world that no
            # longer exists. Falling back beats continuing to follow it.
            return Decision(
                action=MODE_SELF_CONSUME,
                reason="no current plan; falling back to self-consumption",
                loads=self._decide_loads(use_plan=False),
            )

        row = self.coordinator.data.plan.row_at(dt_util.utcnow())
        if row is None or row.p_batt is None:
            return Decision(
                action=MODE_SELF_CONSUME,
                reason="plan has no battery power for this moment",
                loads=self._decide_loads(use_plan=True),
            )

        action, power = _battery_action(row.p_batt, config)
        return Decision(
            action=action,
            power_w=power,
            reason=f"plan schedules {row.p_batt:.0f} W",
            loads=self._decide_loads(use_plan=True),
        )

    def _manual_power(self, mode: str, config: EmhassConfig) -> float:
        if mode == "force_charge":
            return config.battery.charge_power_max_w
        if mode == "force_discharge":
            return config.battery.discharge_power_max_w
        return 0.0

    def _decide_loads(self, *, use_plan: bool) -> dict[str, bool]:
        """Whether each deferrable load should be running."""
        decisions: dict[str, bool] = {}
        for load in self.coordinator.loads.all():
            if not load.enabled:
                continue
            scheduled = self._scheduled(load) if use_plan else False
            decisions[load.subentry_id] = resolve_should_run(load.mode, scheduled)
        return decisions

    def _scheduled(self, load: DeferrableRuntime) -> bool:
        data = self.coordinator.data
        if data.plan is None or (index := data.deferrable_index(load.subentry_id)) is None:
            return False
        row = data.plan.row_at(dt_util.utcnow())
        if row is None or index >= len(row.deferrables):
            return False
        return row.deferrables[index] > load.running_threshold_w

    # -- execution ------------------------------------------------------------

    async def _async_execute(self, decision: Decision) -> None:
        await self._async_apply_battery(decision)
        await self._async_apply_loads(decision)

    async def _async_apply_battery(self, decision: Decision) -> None:
        if not decision.steps:
            return

        current = (decision.action, decision.power_w)
        if self._unchanged(current):
            _LOGGER.debug("Skipping %s: unchanged within the deadband", decision.action)
            return

        try:
            await async_execute_steps(self.hass, decision.steps)
        except Exception as err:  # noqa: BLE001 - surfaced, and must not stop loads
            decision.error = str(err)
            _LOGGER.error("Failed to apply inverter action %s: %s", decision.action, err)
            return

        self._last_applied = current
        decision.applied = True
        _LOGGER.info(
            "Applied %s at %.0f W (%s)", decision.action, decision.power_w, decision.reason
        )

    def _unchanged(self, current: tuple[str, float]) -> bool:
        """Whether this is close enough to the last applied command to skip.

        The deadband only applies while the action itself is unchanged -- a
        switch from charging to discharging is always issued, however small the
        power difference.
        """
        if self._last_applied is None:
            return False
        last_action, last_power = self._last_applied
        if last_action != current[0]:
            return False
        return abs(current[1] - last_power) < DEFAULT_POWER_DEADBAND_W

    async def _async_apply_loads(self, decision: Decision) -> None:
        for subentry_id, should_run in decision.loads.items():
            load = self.coordinator.loads.get(subentry_id)
            if load is None or not load.control_entity:
                continue
            await self._async_set_load(load, should_run, decision)

    async def _async_set_load(
        self, load: DeferrableRuntime, should_run: bool, decision: Decision
    ) -> None:
        entity_id = load.control_entity
        state = self.hass.states.get(entity_id)
        if state is None:
            _LOGGER.warning("Control entity %s for %s does not exist", entity_id, load.name)
            return

        is_on = state.state == "on"
        if is_on == should_run:
            return

        domain = entity_id.partition(".")[0]
        service = "turn_on" if should_run else "turn_off"
        try:
            await self.hass.services.async_call(
                domain, service, {}, blocking=True, target={"entity_id": entity_id}
            )
        except Exception as err:  # noqa: BLE001 - one bad load must not stop the rest
            decision.error = f"{load.name}: {err}"
            _LOGGER.error("Failed to switch %s: %s", load.name, err)
            return

        decision.applied = True
        _LOGGER.info("Turned %s %s", load.name, "on" if should_run else "off")

    # -- profile --------------------------------------------------------------

    def _inverter_profile(self) -> Profile | None:
        selection = self.coordinator.config.inverter
        if not selection.key:
            return None
        return self.coordinator.profiles.get(selection.key)


def _battery_action(p_batt: float, config: EmhassConfig) -> tuple[str, float]:
    """Map planned battery power onto an action.

    EMHASS's convention: positive is discharge, negative is charge. Getting
    that backwards inverts every decision the optimiser makes, so it is stated
    here rather than assumed at the call site.
    """
    if p_batt > DEFAULT_POWER_DEADBAND_W:
        return "force_discharge", min(p_batt, config.battery.discharge_power_max_w)
    if p_batt < -DEFAULT_POWER_DEADBAND_W:
        return "force_charge", min(-p_batt, config.battery.charge_power_max_w)
    return MODE_IDLE, 0.0


__all__ = ["Decision", "Executor"]

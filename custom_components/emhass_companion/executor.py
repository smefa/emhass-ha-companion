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

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .configuration import EmhassConfig
from .const import (
    ACTION_CURTAIL,
    ACTION_PREPARE,
    ACTION_RESTORE,
    ACTION_UNCURTAIL,
    COMMAND_REFRESH_FRACTION,
    DEFAULT_POWER_DEADBAND_W,
    LIFETIME_PERSISTENT,
    MODE_AUTO,
    MODE_FORCE_CHARGE,
    MODE_FORCE_DISCHARGE,
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
from .strategy import decide_battery, decide_curtailment

_LOGGER = logging.getLogger(__name__)

# Keys into Executor._last_applied. Two independent write-suppression tracks,
# since a curtailment write must never be suppressed by the battery's deadband
# or vice versa -- they are different axes of the same inverter.
AXIS_BATTERY: Final = "battery"
AXIS_CURTAIL: Final = "curtail"


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
    """The service calls this decision resolved to -- battery and curtailment
    both, in the order they would be applied."""

    error: str | None = None
    at: datetime | None = None

    curtail: bool | None = None
    """Whether to curtail export right now. None means not applicable: no
    inverter profile, or the profile defines no curtail/uncurtail actions --
    the set of actions a profile defines is its capability list, and reporting
    a would-be curtail decision for hardware that cannot act on it would be
    reporting a capability the install does not have."""

    curtail_w: float = 0.0

    rules: list[str] = field(default_factory=list)
    """Ordered, human-readable trace of which strategy rule fired. The old
    hand-written automation this replaces was a five-level nested `choose`
    with no way to tell which branch ran; this is what makes that visible on
    the sensor, in dry-run, before control is ever handed over."""

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
            "curtail": self.curtail,
            "curtail_w": round(self.curtail_w),
            "rules": self.rules,
        }


@dataclass(slots=True)
class _Command:
    """The last command actually issued on one axis, and when."""

    action: str
    power_w: float
    at: datetime


class Executor:
    """Applies the plan to the user's own integrations."""

    def __init__(self, hass: HomeAssistant, coordinator: EmhassCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.last_decision: Decision | None = None
        self._last_applied: dict[str, _Command] = {}
        self._listeners: list = []
        # Whether `prepare` has run since control was last handed back. Some
        # inverters gate remote control behind a mode that has to be opened
        # once per session rather than before every write.
        self._prepared = False
        # Used to notice the control gate being switched *off*, which is one of
        # the moments an inverter has to be given back.
        self._control_was_enabled = False
        # Every write to the inverter goes through here, one at a time. Applies
        # are fired from two independent sources -- every coordinator update
        # and every clock tick -- and a restore can be triggered from a third
        # (shutdown, unload, the control gate). Without this, two overlapping
        # applies interleave their reads and writes of `_last_applied`: both
        # see the same "last command", both decide the write is worth sending,
        # and the inverter gets the same command twice; worse, a restore
        # landing mid-apply hands control back and is then immediately undone
        # by the apply's own write, which is the state this executor is meant
        # to be incapable of leaving behind.
        self._lock = asyncio.Lock()

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
        """Decide what should happen now, and do it if permitted.

        Serialised against every other apply and against ``async_restore``:
        see ``_lock``. The decision itself is made inside the lock too, not
        just the writes -- deciding against a ``_last_applied`` that another
        apply is about to change is how the same command ends up sent twice.
        """
        async with self._lock:
            return await self._async_apply()

    async def _async_apply(self) -> Decision:
        decision = self._decide()
        decision.at = dt_util.utcnow()

        profile = self._inverter_profile()
        resolved_action = decision.action
        battery_steps: list[dict[str, Any]] = []
        curtail_action = ACTION_UNCURTAIL
        curtail_steps: list[dict[str, Any]] = []

        if profile is None:
            decision.curtail = None
        else:
            resolved_action = self._resolve_action(profile, decision.action)
            if resolved_action != decision.action:
                decision.rules.append(
                    f"{decision.action}→{resolved_action}: profile defines no "
                    f"'{decision.action}' action"
                )
            try:
                battery_steps = render_action(
                    self.hass,
                    profile,
                    self.coordinator.config.inverter.options,
                    resolved_action,
                    power_w=round(decision.power_w),
                    soc=self.coordinator.soc_percent,
                    soc_target=self.coordinator.config.battery.soc_target * 100,
                )
            except ProfileError as err:
                decision.error = str(err)
                _LOGGER.error("Cannot resolve inverter action: %s", err)

            if profile.defines(ACTION_CURTAIL) and profile.defines(ACTION_UNCURTAIL):
                curtail_action = ACTION_CURTAIL if decision.curtail else ACTION_UNCURTAIL
                try:
                    curtail_steps = render_action(
                        self.hass,
                        profile,
                        self.coordinator.config.inverter.options,
                        curtail_action,
                        power_w=0,
                        curtail_w=round(decision.curtail_w),
                        soc=self.coordinator.soc_percent,
                        soc_target=self.coordinator.config.battery.soc_target * 100,
                    )
                except ProfileError as err:
                    decision.error = f"{decision.error}; {err}" if decision.error else str(err)
                    _LOGGER.error("Cannot resolve curtailment action: %s", err)
            else:
                # The set of actions a profile defines is its capability list:
                # no curtail/uncurtail pair means curtailment is not
                # applicable, not that it is off right now.
                decision.curtail = None

        decision.steps = [*battery_steps, *curtail_steps]

        if not self.control_enabled:
            # Turning the gate off mid-session is a handover, not just a stop:
            # a persistent-register inverter would otherwise sit in whatever
            # forced mode the last command left it in, indefinitely.
            if self._control_was_enabled:
                self._control_was_enabled = False
                # Already holding _lock, so the unlocked body directly.
                await self._async_restore("control switch turned off")
            decision.reason = f"{decision.reason} (control disabled, not applied)"
            self.last_decision = decision
            _LOGGER.debug("Would apply %s: %s", decision.action, decision.reason)
            return decision

        self._control_was_enabled = True
        await self._async_execute(
            decision, resolved_action, battery_steps, curtail_action, curtail_steps
        )
        self.last_decision = decision
        return decision

    async def async_restore(self, reason: str) -> None:
        """Hand control of the battery, and any curtailment, back to the inverter.

        Runs on every path that ends a control session -- shutdown, unload, the
        control gate being switched off -- not just on a stale plan. For an
        inverter whose registers persist until changed, skipping this is what
        leaves a battery force-charging through the evening peak because Home
        Assistant restarted at the wrong moment. An export limit left in place
        is the same failure mode for money instead of the battery: silent and
        indefinite, so it is restored first, before the battery.

        Deliberately bypasses the deadband and the "nothing changed" check: the
        whole point is to write even when the last command still looks current.
        It also bypasses the control gate, because the gate being switched off
        is one of the things it has to react to -- checking it here would skip
        the handover in precisely the case the handover exists for. The two
        restores run independently so a failure in one still lets the other
        through.

        What it does check is whether this executor ever took control. Writing
        to an inverter we have never written to would mean a dry run reaching
        for the hardware on shutdown, and fighting whatever automation is
        actually in charge.

        Serialised against ``async_apply`` (see ``_lock``): a handover that
        interleaves with an in-flight apply is a handover the apply's own
        write immediately undoes.
        """
        async with self._lock:
            await self._async_restore(reason)

    async def _async_restore(self, reason: str) -> None:
        profile = self._inverter_profile()
        if profile is None or (not self._last_applied and not self._prepared):
            return

        await self._async_restore_curtailment(profile, reason)
        await self._async_restore_battery(profile, reason)

    async def _async_restore_curtailment(self, profile: Profile, reason: str) -> None:
        if AXIS_CURTAIL not in self._last_applied:
            return
        if not (profile.defines(ACTION_CURTAIL) and profile.defines(ACTION_UNCURTAIL)):
            return

        try:
            steps = render_action(
                self.hass,
                profile,
                self.coordinator.config.inverter.options,
                ACTION_UNCURTAIL,
                power_w=0,
                curtail_w=0,
                soc=self.coordinator.soc_percent,
                soc_target=self.coordinator.config.battery.soc_target * 100,
            )
            await async_execute_steps(self.hass, steps)
        except Exception as err:  # noqa: BLE001 - never let a shutdown path raise
            _LOGGER.error("Could not restore curtailment (%s): %s", reason, err)
            return

        del self._last_applied[AXIS_CURTAIL]
        _LOGGER.info("Restored curtailment: %s", reason)

    async def _async_restore_battery(self, profile: Profile, reason: str) -> None:
        action = ACTION_RESTORE if profile.defines(ACTION_RESTORE) else MODE_SELF_CONSUME
        if not profile.defines(action):
            _LOGGER.debug("Profile %s defines no way to restore control", profile.key)
            return

        try:
            steps = render_action(
                self.hass,
                profile,
                self.coordinator.config.inverter.options,
                action,
                power_w=0,
                soc=self.coordinator.soc_percent,
                soc_target=self.coordinator.config.battery.soc_target * 100,
            )
            await async_execute_steps(self.hass, steps)
        except Exception as err:  # noqa: BLE001 - never let a shutdown path raise
            _LOGGER.error("Could not restore inverter control (%s): %s", reason, err)
            return

        self._last_applied.pop(AXIS_BATTERY, None)
        self._prepared = False
        _LOGGER.info("Restored inverter control: %s", reason)

    # -- decision -------------------------------------------------------------

    def _decide(self) -> Decision:
        config = self.coordinator.config
        mode = self.system_mode

        if mode != MODE_AUTO:
            # A manual mode suspends the optimiser entirely rather than
            # competing with it. No plan is being followed, so there is no
            # basis for curtailing either -- uncurtail rather than leave
            # whatever the last automatic decision happened to set.
            return Decision(
                action=mode,
                power_w=self._manual_power(mode, config),
                reason="manual override",
                loads=self._decide_loads(use_plan=False),
                curtail=False,
                rules=["manual override active; uncurtailing"],
            )

        if self.coordinator.plan_is_stale or not self.coordinator.data.plan:
            # A plan that stopped being refreshed describes a world that no
            # longer exists. Falling back beats continuing to follow it.
            return Decision(
                action=MODE_SELF_CONSUME,
                reason="no current plan; falling back to self-consumption",
                loads=self._decide_loads(use_plan=False),
                curtail=False,
                rules=["no current plan; uncurtailing"],
            )

        row = self.coordinator.data.plan.row_at(dt_util.utcnow())
        if row is None or row.p_batt is None:
            return Decision(
                action=MODE_SELF_CONSUME,
                reason="plan has no battery power for this moment",
                loads=self._decide_loads(use_plan=True),
                curtail=False,
                rules=["plan has no battery power for this moment; uncurtailing"],
            )

        in_self_consume = (
            self.last_decision is not None and self.last_decision.action == MODE_SELF_CONSUME
        )
        action, power, battery_rules = decide_battery(row, config, in_self_consume=in_self_consume)
        curtail, curtail_w, curtail_rules = decide_curtailment(row)
        return Decision(
            action=action,
            power_w=power,
            reason=f"plan schedules {row.p_batt:.0f} W",
            loads=self._decide_loads(use_plan=True),
            curtail=curtail,
            curtail_w=curtail_w,
            rules=[*battery_rules, *curtail_rules],
        )

    def _manual_power(self, mode: str, config: EmhassConfig) -> float:
        if mode == MODE_FORCE_CHARGE:
            return config.battery.charge_power_max_w
        if mode == MODE_FORCE_DISCHARGE:
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

    async def _async_execute(
        self,
        decision: Decision,
        resolved_action: str,
        battery_steps: list[dict[str, Any]],
        curtail_action: str,
        curtail_steps: list[dict[str, Any]],
    ) -> None:
        await self._async_apply_battery(decision, resolved_action, battery_steps)
        await self._async_apply_curtailment(decision, curtail_action, curtail_steps)
        await self._async_apply_loads(decision)

    async def _async_apply_battery(
        self, decision: Decision, resolved_action: str, steps: list[dict[str, Any]]
    ) -> None:
        if not steps:
            return

        profile = self._inverter_profile()
        now = dt_util.utcnow()
        if not self._should_issue(
            axis=AXIS_BATTERY,
            action=resolved_action,
            power_w=decision.power_w,
            profile=profile,
            now=now,
        ):
            return

        try:
            if profile is not None:
                await self._async_prepare(profile, decision)
            await async_execute_steps(self.hass, steps)
        except Exception as err:  # noqa: BLE001 - surfaced, and must not stop loads/curtail
            decision.error = str(err)
            _LOGGER.error("Failed to apply inverter action %s: %s", resolved_action, err)
            return

        self._last_applied[AXIS_BATTERY] = _Command(resolved_action, decision.power_w, now)
        decision.applied = True
        _LOGGER.info(
            "Applied %s at %.0f W (%s)", resolved_action, decision.power_w, decision.reason
        )

    async def _async_apply_curtailment(
        self, decision: Decision, curtail_action: str, steps: list[dict[str, Any]]
    ) -> None:
        if not steps or decision.curtail is None:
            return

        profile = self._inverter_profile()
        now = dt_util.utcnow()
        if not self._should_issue(
            axis=AXIS_CURTAIL,
            action=curtail_action,
            power_w=decision.curtail_w,
            profile=profile,
            now=now,
        ):
            return

        try:
            await async_execute_steps(self.hass, steps)
        except Exception as err:  # noqa: BLE001 - one bad axis must not stop the other
            decision.error = f"{decision.error}; {err}" if decision.error else str(err)
            _LOGGER.error("Failed to apply curtailment action %s: %s", curtail_action, err)
            return

        self._last_applied[AXIS_CURTAIL] = _Command(curtail_action, decision.curtail_w, now)
        decision.applied = True
        _LOGGER.info("Applied %s (%s)", curtail_action, decision.reason)

    async def _async_prepare(self, profile: Profile, decision: Decision) -> None:
        """Open the inverter's remote-control gate, once per session.

        SolarEdge needs its storage control mode set to Remote Control, Victron
        needs ESS switched to external control, Sigenergy needs remote EMS
        enabled. All of them are once-per-session rather than once-per-write.
        """
        if self._prepared or not profile.defines(ACTION_PREPARE):
            return
        steps = render_action(
            self.hass,
            profile,
            self.coordinator.config.inverter.options,
            ACTION_PREPARE,
            power_w=round(decision.power_w),
            soc=self.coordinator.soc_percent,
            soc_target=self.coordinator.config.battery.soc_target * 100,
        )
        await async_execute_steps(self.hass, steps)
        self._prepared = True
        _LOGGER.debug("Prepared %s for remote control", profile.key)

    def _should_issue(
        self, *, axis: str, action: str, power_w: float, profile: Profile | None, now: datetime
    ) -> bool:
        """Whether this command is worth sending, on one axis.

        Three independent reasons to write, and one reason not to:

        * the action changed -- always sent, however small the power difference,
          because charging and discharging (or curtailing and not) are not
          interchangeable;
        * the power moved further than the deadband;
        * the last command is running out of time. An inverter whose forced mode
          carries its own duration reverts on its own, so an unchanged command
          still has to be re-sent before it lapses. Suppressing that as
          "unchanged" is how a battery quietly stops following the plan halfway
          through the evening.

        The one reason not to is a profile's own minimum write interval, for a
        bus that does not tolerate being hammered.

        Keyed by ``axis`` because the battery and curtailment writes are
        independent commands to the same inverter -- a curtailment change must
        never be suppressed by the battery's deadband, or vice versa.
        """
        control = profile.control if profile is not None else {}
        last = self._last_applied.get(axis)

        if last is None:
            return True

        age = (now - last.at).total_seconds()
        if age < float(control.get("min_write_interval_s", 0)):
            _LOGGER.debug("Skipping %s %s: inside the minimum write interval", axis, action)
            return False

        if last.action != action:
            return True

        deadband = float(control.get("deadband_w", DEFAULT_POWER_DEADBAND_W))
        if abs(power_w - last.power_w) >= deadband:
            return True

        if control.get("lifetime", LIFETIME_PERSISTENT) != LIFETIME_PERSISTENT:
            lifetime_s = float(control["duration_min"]) * 60
            if age >= lifetime_s * COMMAND_REFRESH_FRACTION:
                _LOGGER.debug("Re-issuing %s %s before it expires", axis, action)
                return True

        _LOGGER.debug("Skipping %s %s: unchanged within the deadband", axis, action)
        return False

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

    def _resolve_action(self, profile: Profile, action: str) -> str:
        """The action to actually render, when the plan's own choice is undefined.

        The set of actions a profile defines is its capability list
        (const.py): a profile with no ``idle`` is saying its hardware has no
        true standby, not that the executor should refuse a decision the plan
        legitimately produced. ``idle`` falling back to ``self_consume`` is
        the one substitution this makes -- every other battery action is
        already guaranteed to exist by validation (``self_consume``/``restore``
        for ``restore_required``), or has no sensible substitute at all
        (forcing charge instead of discharge is not a fallback, it is the
        opposite decision).
        """
        if profile.defines(action):
            return action
        if action == MODE_IDLE:
            return MODE_SELF_CONSUME
        return action

    def _inverter_profile(self) -> Profile | None:
        selection = self.coordinator.config.inverter
        if not selection.key:
            return None
        return self.coordinator.profiles.get(selection.key)


__all__ = ["Decision", "Executor"]

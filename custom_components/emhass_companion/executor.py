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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import logging
import math
from typing import Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

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
    MODE_IDLE,
    MODE_SELF_CONSUME,
)
from .coordinator import EmhassCoordinator
from .deferrable import DeferrableRuntime, resolve_should_run
from .phase_guard import clamp_blocker, clamp_charge
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

    planned_charge_w: float = 0.0
    """The forced charge the plan asked for, before the phase guard. Zero
    whenever the plan is not asking to charge, which is also how the guard
    knows there is nothing of this decision for it to revisit."""

    phase_cut_w: float = 0.0
    """How much of ``planned_charge_w`` the phase guard took away."""

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
            "planned_charge_w": round(self.planned_charge_w),
            "phase_cut_w": round(self.phase_cut_w),
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
        self._listeners: list[Callable[[], None]] = []
        # Whether `prepare` has run since control was last handed back. Some
        # inverters gate remote control behind a mode that has to be opened
        # once per session rather than before every write.
        self._prepared = False
        # Axes this executor may have written to since control was last handed
        # back. Marked *before* a write rather than after it succeeds: a
        # multi-step action that fails halfway (mode set, power write times
        # out) has still left the inverter somewhere it was not, and only a
        # restore puts it back. `_last_applied` cannot answer this -- it records
        # commands known to have landed, for the deadband. Cleared per axis
        # only by a restore that went through, so a handover that fails is
        # retried rather than forgotten.
        self._held: set[str] = set()
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
        # Set while a phase-guard re-apply is queued or running, so a meter
        # reporting every second queues one apply rather than a pile of them.
        self._phase_apply_pending = False

    # -- gates ----------------------------------------------------------------

    @property
    def control_enabled(self) -> bool:
        """Whether the executor may actually issue service calls."""
        return self.coordinator.control_enabled

    @property
    def system_mode(self) -> str:
        return self.coordinator.system_mode

    # -- change notification --------------------------------------------------

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a callback for "a new decision has been taken".

        Entities showing the decision cannot ride the coordinator for this.
        The apply is scheduled *from* a coordinator listener, so by the time a
        new decision exists every ``CoordinatorEntity`` has already published
        the previous one -- which is how the action sensor came to say
        "force charge" for a full clock tick after the executor had already
        sent the inverter a stop.

        Routing the notification back through ``async_update_listeners`` is
        not an option either: ``_async_plan_updated`` is itself a coordinator
        listener, so every apply would schedule another one, forever.
        """
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def _record(self, decision: Decision) -> None:
        """Publish a decision as the current one, and say so."""
        self.last_decision = decision
        for listener in list(self._listeners):
            listener()

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
            # A phase-guard cap is a ceiling on what is written, not just on
            # what was decided: the profile's boost and rounding come after
            # the clamp, and must not take the command back over it.
            ceiling_w = decision.power_w if decision.phase_cut_w else None
            try:
                battery_steps = render_action(
                    self.hass,
                    profile,
                    self.coordinator.config.inverter.options,
                    resolved_action,
                    power_w=math.floor(decision.power_w)
                    if ceiling_w is not None
                    else round(decision.power_w),
                    ceiling_w=ceiling_w,
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
            # forced mode the last command left it in, indefinitely. Asked on
            # every gated cycle until it has gone through: a handover that
            # failed once (the inverter was briefly unreachable) is exactly
            # the one that still needs doing.
            if self._held:
                # Already holding _lock, so the unlocked body directly.
                await self._async_restore("control switch turned off")
            decision.reason = f"{decision.reason} (control disabled, not applied)"
            # Nothing is being asked of any appliance while the gate is off, so
            # no run may go on being credited with commanded time. Left open,
            # a span would quietly run a request to completion during exactly
            # the period the integration was told to keep its hands off.
            for load in self.coordinator.loads.all():
                load.observe_command(False, decision.at or dt_util.utcnow())
            self._record(decision)
            _LOGGER.debug("Would apply %s: %s", decision.action, decision.reason)
            return decision

        await self._async_execute(
            decision, resolved_action, battery_steps, curtail_action, curtail_steps
        )
        self._record(decision)
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

        What it does check is whether this executor may have written to each
        axis since the last handover (``_held``) -- including a write that
        raised partway. Writing to an inverter we have never written to would
        mean a dry run reaching for the hardware on shutdown, and fighting
        whatever automation is actually in charge.

        Serialised against ``async_apply`` (see ``_lock``): a handover that
        interleaves with an in-flight apply is a handover the apply's own
        write immediately undoes.
        """
        async with self._lock:
            await self._async_restore(reason)

    async def _async_restore(self, reason: str) -> None:
        profile = self._inverter_profile()
        if profile is None or not self._held:
            return

        await self._async_restore_curtailment(profile, reason)
        await self._async_restore_battery(profile, reason)

    async def _async_restore_curtailment(self, profile: Profile, reason: str) -> None:
        if AXIS_CURTAIL not in self._held:
            return
        if not (profile.defines(ACTION_CURTAIL) and profile.defines(ACTION_UNCURTAIL)):
            # Nothing left to undo it with (the profile changed under us), and
            # asking again every cycle would not change that.
            self._held.discard(AXIS_CURTAIL)
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

        # pop, not del: a write that raised partway is held without ever
        # having been recorded as applied.
        self._last_applied.pop(AXIS_CURTAIL, None)
        self._held.discard(AXIS_CURTAIL)
        _LOGGER.info("Restored curtailment: %s", reason)

    async def _async_restore_battery(self, profile: Profile, reason: str) -> None:
        if AXIS_BATTERY not in self._held:
            # Only curtailment was ever written. Handing back a battery we
            # never commanded is a write nobody asked for -- and, with the
            # handover retried until it goes through, one that would repeat
            # every cycle a curtailment restore keeps failing.
            return
        action = ACTION_RESTORE if profile.defines(ACTION_RESTORE) else MODE_SELF_CONSUME
        if not profile.defines(action):
            _LOGGER.debug("Profile %s defines no way to restore control", profile.key)
            self._held.discard(AXIS_BATTERY)
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
        self._held.discard(AXIS_BATTERY)
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
            #
            # Every selectable mode (SYSTEM_MODES) is a zero-power steady
            # state, so there is no manual power to work out: self-consumption
            # hands the battery to the inverter's own logic and idle stops it.
            return Decision(
                action=mode,
                power_w=0.0,
                reason="manual override",
                loads=self._decide_loads(use_plan=False),
                curtail=False,
                rules=["manual override active; uncurtailing"],
            )

        if blind := self.coordinator.blind_sources:
            # The readings this would command the inverter from have been gone
            # long enough to be a fault rather than a blip (health.py). The
            # plan itself is still fresh, so it is not stale -- it is worse
            # than stale: it was solved from a configured SOC constant that
            # has no relationship to how full the battery actually is, and
            # acting on it can push a full battery or flatten an empty one.
            #
            # The load schedule is left alone. Those rows are not made unsafe
            # by a missing battery reading, only less well timed, and stopping
            # every appliance in the house is a far bigger intervention than
            # this fault warrants.
            return Decision(
                action=MODE_SELF_CONSUME,
                reason=(
                    f"source readings unavailable ({', '.join(blind)}); "
                    "handing the battery back to the inverter"
                ),
                loads=self._decide_loads(use_plan=self._plan_usable()),
                curtail=False,
                rules=[f"{entity_id} is unavailable; not commanding blind" for entity_id in blind],
            )

        if not self._plan_usable():
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

        # Self-consumption the phase guard forced is not the plan's choice, so
        # it must not make leaving self-consumption harder once the phases
        # clear -- that hysteresis is for the plan's own boundary chatter.
        in_self_consume = (
            self.last_decision is not None
            and self.last_decision.action == MODE_SELF_CONSUME
            and not self.last_decision.phase_cut_w
        )
        action, power, battery_rules = decide_battery(row, config, in_self_consume=in_self_consume)
        planned_charge_w = power if action == MODE_FORCE_CHARGE else 0.0
        phase_cut_w = 0.0
        if planned_charge_w:
            action, power, phase_cut_w, phase_rules = self._phase_clamp(planned_charge_w)
            battery_rules.extend(phase_rules)
        curtail, curtail_w, curtail_rules = decide_curtailment(row)
        return Decision(
            action=action,
            power_w=power,
            reason=f"plan schedules {row.p_batt:.0f} W",
            loads=self._decide_loads(use_plan=True),
            curtail=curtail,
            curtail_w=curtail_w,
            rules=[*battery_rules, *curtail_rules],
            planned_charge_w=planned_charge_w,
            phase_cut_w=phase_cut_w,
        )

    def _phase_clamp(self, planned_w: float) -> tuple[str, float, float, list[str]]:
        """A planned forced charge, cut to what the main fuse's phases allow now.

        Only a forced charge is ever touched. Discharge and self-consumption
        both take load off the grid, and the plan's own import limit already
        covers the aggregate -- what it cannot see is the worst phase between
        two solves, which is what this is for.
        """
        guard = self.coordinator.phase_guard
        if guard is None:
            return MODE_FORCE_CHARGE, planned_w, 0.0, []
        if blocker := self.phase_clamp_blocker():
            return (
                MODE_FORCE_CHARGE,
                planned_w,
                0.0,
                [f"phase guard: clamp off ({blocker}); charge not limited"],
            )
        if not guard.available:
            return (
                MODE_FORCE_CHARGE,
                planned_w,
                0.0,
                ["phase guard: phase readings unavailable; charge not limited"],
            )
        if not guard.charge_ready:
            return (
                MODE_FORCE_CHARGE,
                planned_w,
                0.0,
                ["phase guard: battery power unavailable; charge not limited"],
            )
        return clamp_charge(planned_w, guard.charge_cap_w)

    def phase_clamp_blocker(self) -> str | None:
        """Why the phase clamp cannot run here, or None. See clamp_blocker."""
        config = self.coordinator.config
        profile = self._inverter_profile()
        control = profile.control if profile is not None else {}
        return clamp_blocker(
            battery_enabled=config.battery.enabled,
            battery_power_entity=config.battery_power_entity,
            min_write_interval_s=float(control.get("min_write_interval_s", 0)),
        )

    # -- phase guard ----------------------------------------------------------

    @callback
    def async_phase_changed(self) -> None:
        """React to a new phase reading between plans, if the clamp would move.

        Runs on every meter update, so it is only a comparison until there is
        something to do. A full apply rather than a battery-only write, so the
        published decision, the rules trace and the inverter never disagree.
        """
        if self._phase_apply_pending or not self._phase_retarget_needed():
            return
        self._phase_apply_pending = True
        self.coordinator.config_entry.async_create_background_task(
            self.hass, self._async_phase_apply(), "emhass_phase_guard_apply", eager_start=False
        )

    async def _async_phase_apply(self) -> None:
        try:
            await self.async_apply()
        finally:
            self._phase_apply_pending = False

    def _phase_retarget_needed(self) -> bool:
        decision = self.last_decision
        guard = self.coordinator.phase_guard
        if guard is None or decision is None or not decision.planned_charge_w:
            return False
        if self.phase_clamp_blocker():
            return False
        if decision.action not in (MODE_FORCE_CHARGE, MODE_SELF_CONSUME):
            # The plan has moved on (manual mode, stale plan) since this
            # decision; the next regular apply owns it.
            return False
        cap = guard.charge_cap_w
        action, power, _cut, _rules = clamp_charge(decision.planned_charge_w, cap)
        deadband = self._deadband_w()
        if action != decision.action or abs(power - decision.power_w) >= deadband:
            return True
        # Decided, but not yet written: a profile's minimum write interval can
        # hold a cut back. Keep asking until it lands -- a change of action
        # whatever the watts (150 W of charge still has to become
        # self-consumption under a 200 W deadband), and a lowering by more
        # than the deadband.
        last = self._last_applied.get(AXIS_BATTERY)
        if not self.control_enabled or last is None:
            return False
        profile = self._inverter_profile()
        target = self._resolve_action(profile, action) if profile is not None else action
        if last.action != target:
            return True
        return last.action == MODE_FORCE_CHARGE and last.power_w - power >= deadband

    def _deadband_w(self) -> float:
        profile = self._inverter_profile()
        control = profile.control if profile is not None else {}
        return float(control.get("deadband_w", DEFAULT_POWER_DEADBAND_W))

    def _plan_usable(self) -> bool:
        """Whether there is a plan worth reading rows out of.

        Two callers, which is the whole reason it exists: the stale branch
        below asks so it can fall back, and the blind-sources branch above
        asks so it can keep following the load schedule while still refusing
        to command the battery.
        """
        return bool(self.coordinator.data and self.coordinator.data.plan) and (
            not self.coordinator.plan_is_stale
        )

    def _decide_loads(self, *, use_plan: bool) -> dict[str, bool]:
        """Whether each deferrable load should be running."""
        now = dt_util.utcnow()
        decisions: dict[str, bool] = {}
        for load in self.coordinator.loads.all():
            if not load.enabled:
                # Parked, not abandoned: the same answer its Should run sensor
                # gives (off, unless forced on). Skipping it instead left an
                # appliance that was running when it was disabled switched on,
                # and its commanded clock open -- an on-demand run went on
                # being credited while the sensor said "off, disabled".
                decisions[load.subentry_id] = resolve_should_run(load.mode, False)
                continue
            scheduled = self._scheduled(load) if use_plan else False
            if load.armable and not load.requested:
                # The plan outlives the request that produced it: a run that
                # ended -- finished, cancelled, or disarmed by
                # check_auto_disarm -- would otherwise keep being switched on
                # from the same stale rows until the next optimisation replaces
                # them. Not applied to a forced run, which is by definition
                # "run regardless".
                scheduled = False
            elif load.in_completion_hold(now):
                # Its target is met, so the plan has stopped asking for it, but
                # the appliance is still drawing: leaving it to the plan here
                # would cut power mid-program. check_auto_disarm ends this
                # within one timestep either way.
                scheduled = True
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

        # Before the first step, not after the last: see `_held`.
        self._held.add(AXIS_BATTERY)
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

        self._held.add(AXIS_CURTAIL)
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
        now = decision.at or dt_util.utcnow()
        for subentry_id, should_run in decision.loads.items():
            load = self.coordinator.loads.get(subentry_id)
            if load is None:
                continue
            # The commanded clock ticks here and nowhere else: this is the one
            # point at which the decision is known to have been acted on. A
            # decision computed while the control gate is off never reaches
            # this method, and must not be credited as run time -- nothing was
            # asked of the appliance. A load with no control entity still
            # counts: its own automation follows the same decision through the
            # Should run binary sensor, so the decision is the command.
            if not load.control_entity:
                load.observe_command(should_run, now)
                continue
            # A controlled one is credited with what the switch was actually
            # left in, after the call: a turn_on that failed (the plug is
            # offline) commanded nothing, and crediting it would let an
            # on-demand run "complete" without the appliance ever starting.
            running = await self._async_set_load(load, should_run, decision)
            load.observe_command(running, now)

    async def _async_set_load(
        self, load: DeferrableRuntime, should_run: bool, decision: Decision
    ) -> bool:
        """Switch one load, and say whether it is now commanded on.

        ``should_run`` when the switch is (or was just put) where the decision
        wants it; what the switch still says when the call failed; False when
        there is no switch to command at all.
        """
        entity_id = load.control_entity
        state = self.hass.states.get(entity_id)
        if state is None:
            _LOGGER.warning("Control entity %s for %s does not exist", entity_id, load.name)
            return False

        is_on = state.state == "on"
        if is_on == should_run:
            return should_run

        domain = entity_id.partition(".")[0]
        service = "turn_on" if should_run else "turn_off"
        try:
            await self.hass.services.async_call(
                domain, service, {}, blocking=True, target={"entity_id": entity_id}
            )
        except Exception as err:  # noqa: BLE001 - one bad load must not stop the rest
            decision.error = f"{load.name}: {err}"
            _LOGGER.error("Failed to switch %s: %s", load.name, err)
            return is_on

        decision.applied = True
        _LOGGER.info("Turned %s %s", load.name, "on" if should_run else "off")
        return should_run

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

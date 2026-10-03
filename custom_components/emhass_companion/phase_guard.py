"""Keep every phase of the main fuse inside its rating.

EMHASS plans one aggregate grid power; the main fuse is three fuses, one per
phase, each seeing only its own current. A battery charging evenly across the
phases while a single-phase heater runs on L1 can put L1 well past its fuse
with the aggregate comfortably inside the connection's total. Three things make
the plan alone unable to prevent that (docs/plan/phase_imbalance_safety.md):
it samples the phases once per solve, it assumes added load is symmetric, and
it sees an appliance's average power rather than its bursts.

So this module produces two numbers from the same per-phase readings:

* ``import_limit_w`` -- the aggregate import limit for the *plan*, the same
  worst-phase formula the hand-written template in docs/grid_limits.md used,
  but taken as the lowest value over a window rather than the reading at the
  instant of the solve. That is what a thermal fuse needs: worst case of the
  last half hour, not whatever the dishwasher happened to be doing at 22:00.
* ``charge_cap_w`` -- how much battery charging fits *right now*, for the
  executor to clamp a forced charge with between plans. This is the real
  protection; the planner limit only makes it rarely needed.

The battery is what gets throttled: it is symmetric, it responds in seconds,
and charging a little less never hurts anything.

Everything above ``PhaseGuard`` is pure and unit-tested without Home Assistant.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
import logging
import math
from typing import Any

from homeassistant.const import UnitOfElectricCurrent, UnitOfElectricPotential, UnitOfPower
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    MODE_FORCE_CHARGE,
    MODE_SELF_CONSUME,
    PHASE_GUARD_MAX_WRITE_INTERVAL_S,
    PHASE_GUARD_MIN_CHARGE_W,
    PHASE_GUARD_RECHECK_S,
    PHASE_GUARD_RELEASE_S,
    PHASE_OVERLOAD_HEAVY,
    PHASE_OVERLOAD_LIGHT,
    PHASE_VOLTAGE_TOLERANCE,
    UNREADABLE_STATES,
)
from .models import GridConfig

_LOGGER = logging.getLogger(__name__)

# Why the real-time clamp cannot run (clamp_blocker). Also the suffix of the
# repair issue's translation key.
CLAMP_OFF_NO_BATTERY_SENSOR = "no_battery_sensor"
CLAMP_OFF_SLOW_PROFILE = "slow_profile"

OVERLOAD_NONE = "none"
OVERLOAD_LIGHT = "light"
OVERLOAD_HEAVY = "heavy"
_OVERLOAD_RANK = {OVERLOAD_NONE: 0, OVERLOAD_LIGHT: 1, OVERLOAD_HEAVY: 2}

_AMP_UNITS = {UnitOfElectricCurrent.AMPERE: 1.0, UnitOfElectricCurrent.MILLIAMPERE: 0.001}
_WATT_UNITS = {UnitOfPower.WATT: 1.0, UnitOfPower.KILO_WATT: 1000.0}
_VOLT_UNITS = {
    UnitOfElectricPotential.VOLT: 1.0,
    UnitOfElectricPotential.MILLIVOLT: 0.001,
    UnitOfElectricPotential.KILOVOLT: 1000.0,
}


# -- pure ---------------------------------------------------------------------


def reading(state: State | None) -> float | None:
    """A sensor's state as a finite number, or None.

    ``float()`` accepts ``"nan"`` and ``"inf"``, which a template sensor can
    produce from a division by zero. Neither is a reading: NaN compares false
    against every limit, so it would sail past the overload checks and only
    fail later, in a ``round()``, while ``inf`` reads as a phase infinitely
    loaded or a battery infinitely charging.
    """
    if state is None or state.state in UNREADABLE_STATES:
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def to_amps(value: float, unit: str | None, voltage_v: float) -> float | None:
    """One phase reading as current, or None for a unit this cannot place.

    A missing or unfamiliar unit is refused rather than assumed: reading a
    current sensor as watts would make every phase look 230x emptier than it
    is, which is the one mistake a fuse guard must not make quietly.
    """
    if unit in _AMP_UNITS:
        return value * _AMP_UNITS[unit]
    if unit in _WATT_UNITS and voltage_v > 0:
        return value * _WATT_UNITS[unit] / voltage_v
    return None


def to_volts(value: float, unit: str | None, nominal_v: float) -> float | None:
    """A voltage reading in volts, or None unless it is a plausible phase voltage.

    Bounded rather than trusted: a line-to-line sensor (400 V) picked in place
    of a phase one would make every watt reading look like 40% fewer amps,
    which is the unsafe direction. Plausible means within
    ``PHASE_VOLTAGE_TOLERANCE`` of the configured nominal voltage. A missing
    unit is refused for the same reason ``to_amps`` refuses one.
    """
    if unit not in _VOLT_UNITS:
        return None
    volts = value * _VOLT_UNITS[unit]
    return volts if abs(volts - nominal_v) <= nominal_v * PHASE_VOLTAGE_TOLERANCE else None


def phase_import_limit_w(currents_a: Sequence[float], limit_a: float, voltage_v: float) -> float:
    """The aggregate import that puts the worst phase exactly at ``limit_a``.

    Load added symmetrically lands a third on each phase, so the worst phase
    reaches its limit after ``n * (limit - worst)`` more; the aggregate limit
    is today's total plus that. Phase readings are deliberately *not* clamped
    at zero -- an exporting phase really does lower what the others may import
    -- and the result is, since it only goes negative once a phase is already
    over and zero is the honest answer then. See docs/grid_limits.md.
    """
    n = len(currents_a)
    total = sum(currents_a)
    return max(total + n * limit_a - n * max(currents_a), 0.0) * voltage_v


def charge_headroom_w(
    currents_a: Sequence[float], limit_a: float, voltage_v: float, battery_w: float
) -> float:
    """How much symmetric battery charging fits right now. Negative when over.

    ``battery_w`` is the battery's measured power, positive while charging
    and negative while discharging. Its share is taken back out of each
    phase first, so the answer is the *total* charge that fits, not what
    fits on top of whatever the battery is doing now. Both signs matter: a
    charge left in would measure every cut against a reading that still
    contains it, and a discharge left out hides house load behind the
    battery's output -- the house then looks light, and switching from
    discharge to charge lands both the house and the charge on the phase.
    """
    n = len(currents_a)
    share_a = battery_w / (n * voltage_v)
    worst_base_a = max(current - share_a for current in currents_a)
    return n * (limit_a - worst_base_a) * voltage_v


def overload_level(worst_a: float, fuse_a: float) -> str:
    """How close a gG fuse is to blowing, from its rating and the worst phase.

    "light" is above the rating but within 1.25x, which a gG fuse holds for up
    to an hour; "heavy" is beyond that, where 1.5-2x blows it in minutes.
    """
    ratio = worst_a / fuse_a if fuse_a > 0 else 0.0
    if ratio > PHASE_OVERLOAD_HEAVY:
        return OVERLOAD_HEAVY
    if ratio > PHASE_OVERLOAD_LIGHT:
        return OVERLOAD_LIGHT
    return OVERLOAD_NONE


def clamp_blocker(
    *, battery_enabled: bool, battery_power_entity: str | None, min_write_interval_s: float
) -> str | None:
    """Why the real-time clamp cannot run on this install, or None if it can.

    * No battery power sensor. The battery's share has to be *measured*:
      a commanded charge the inverter is not drawing yet (ramping, a BMS
      taper, solar covering it on the DC side, a missed write) makes the
      house look lighter by that much, and a discharge cannot be known at
      all under self-consumption. Subtracting nothing instead undercuts
      every running charge and settles at about half of what fits.
    * A profile that cannot be written fast enough to carry a cut.

    The plan's import limit does not depend on either, and still applies.
    """
    if battery_enabled and not battery_power_entity:
        return CLAMP_OFF_NO_BATTERY_SENSOR
    if min_write_interval_s > PHASE_GUARD_MAX_WRITE_INTERVAL_S:
        return CLAMP_OFF_SLOW_PROFILE
    return None


def clamp_charge(planned_w: float, cap_w: float | None) -> tuple[str, float, float, list[str]]:
    """Fit a planned forced charge under the phase cap.

    Returns the action, the power to command, how many watts were cut, and
    the rules trace. A cap too small to be worth commanding hands the battery
    to self-consumption rather than forcing a token charge: on a hybrid that
    also lets the battery cover the house, which takes load *off* the phase
    that is in trouble.
    """
    if cap_w is None or planned_w <= cap_w:
        return MODE_FORCE_CHARGE, planned_w, 0.0, []
    if cap_w < PHASE_GUARD_MIN_CHARGE_W:
        return (
            MODE_SELF_CONSUME,
            0.0,
            planned_w,
            [
                f"phase guard: room for {max(cap_w, 0.0):.0f}W of charging, "
                f"plan wants {planned_w:.0f}W; handing the battery to self-consumption"
            ],
        )
    return (
        MODE_FORCE_CHARGE,
        cap_w,
        planned_w - cap_w,
        [f"phase guard: charge capped {planned_w:.0f}W→{cap_w:.0f}W"],
    )


class SlidingMin:
    """The lowest value a piecewise-constant reading took over a time window.

    A reading holds until the next one replaces it, so the minimum over the
    window includes the reading that was current when the window opened, not
    only the ones that arrived inside it -- a meter that has reported nothing
    for a minute has still been reporting its last value all along.

    Kept as a monotonic deque: anything not lower than a newer sample can
    never be the minimum again and is dropped on arrival, so memory stays
    small however often the meter reports.
    """

    def __init__(self, window: timedelta) -> None:
        self.window = window
        # [time, value, superseded_at]; values strictly increasing front to back.
        self._samples: deque[list[Any]] = deque()

    def add(self, at: datetime, value: float) -> None:
        if self._samples:
            # The newest sample is always at the back, whatever was dropped
            # before it, so this is the one reading the new one replaces.
            self._samples[-1][2] = at
        while self._samples and self._samples[-1][1] >= value:
            self._samples.pop()
        self._samples.append([at, value, None])

    def value(self, now: datetime) -> float | None:
        opens = now - self.window
        while self._samples and (ended := self._samples[0][2]) is not None and ended <= opens:
            self._samples.popleft()
        return self._samples[0][1] if self._samples else None

    def clear(self) -> None:
        self._samples.clear()


# -- Home Assistant -------------------------------------------------------------


class PhaseGuard:
    """Live per-phase readings, and the two limits derived from them."""

    def __init__(
        self,
        hass: HomeAssistant,
        grid: GridConfig,
        *,
        battery_enabled: bool = False,
        battery_power_entity: str | None = None,
        battery_power_invert: bool = False,
    ) -> None:
        self.hass = hass
        self.grid = grid
        self.battery_enabled = battery_enabled
        self.battery_power_entity = battery_power_entity
        self.battery_power_invert = battery_power_invert

        self.currents_a: tuple[float, ...] | None = None
        self.headroom_w: float | None = None
        # Measured battery power, positive charging; None when there is a
        # battery but no usable reading of it.
        self.battery_w: float | None = None
        self.unreadable: list[str] = []
        self.overload = OVERLOAD_NONE
        # The voltage the latest reading was converted with, and whether it
        # was measured or the configured fallback.
        self.voltage_v = grid.phase_voltage_v
        self.voltage_measured = False
        self._warned_voltage = False
        self._charge_window = SlidingMin(timedelta(seconds=PHASE_GUARD_RELEASE_S))
        self._limit_window = SlidingMin(timedelta(minutes=max(grid.phase_limit_window_min, 0)))
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._warned_unreadable = False
        # The worst overload logged in the current episode, and when the phases
        # last went clear -- so a phase hovering at its rating is one warning,
        # not one per meter reading.
        self._episode_overload = OVERLOAD_NONE
        self._clear_since: datetime | None = None

    # -- lifecycle ------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        watched = [*self.grid.phase_entities]
        if self.battery_power_entity:
            watched.append(self.battery_power_entity)
        self._unsubs.append(
            async_track_state_change_event(self.hass, watched, self._async_state_changed)
        )
        # Not only for a quiet meter: the release window has to expire on time
        # even when no reading changes, or a cut would outlast its cause.
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_recheck, timedelta(seconds=PHASE_GUARD_RECHECK_S)
            )
        )
        self.async_update()

    @callback
    def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def _async_state_changed(self, _event: Event) -> None:
        self.async_update()

    @callback
    def _async_recheck(self, _now: datetime) -> None:
        self.async_update()

    # -- readings -------------------------------------------------------------

    @callback
    def async_update(self, now: datetime | None = None) -> None:
        """Take a fresh reading of every phase and tell whoever is listening."""
        now = now or dt_util.utcnow()
        self._read_voltage()
        currents, unreadable = self._read_phases()
        self.unreadable = unreadable
        if currents is None:
            # Fails open, like the import limit sensor it replaces: a meter
            # integration restarting must not stop the battery charging. The
            # windows are cleared rather than held, since a value held across
            # an outage describes nothing.
            if not self._warned_unreadable:
                _LOGGER.warning(
                    "Phase readings unavailable (%s); the phase guard is not limiting "
                    "charging until they return",
                    ", ".join(unreadable),
                )
                self._warned_unreadable = True
            self.currents_a = None
            self.headroom_w = None
            self.battery_w = None
            self.overload = OVERLOAD_NONE
            self._charge_window.clear()
            self._limit_window.clear()
        else:
            if self._warned_unreadable:
                _LOGGER.info("Phase readings are back; the phase guard is active again")
                self._warned_unreadable = False
            limit_a = self.grid.phase_limit_a
            voltage = self.voltage_v
            self.currents_a = currents
            self.battery_w = self._read_battery_w() if self.battery_enabled else 0.0
            # Without a battery reading the headroom is only what fits on top
            # of whatever the battery is doing -- published, but never used to
            # clamp (charge_ready). The window is dropped rather than fed it.
            self.headroom_w = charge_headroom_w(currents, limit_a, voltage, self.battery_w or 0.0)
            if self.battery_w is None:
                self._charge_window.clear()
            else:
                self._charge_window.add(now, self.headroom_w)
            self._limit_window.add(now, phase_import_limit_w(currents, limit_a, voltage))
            self._track_overload(currents, now)

        for listener in list(self._listeners):
            listener()

    def _read_phases(self) -> tuple[tuple[float, ...] | None, list[str]]:
        currents: list[float] = []
        unreadable: list[str] = []
        for entity_id in self.grid.phase_entities:
            amps = None
            state = self.hass.states.get(entity_id)
            if state is not None and (value := reading(state)) is not None:
                amps = to_amps(value, state.attributes.get("unit_of_measurement"), self.voltage_v)
            if amps is None:
                unreadable.append(entity_id)
            else:
                currents.append(amps)
        return (None if unreadable else tuple(currents)), unreadable

    def _read_voltage(self) -> None:
        """The live voltage if there is a sane one, else the configured number.

        Read on every update rather than subscribed to: the phase readings
        and the re-check timer already drive updates, and a voltage that
        changes by a volt every second is not a reason for one of its own.
        Falling back is logged once per outage, like the phase readings.
        """
        entity_id = self.grid.phase_voltage_entity
        volts = None
        if (
            entity_id
            and (state := self.hass.states.get(entity_id)) is not None
            and (value := reading(state)) is not None
        ):
            volts = to_volts(
                value, state.attributes.get("unit_of_measurement"), self.grid.phase_voltage_v
            )
        if entity_id and volts is None and not self._warned_voltage:
            _LOGGER.warning(
                "Voltage sensor %s is unavailable or out of range; converting phase "
                "readings at the configured %.0f V until it is back",
                entity_id,
                self.grid.phase_voltage_v,
            )
            self._warned_voltage = True
        elif volts is not None:
            self._warned_voltage = False
        self.voltage_measured = volts is not None
        self.voltage_v = volts if volts is not None else self.grid.phase_voltage_v

    def _read_battery_w(self) -> float | None:
        """The battery's measured power, positive charging, or None.

        Measured only -- never the commanded setpoint, which the battery may
        not be drawing (see clamp_blocker). Errs safe in both directions on a
        DC-side sensor: a DC charge reads below its AC draw, and a DC
        discharge above its AC output, so either way the house looks
        slightly heavier than it is.
        """
        if not self.battery_power_entity:
            return None
        state = self.hass.states.get(self.battery_power_entity)
        if (value := reading(state)) is None or state is None:
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit not in _WATT_UNITS:
            return None
        watts = value * _WATT_UNITS[unit]
        # EMHASS's convention is positive for discharge; the invert option is
        # for a sensor that is positive while charging.
        return watts if self.battery_power_invert else -watts

    def _track_overload(self, currents: tuple[float, ...], now: datetime) -> None:
        fuse_a = self.grid.main_fuse_a or 0.0
        worst = max(currents)
        level = overload_level(worst, fuse_a)
        if level == OVERLOAD_NONE:
            self._clear_since = self._clear_since or now
            if now - self._clear_since >= timedelta(seconds=PHASE_GUARD_RELEASE_S):
                self._episode_overload = OVERLOAD_NONE
        else:
            self._clear_since = None
        if _OVERLOAD_RANK[level] > _OVERLOAD_RANK[self._episode_overload]:
            # Logged on the way in, and again only if it gets worse. Whether
            # the guard can do anything about it is the executor's business (it
            # may already have cut charging to nothing); this is the record
            # that it happened at all.
            self._episode_overload = level
            _LOGGER.warning(
                "L%d at %.1f A on a %.0f A main fuse (%s overload)",
                currents.index(worst) + 1,
                worst,
                fuse_a,
                level,
            )
        self.overload = level

    # -- outputs --------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.currents_a is not None

    @property
    def charge_ready(self) -> bool:
        """Whether there is a measured basis for capping a charge right now."""
        return self.currents_a is not None and self.battery_w is not None

    @property
    def charge_cap_w(self) -> float | None:
        """Battery charging allowed now: the lowest headroom of the release window.

        Drops the moment a phase climbs, and only comes back up once the
        phases have stayed clear for the whole window.
        """
        if not self.charge_ready:
            return None
        value = self._charge_window.value(dt_util.utcnow())
        return None if value is None else max(value, 0.0)

    @property
    def import_limit_w(self) -> float | None:
        """The plan's aggregate import limit: the lowest of the smoothing window."""
        if self.grid.phase_limit_window_min <= 0 and self.currents_a is not None:
            return phase_import_limit_w(self.currents_a, self.grid.phase_limit_a, self.voltage_v)
        value = self._limit_window.value(dt_util.utcnow())
        return None if value is None else round(value)

    def as_attributes(self) -> dict[str, Any]:
        currents = self.currents_a
        cap = self.charge_cap_w
        limit = self.import_limit_w
        return {
            "phase_currents_a": [round(current, 1) for current in currents] if currents else None,
            "worst_phase": f"L{currents.index(max(currents)) + 1}" if currents else None,
            "fuse_a": self.grid.main_fuse_a,
            "limit_a": self.grid.phase_limit_a,
            "voltage_v": round(self.voltage_v, 1),
            "voltage_measured": self.voltage_measured,
            "battery_w": None if self.battery_w is None else round(self.battery_w),
            "charge_cap_w": None if cap is None else round(cap),
            "import_limit_w": None if limit is None else round(limit),
            "overload": self.overload,
            "unreadable": self.unreadable,
        }


__all__ = [
    "CLAMP_OFF_NO_BATTERY_SENSOR",
    "CLAMP_OFF_SLOW_PROFILE",
    "OVERLOAD_HEAVY",
    "OVERLOAD_LIGHT",
    "OVERLOAD_NONE",
    "PhaseGuard",
    "SlidingMin",
    "charge_headroom_w",
    "clamp_blocker",
    "clamp_charge",
    "overload_level",
    "phase_import_limit_w",
    "to_amps",
    "to_volts",
]

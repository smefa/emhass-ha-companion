"""Notices when the entities this integration reads from stop reporting.

Every source the companion consumes -- battery SOC, live PV, the house load,
the grid limits, each load's own power and control entities, the inverter's
own controls -- belongs to somebody else's integration. When one of them goes
away this integration has always degraded quietly: ``_read_soc`` falls back to
the configured initial SOC, ``_read_pv_live`` skips the blend, ``_read_grid_limit``
reverts to the static number, and the only trace is a log line nobody is
watching. The plan goes on being produced and the executor goes on commanding
the inverter from figures that stopped being measurements hours ago.

Each of those fallbacks is right on its own: one dead sensor must not be able
to take the whole optimisation down. What was missing is the other half --
somewhere that notices they are *all* gone, and says so. That is this module:
it watches the entity ids the config actually points at and reports the ones
that have been unreadable long enough to be a fault rather than a blip, so the
coordinator can raise a repair and the executor can hand the inverter back.

Two exemptions keep it from crying wolf:

* Nothing is counted until Home Assistant has finished starting. Half the
  house reads ``unavailable`` while integrations are still loading, so a
  watch that counted from setup would fire on every single restart -- and an
  alert that appears every restart is one nobody reads by the third time.
* A reading must then stay gone for :data:`SOURCE_BLIND_GRACE` before it
  counts. A Modbus inverter that drops its TCP connection and picks it up a
  minute later is the normal behaviour of the hardware, not something worth
  standing control down for.

Only entities Home Assistant has actually heard of are watched. An id in the
options that matches nothing -- a typo, a renamed entity, a profile field
holding something that merely looks like an entity id -- is a configuration
error rather than a source that went away, and the diagnostics bundle already
reports those with far more context than a repair notice could.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta
import logging
import re
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from .configuration import EmhassConfig
from .const import SOURCE_BLIND_GRACE, UNREADABLE_STATES

_LOGGER = logging.getLogger(__name__)

# How often the watch re-checks. State change events alone cannot do this job:
# they fire when a reading goes and when it comes back, never while it stays
# gone, which is the only moment the grace period can actually expire.
_SCAN_INTERVAL: Final = timedelta(minutes=1)

# Deliberately a simple local shape rather than
# homeassistant.helpers.config_validation's entity-id validator: this walks
# arbitrary profile option blobs whose schema this module does not know, and a
# local pattern is robust to that in a way importing a stricter validator would
# not be. Over-matching is harmless here -- anything Home Assistant has never
# heard of is dropped by _async_evaluate before it can be called blind.
ENTITY_ID_RE: Final = re.compile(r"[a-z_]+\.[a-z0-9_]+")


def walk_entity_candidates(value: Any, path: str) -> Iterator[tuple[str, str]]:
    """Every string under ``value`` shaped like an entity id, and where it sits."""
    if isinstance(value, str):
        if ENTITY_ID_RE.fullmatch(value):
            yield path, value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from walk_entity_candidates(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from walk_entity_candidates(item, f"{path}[{index}]")


def collect_entity_references(entry: ConfigEntry) -> dict[str, list[str]]:
    """Every entity id referenced anywhere in the config, and where from.

    Walks ``entry.data``, ``entry.options`` and every subentry's ``data`` --
    not just the fields any one module knows the shape of -- because a
    profile's own options (a Solcast entity list, a template's referenced
    sensors) are exactly where the two most common remote failures ("wrong
    entity id" and "kW where the code expects W") actually show up.
    """
    references: dict[str, list[str]] = {}
    roots: list[tuple[str, Any]] = [("data", entry.data), ("options", entry.options)]
    for subentry in entry.subentries.values():
        roots.append((f"subentries[{subentry.subentry_type}:{subentry.title}]", subentry.data))
    for root_name, root_value in roots:
        for path, entity_id in walk_entity_candidates(root_value, root_name):
            references.setdefault(entity_id, []).append(path)
    return references


def critical_entities(config: EmhassConfig) -> set[str]:
    """The readings the inverter must not go on being commanded without.

    Deliberately not everything the watch covers. A deferrable load's power
    sensor going away costs that load its accounting, and a dead price sensor
    costs the next plan its prices -- both worth reporting, neither a reason to
    take the battery away from the optimiser. These are the ones whose absence
    makes the *writes themselves* wrong:

    * the SOC entity, because it is what ``Executor`` hands to every inverter
      action template and what the optimiser's whole battery trajectory is
      anchored on. With it gone the plan is solved from
      ``BatteryConfig.soc_init`` -- a configured constant that has no
      relationship to how full the battery actually is -- and a forced charge
      computed from it will happily push a full battery or flatten an empty
      one.
    * the inverter profile's own entities, because those are the selects and
      numbers being written to. Nothing useful can be commanded through a
      control surface that is not reporting.

    ``pv_live_entity`` and ``battery_power_entity`` are pointedly not here.
    The first only declines to blend a live sample into the first forecast
    step, leaving a forecast that is merely less sharp; the second is read by
    nothing but the dashboard cards.
    """
    entities = {config.soc_entity} if config.battery.enabled else set()
    entities.update(
        entity_id for _path, entity_id in walk_entity_candidates(config.inverter.options, "options")
    )
    return {entity_id for entity_id in entities if entity_id}


class SourceHealth:
    """Watches the config's source entities and reports the ones that went blind.

    Owned by the coordinator, which has the ``hass`` and ``entry`` this needs
    and is already where every other "notice something and raise a repair"
    lives. Started and stopped alongside the coordinator's other trackers, so
    an entry that is only constructed (as every unit test does) never watches
    anything and never reports a fault.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        grace: timedelta = SOURCE_BLIND_GRACE,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.grace = grace
        # When each currently-unreadable entity was first seen that way. An
        # entity leaving this dict is what "it came back" means, which is why
        # the grace period restarts from scratch on a reading that flaps.
        self._blind_since: dict[str, datetime] = {}
        self._blind: tuple[str, ...] = ()
        # None until Home Assistant has finished starting; see the module
        # docstring. Nothing is counted as gone before this.
        self._watching_from: datetime | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[Callable[[], None]] = []
        self._unsub_started: Callable[[], None] | None = None

    # -- lifecycle ------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Begin watching, once Home Assistant is up.

        During a cold start this subscribes now but starts the clock later:
        the entities are already being watched, they simply cannot be held
        against anyone until the rest of the house has had its chance to load.
        """
        self.async_stop()
        entity_ids = sorted(collect_entity_references(self.entry))
        if not entity_ids:
            return
        self._unsubs.append(
            async_track_state_change_event(self.hass, entity_ids, self._async_state_changed)
        )
        self._unsubs.append(async_track_time_interval(self.hass, self._async_scan, _SCAN_INTERVAL))
        # Deliberately CoreState.running rather than hass.is_running, which is
        # also true during CoreState.starting -- and starting is precisely when
        # a cold boot sets this entry up, so the exemption would never once
        # apply on the restart it exists for.
        if self.hass.state is CoreState.running:
            self._watching_from = dt_util.utcnow()
        else:
            # Held apart from _unsubs and dropped the moment it fires: calling
            # a one-time listener's unsub after the event has already arrived
            # makes core log "Unable to remove unknown job listener" with a
            # traceback, and an unload that looks like an error in the log is
            # how a real one goes unnoticed.
            self._unsub_started = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._async_started
            )

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        if self._unsub_started is not None:
            self._unsub_started()
            self._unsub_started = None
        self._watching_from = None
        self._blind_since = {}
        self._blind = ()

    @callback
    def _async_started(self, _event: Event) -> None:
        self._unsub_started = None
        self._watching_from = dt_util.utcnow()

    # -- change notification --------------------------------------------------

    @callback
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a callback for "the set of blind sources changed"."""
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    # -- what it found --------------------------------------------------------

    @property
    def blind(self) -> tuple[str, ...]:
        """Watched entities unreadable for longer than the grace period."""
        return self._blind

    @property
    def blind_critical(self) -> tuple[str, ...]:
        """The blind entities that make commanding the inverter unsafe."""
        critical = critical_entities(EmhassConfig.from_entry(self.hass, self.entry))
        return tuple(entity_id for entity_id in self._blind if entity_id in critical)

    @property
    def report(self) -> dict[str, Any]:
        """Attributes for the binary sensor, and the same figures for a repair."""
        return {
            "blind_entities": list(self._blind),
            "blind_critical_entities": list(self.blind_critical),
            "blind_since": (
                dt_util.as_local(
                    min(self._blind_since[entity] for entity in self._blind)
                ).isoformat()
                if self._blind
                else None
            ),
            "grace_period": str(self.grace),
        }

    # -- the watch itself -----------------------------------------------------

    @callback
    def _async_state_changed(self, _event: Event[EventStateChangedData]) -> None:
        self._async_evaluate()

    @callback
    def _async_scan(self, _now: datetime) -> None:
        self._async_evaluate()

    @callback
    def _async_evaluate(self) -> None:
        """Re-derive the blind set, and tell the listeners if it moved.

        The watched set is re-collected on every pass rather than cached at
        start: an entity id that matched nothing at setup can be registered
        later (a Modbus integration that had not loaded yet, a template sensor
        the user has just written), and one that has been deleted outright
        should stop being counted rather than stay blind forever.
        """
        if self._watching_from is None:
            return

        now = dt_util.utcnow()
        registry = er.async_get(self.hass)
        blind: list[str] = []
        seen: set[str] = set()

        for entity_id in sorted(collect_entity_references(self.entry)):
            state = self.hass.states.get(entity_id)
            if state is None and registry.async_get(entity_id) is None:
                # Never heard of it. A config error, not a source that failed.
                self._blind_since.pop(entity_id, None)
                continue
            seen.add(entity_id)
            if state is not None and state.state not in UNREADABLE_STATES:
                self._blind_since.pop(entity_id, None)
                continue
            # A reading already missing when Home Assistant came up has its
            # clock started here, on the first pass past the guard above,
            # rather than at whatever time it actually stopped reporting --
            # which this cannot know and which may predate the restart. That
            # is what gives a slow-loading integration a full grace period
            # instead of being condemned the moment startup finishes.
            since = self._blind_since.setdefault(entity_id, now)
            if now - since >= self.grace:
                blind.append(entity_id)

        for entity_id in set(self._blind_since) - seen:
            self._blind_since.pop(entity_id, None)

        if (found := tuple(blind)) == self._blind:
            return

        if found:
            _LOGGER.warning(
                "Source readings unavailable for more than %s: %s", self.grace, ", ".join(found)
            )
        else:
            _LOGGER.info("Source readings are being reported again")

        self._blind = found
        for listener in list(self._listeners):
            listener()

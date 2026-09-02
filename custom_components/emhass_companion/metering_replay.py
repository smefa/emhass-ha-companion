"""Replaying a restart gap's recorder history once real prices exist.

The rest of this feature lives in :mod:`metering`: a restart gap is noticed
and written off in :meth:`SavingsTracker.async_load`, and a
:class:`~metering.PendingGapReplay` is captured describing exactly what to
try to recover. This module is the recovery itself -- fetch the gap's own
recorder history, walk it through the same :func:`metering.read_meters` /
:meth:`savings.Ledger.record` machinery the live tracker uses, and hand back
whether it actually worked.

Copyright (C) 2026 Tomas Smedberg.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. It is distributed without any warranty; see the LICENSE file at
the repository root for the full terms.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging

from homeassistant.components.recorder import get_instance, history
from homeassistant.core import HomeAssistant, State

from .metering import GapSeed, Meter, MeterSpec, PendingGapReplay, read_meters
from .savings import Ledger, Prices

_LOGGER = logging.getLogger(__name__)


def _scratch_meters(seeds: dict[str, GapSeed]) -> MeterSpec:
    """One :class:`Meter` per seed, pre-loaded at its own gap-start reading.

    Never the live tracker's own ``Meter`` objects -- those were already
    disowned by :meth:`Meter.restore` before this ever runs. A fresh scratch
    copy per seed is what lets the walk below resume each meter from exactly
    where the gap began.
    """
    spec = MeterSpec()
    for name, seed in seeds.items():
        setattr(
            spec,
            name,
            Meter.seeded(
                seed.entity_id,
                kind=seed.kind,
                invert=seed.invert,
                last_value=seed.last_value,
                last_time=seed.last_time,
            ),
        )
    return spec


def _walk(
    fetched: dict[str, list[State]],
    scratch: MeterSpec,
    prices: Callable[[datetime], Prices | None],
    ledger: Ledger,
) -> None:
    """Fold every fetched instant into ``ledger``, oldest first.

    Mirrors ``_async_source_changed``'s "any entity's change ticks every
    meter" rule: the timeline is the union of every entity's own change
    times, and at each one every meter is read against whichever state each
    of *its own* entities last reported at or before that instant -- a plain
    hold-last, the same thing an unavailable live sensor already does in
    :meth:`Meter._take_energy_from`.
    """
    timeline = sorted({state.last_changed for states in fetched.values() for state in states})
    cursors = dict.fromkeys(fetched, 0)
    held: dict[str, State] = {}
    for when in timeline:
        for entity_id, states in fetched.items():
            index = cursors[entity_id]
            while index < len(states) and states[index].last_changed <= when:
                held[entity_id] = states[index]
                index += 1
            cursors[entity_id] = index
        ledger.record(read_meters(scratch, when, held.get), prices(when))


async def async_replay_gap(
    hass: HomeAssistant,
    meters: MeterSpec,
    prices: Callable[[datetime], Prices | None],
    ledger: Ledger,
    pending: PendingGapReplay,
) -> bool:
    """Reconstruct one restart gap from recorder history and fold it into ``ledger``.

    Returns whether the replay actually ran. ``ledger`` is left completely
    untouched on a ``False`` -- a history probe must never leave the books
    half-updated, the same discipline
    :meth:`coordinator.EmhassCoordinator._recent_load_series` already
    applies to a load forecast bootstrap.

    No partial replay: a shared :class:`~savings.IntervalEnergy` cannot mix a
    meter reconstructed from history with one silently assumed idle for the
    same instant, so recorder history must hold *some* observation
    somewhere in the gap for *every* seeded entity or nothing is touched at
    all.
    """
    try:
        instance = get_instance(hass)
    except (KeyError, RuntimeError):
        return False

    def fetch() -> dict[str, list[State]] | None:
        """Fetch and reduce inside the one executor job.

        One call per entity rather than a single batched query: it reuses
        ``history.state_changes_during_period`` exactly as
        ``coordinator._recent_load_series`` already does, rather than taking
        on ``get_significant_states``'s own, different "significant change"
        semantics for a handful of entities that is not worth the extra
        surface.
        """
        fetched: dict[str, list[State]] = {}
        for seed in pending.seeds.values():
            found = history.state_changes_during_period(
                hass,
                pending.window_start,
                pending.window_end,
                seed.entity_id,
                False,  # no_attributes -- unit conversion needs them
                False,  # descending
                None,  # limit
                True,  # include_start_time_state
            )
            states = found.get(seed.entity_id)
            if not states:
                # No observation anywhere in the gap for this entity --
                # purged, disabled, or only added after the gap began. The
                # scratch meter is seeded from ``GapSeed`` regardless of
                # whether history reaches all the way back to
                # ``window_start`` (a hold-last read of *any* in-window
                # sample still folds in everything since the seed
                # correctly); it is a *complete absence* of samples that
                # would leave this meter reading as an unintended zero for
                # the whole gap. See the no-partial-replay note above.
                return None
            fetched[seed.entity_id] = states
        return fetched

    try:
        fetched = await instance.async_add_executor_job(fetch)
    except Exception:  # a history probe must never break a run
        _LOGGER.debug("Could not fetch recorder history to replay a restart gap", exc_info=True)
        return False
    if fetched is None:
        _LOGGER.debug(
            "Recorder history does not cover %s -> %s for every meter; leaving the gap unpriced",
            pending.window_start,
            pending.window_end,
        )
        return False

    _LOGGER.debug(
        "Replaying restart gap %s -> %s across %s",
        pending.window_start,
        pending.window_end,
        ", ".join(meters.entities()),
    )
    _walk(fetched, _scratch_meters(pending.seeds), prices, ledger)
    ledger.unpriced_kwh -= pending.missed_kwh
    return True

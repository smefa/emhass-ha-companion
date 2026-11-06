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
from dataclasses import replace
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


async def _async_fetch_gap(
    hass: HomeAssistant, pending: PendingGapReplay
) -> dict[str, list[State]] | None:
    """Fetch complete source coverage, leaving callers untouched on failure."""
    try:
        instance = get_instance(hass)
    except (KeyError, RuntimeError):
        return None

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
        return None
    return fetched


async def async_prepare_gap(
    hass: HomeAssistant, meters: MeterSpec, pending: PendingGapReplay
) -> PendingGapReplay:
    """Resolve counter baselines before live tracking, independently of prices.

    Cache the exact observations for the later priced replay. Otherwise a
    recovery followed by another correction during downtime can leave live
    tracking below the replay's final high-water mark and count recovery twice.
    Failed preparation cannot be retried after live accounting has started:
    without a matching baseline, later replay would overlap live recovery.
    """
    closing = {seed.entity_id: hass.states.get(seed.entity_id) for seed in pending.seeds.values()}
    fetched = await _async_fetch_gap(hass, pending)
    if fetched is not None:
        for entity_id, state in closing.items():
            if state is not None:
                fetched[entity_id] = [
                    sample
                    for sample in fetched[entity_id]
                    if sample.last_changed < pending.window_end
                ] + [
                    State(
                        entity_id,
                        state.state,
                        dict(state.attributes),
                        last_changed=pending.window_end,
                    )
                ]
        scratch = _scratch_meters(pending.seeds)
        _walk(fetched, scratch, lambda _when: None, Ledger())
        for name in pending.seeds:
            meter = getattr(meters, name)
            meter.resume_counter(
                getattr(scratch, name), closing[meter.entity_id], now=pending.window_end
            )
    return replace(pending, prepared=True, history=fetched)


async def async_replay_gap(
    hass: HomeAssistant,
    meters: MeterSpec,
    prices: Callable[[datetime], Prices | None],
    ledger: Ledger,
    pending: PendingGapReplay,
) -> bool:
    """Reconstruct a gap using the same observations as its restored baselines.

    The ledger stays untouched on failure or absent source coverage. Direct
    callers without a prepared gap fetch history themselves; SavingsTracker
    prepares before subscribing so a delayed replay cannot overlap live energy.
    """
    fetched = pending.history if pending.prepared else await _async_fetch_gap(hass, pending)
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

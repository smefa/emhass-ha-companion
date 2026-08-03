"""Tests for the Requested switch's restore-across-restart contract.

``requested_at`` surviving a restart is well covered elsewhere; what is not is
``request_runtime`` -- the progress made towards ``operating_hours`` since that
anchor. Losing it on restart makes an already-progressed (or even
already-satisfied) on-demand request look freshly armed again, with its
original, now stale, deadline -- the "deferrable not updating run time"
symptom.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.emhass_companion.deferrable import DeferrableRuntime
from custom_components.emhass_companion.switch import _requested_attrs, _restore_requested

T0 = datetime(2026, 8, 1, 10, 56, 22, tzinfo=UTC)


def _load(**overrides) -> DeferrableRuntime:
    defaults = {
        "subentry_id": "dishwasher",
        "name": "Dishwasher",
        "nominal_power_w": 1101.0,
        "operating_hours": 1.0,
    }
    return DeferrableRuntime(**{**defaults, **overrides})


def test_requested_attrs_expose_the_runtime_made_so_far():
    load = _load()
    load.request(T0)
    load.request_runtime = timedelta(minutes=40)
    attrs = _requested_attrs(load)
    assert attrs["request_runtime_seconds"] == 2400.0


def test_restore_recovers_progress_made_before_a_restart():
    """The scenario this was added for.

    A load requested at T0 runs its full 1 h target, then Home Assistant
    restarts before the next refresh gets a chance to auto-disarm it. Restore
    must bring back not just *when* it was requested but *how much of it was
    already done*, or the load looks freshly armed against its original,
    now long-past, deadline forever.
    """
    before_restart = _load()
    before_restart.request(T0)
    before_restart.request_runtime = timedelta(hours=1)  # target fully met
    attrs = _requested_attrs(before_restart)

    # A fresh instance, as if re-created after restart: set_fn has already run
    # (arming a brand new, zero-progress request anchored at "now"), and
    # restore_fn is applied on top, exactly as LoadSwitch.async_added_to_hass
    # does it.
    after_restart = _load()
    after_restart.request(datetime(2026, 8, 1, 18, 0, tzinfo=UTC))
    _restore_requested(after_restart, attrs)

    assert after_restart.requested_at == T0
    assert after_restart.request_runtime == timedelta(hours=1)


def test_restore_leaves_a_fresh_request_alone_when_nothing_was_done_yet():
    load = _load()
    load.request(T0)
    attrs = _requested_attrs(load)
    assert attrs["request_runtime_seconds"] == 0.0

    restored = _load()
    restored.request(T0)
    _restore_requested(restored, attrs)
    assert restored.request_runtime == timedelta()


def test_restore_ignores_a_missing_runtime_attribute():
    """A request restored from before this attribute existed must not crash,
    and must not fabricate progress that was never recorded."""
    load = _load()
    load.request(T0)
    _restore_requested(load, {"requested_at": T0.isoformat()})
    assert load.request_runtime == timedelta()

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
from unittest.mock import patch

from homeassistant.util import dt as dt_util

from custom_components.emhass_companion.const import COMPLETION_NEVER_STARTED
from custom_components.emhass_companion.deferrable import DeferrableRuntime
from custom_components.emhass_companion.switch import _requested_attrs, _restore_requested

T0 = datetime(2026, 8, 1, 10, 56, 22, tzinfo=UTC)


def _requested_attrs_at(load: DeferrableRuntime, now: datetime) -> dict:
    """_requested_attrs reads the clock; freeze it so the span is exact."""
    with patch.object(dt_util, "utcnow", return_value=now):
        return _requested_attrs(load)


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


def test_the_commanded_clock_survives_a_restart():
    """The clock a run is judged on, so losing it restarts the judgement."""
    before_restart = _load()
    before_restart.request(T0)
    before_restart.observe_command(True, T0)
    attrs = _requested_attrs_at(before_restart, T0 + timedelta(minutes=50))

    after_restart = _load()
    after_restart.request(T0 + timedelta(hours=8))
    _restore_requested(after_restart, attrs)

    assert after_restart.command_runtime == timedelta(minutes=50)


def test_an_open_command_span_is_not_rolled_back_by_a_restart():
    """request_runtime only banks closed spans; the commanded clock has to
    include the one still running, or every restart forgets the current block."""
    load = _load()
    load.request(T0)
    load.observe_command(True, T0)
    attrs = _requested_attrs_at(load, T0 + timedelta(minutes=20))
    assert attrs["command_runtime_seconds"] == 1200.0


def test_a_part_served_idle_window_survives_a_restart():
    """An appliance that finished a minute before a restart has already served
    most of its window; making it serve a fresh one is how a run that is
    plainly over stays armed for another quarter hour after every restart."""
    before_restart = _load(power_sensor="sensor.dishwasher_power")
    before_restart.request(T0)
    before_restart.observe_command(True, T0)
    before_restart.observe_power(1900, T0)
    before_restart.observe_power(0, T0 + timedelta(minutes=40))
    attrs = _requested_attrs(before_restart)

    after_restart = _load(power_sensor="sensor.dishwasher_power")
    after_restart.request(T0)
    _restore_requested(after_restart, attrs)

    assert after_restart.idle_since == T0 + timedelta(minutes=40)
    assert after_restart.seen_running is True


def test_readings_before_the_first_command_do_not_discard_the_restored_window():
    """Between a restart and the first executor apply nothing is known about
    what is being commanded -- which must not read as "not commanded"."""
    load = _load(power_sensor="sensor.dishwasher_power")
    load.request(T0)
    _restore_requested(
        load,
        {
            "requested_at": T0.isoformat(),
            "seen_running": True,
            "idle_since": (T0 + timedelta(minutes=40)).isoformat(),
        },
    )
    load.observe_power(0, T0 + timedelta(minutes=41))

    assert load.idle_since == T0 + timedelta(minutes=40)


def test_how_the_last_run_ended_outlives_the_run():
    """cancel() clears the request; "why did it stop" is asked afterwards."""
    load = _load()
    load.request(T0)
    load.cancel(COMPLETION_NEVER_STARTED, T0 + timedelta(hours=1))
    attrs = _requested_attrs(load)
    assert attrs["last_completion_reason"] == COMPLETION_NEVER_STARTED

    restored = _load()
    _restore_requested(restored, attrs)
    assert restored.last_completion_reason == COMPLETION_NEVER_STARTED
    assert restored.last_completion_at == T0 + timedelta(hours=1)

"""``SavingsTracker.async_load`` capturing a ``PendingGapReplay``, and
``metering_replay.async_replay_gap`` actually recovering one.

Step 3 of the restart-gap replay plan: before any replay logic exists, the
tracker must correctly *notice* a replayable gap at load time -- one
``PendingGapReplay`` only when every currently configured meter's stored
reading is stale, seeded from each meter's own last-known reading, and
clamped to today when the gap crossed midnight. Step 5 (firing the replay
once prices exist) is not built yet; ``_pending_gap`` is still inert as far
as ``SavingsTracker`` itself is concerned.

Storage is exercised for real (not hand-built dicts): each step-3 scenario
runs a first ``SavingsTracker`` through ``async_load``/``async_start``/
``async_shutdown`` to write genuine ``Store`` data via a live meter tick, then
loads a second tracker against it -- the same before/after-restart pattern
``tests/integration/test_peak_tracker.py`` uses for ``PeakTracker``.

``prices`` always answers with a real price here, deliberately -- otherwise
every live tick would book its own energy into ``unpriced_kwh`` too (that is
``Ledger.record``'s own contract, exercised elsewhere in ``tests/
test_savings.py``), which would swamp the one source of ``unpriced_kwh`` these
tests actually care about: the restart-gap write-off in ``Meter.restore``.

Step 4's ``async_replay_gap`` scenarios below call it directly against a
hand-built ``Ledger``/``PendingGapReplay`` rather than driving a whole
tracker through a restart -- the fetch/merge/walk logic under test does not
care how the ``PendingGapReplay`` it is handed came to exist. Recorder
history is stubbed for most of them (``_history_stub``); one true end-to-end
case at the bottom drives a real in-memory recorder (``recorder_mock``) to
prove the executor-job/history-API wiring itself.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, State
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.emhass_companion.const import DOMAIN
from custom_components.emhass_companion.metering import (
    _RESTORE_MAX_GAP,
    GapSeed,
    Meter,
    MeterSpec,
    PendingGapReplay,
    SavingsTracker,
    read_meters,
)
from custom_components.emhass_companion.metering_replay import (
    _walk,
    async_prepare_gap,
    async_replay_gap,
)
from custom_components.emhass_companion.savings import Ledger, Prices

_PRICES = Prices(buy=1.0, sell=0.5)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations() -> None:
    """Shadow the package-wide autouse fixture of the same name for this module.

    That fixture depends on ``hass``, which would build it before
    ``recorder_mock`` gets a chance to -- ``recorder_db_url`` asserts ``hass``
    was not already set up, precisely to catch that ordering. No test in this
    module goes through ``hass.config_entries.async_setup`` (they construct
    ``SavingsTracker`` / call ``async_replay_gap`` directly), so loading a
    custom integration for real is never needed here.
    """
    return


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"})
    entry.add_to_hass(hass)
    return entry


class _FakePriceListenerBus:
    """Stand-in for ``DataUpdateCoordinator.async_add_listener``.

    Captures the callback so a test can fire it by hand instead of driving a
    real coordinator refresh, and counts how many times it was unsubscribed.
    """

    def __init__(self) -> None:
        self.listener: CALLBACK_TYPE | None = None
        self.unsub_calls = 0

    def add(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self.listener = listener

        def _unsub() -> None:
            self.unsub_calls += 1

        return _unsub


def _tracker(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    meters: MeterSpec,
    *,
    prices: Callable[[datetime], Prices | None] = lambda _now: _PRICES,
    add_price_listener: Callable[[CALLBACK_TYPE], CALLBACK_TYPE] | None = None,
    battery_enabled: bool = True,
) -> SavingsTracker:
    return SavingsTracker(
        hass,
        entry,
        meters,
        soc_entity=None,
        capacity_kwh=None,
        prices=prices,
        plan_forecast=lambda _now, _window: None,
        add_price_listener=add_price_listener or (lambda _listener: lambda: None),
        battery_enabled=battery_enabled,
    )


def _set_energy(hass: HomeAssistant, entity_id: str, value: float) -> None:
    hass.states.async_set(
        entity_id,
        str(value),
        {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
        force_update=True,
    )


def _energy_state(entity_id: str, value: float, when: datetime) -> State:
    return State(
        entity_id,
        str(value),
        {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
        last_changed=when,
    )


def _history_stub(per_entity: dict[str, list[State]]):
    """A ``history.state_changes_during_period`` stand-in fed canned states.

    ``async_replay_gap`` calls the real function once per seeded entity, so
    this mirrors its signature and answers only for entities the test wired
    up -- an entity with no entry behaves exactly as recorder history that
    does not reach back far enough does: an empty result.
    """

    def _fake(
        _hass: HomeAssistant,
        _start_time: datetime,
        _end_time: datetime,
        entity_id: str,
        _no_attributes: bool,
        _descending: bool,
        _limit: int | None,
        _include_start_time_state: bool,
    ) -> dict[str, list[State]]:
        states = per_entity.get(entity_id)
        return {entity_id: states} if states is not None else {}

    return _fake


def _patched_history(per_entity: dict[str, list[State]]):
    return patch(
        "custom_components.emhass_companion.metering_replay.history.state_changes_during_period",
        side_effect=_history_stub(per_entity),
    )


@pytest.mark.parametrize("buy,sell", [(2.0, 0.5), (-0.5, -1.0)])
def test_no_battery_async_counters_match_live_and_replay(buy, sell):
    start = datetime(2026, 11, 2, 9, tzinfo=UTC)
    prices = Prices(buy=buy, sell=sell)
    fetched = {
        "sensor.grid_import": [
            _energy_state("sensor.grid_import", minute / 60, start + timedelta(minutes=minute))
            for minute in range(61)
        ],
        "sensor.grid_export": [_energy_state("sensor.grid_export", 0, start)],
        "sensor.pv": [
            _energy_state("sensor.pv", 0, start),
            _energy_state("sensor.pv", 1, start + timedelta(hours=1)),
        ],
        "sensor.house": [
            State("sensor.house", "2000", {"unit_of_measurement": "W"}, last_changed=start)
        ],
    }

    def meters():
        return MeterSpec(
            grid_import=Meter("sensor.grid_import", kind="energy"),
            grid_export=Meter("sensor.grid_export", kind="energy"),
            pv=Meter("sensor.pv", kind="energy"),
            house_load=Meter("sensor.house", kind="power"),
        )

    live = Ledger(battery_enabled=False)
    live_meters = meters()
    for minute in range(61):
        when = start + timedelta(minutes=minute)
        held = {
            entity_id: [state for state in states if state.last_changed <= when][-1]
            for entity_id, states in fetched.items()
        }
        live.record(read_meters(live_meters, when, held.get), prices)

    replay = Ledger(battery_enabled=False)
    _walk(fetched, meters(), lambda _when: prices, replay)

    assert replay == live
    assert live.imported_kwh == pytest.approx(1.0)
    assert live.pv_kwh == pytest.approx(1.0)
    assert live.house_load_kwh == pytest.approx(2.0)
    assert live.balance_residual_kwh == pytest.approx(0.0, abs=1e-9)
    assert live.solar_only_cost == pytest.approx(59 / 30 * buy - 29 / 30 * sell)
    assert live.solar_only_baseline_cost == pytest.approx(buy)
    assert live.solar_savings == pytest.approx(buy)
    assert live.accounting_difference == pytest.approx(29 / 30 * (buy - sell))
    assert live.battery_savings == 0.0
    assert live.battery_charge_kwh == live.battery_discharge_kwh == 0.0
    assert live.battery_book_kwh is None
    assert live.total_savings == pytest.approx(live.solar_savings)


@pytest.mark.parametrize("cross_midnight", [False, True])
async def test_no_battery_restore_and_rollover_corrects_legacy_baseline(
    hass: HomeAssistant, freezer, cross_midnight
):
    await hass.config.async_set_time_zone("UTC")
    start = datetime(2026, 11, 2, 9, tzinfo=UTC)
    freezer.move_to(start)
    entry = _entry(hass)
    legacy = _tracker(hass, entry, MeterSpec())
    legacy.ledger = Ledger(
        day="2026-11-02",
        actual_cost=46.23,
        solar_only_cost=53.55,
        grid_only_cost=63.08,
        battery_book_value=5.0,
        battery_book_value_start=1.0,
    )
    await legacy.async_shutdown()
    if cross_midnight:
        freezer.move_to(start + timedelta(days=1))

    restored = _tracker(hass, entry, MeterSpec(), battery_enabled=False)
    await restored.async_load()
    assert restored.ledger.battery_enabled is False
    assert restored.ledger.battery_savings == 0.0
    assert restored.ledger.storage_carry == 0.0
    assert restored.ledger.accounting_difference == pytest.approx(0.0 if cross_midnight else 7.32)
    assert restored.ledger.actual_cost == (0.0 if cross_midnight else 46.23)
    assert restored.ledger.solar_only_cost == (0.0 if cross_midnight else 53.55)
    assert restored.ledger.solar_only_baseline_cost == (0.0 if cross_midnight else 46.23)
    assert restored.ledger.solar_savings == pytest.approx(0.0 if cross_midnight else 16.85)

    restored._roll_over(dt_util.utcnow() + timedelta(days=1))
    assert restored.ledger.battery_enabled is False
    assert restored.ledger.battery_savings == 0.0
    assert restored.ledger.accounting_difference == 0.0
    await restored.async_shutdown()


@pytest.mark.parametrize("battery_enabled", [False, True])
async def test_soc_subscription_respects_battery_setting(hass, battery_enabled):
    tracker = SavingsTracker(
        hass,
        _entry(hass),
        MeterSpec(grid_import=Meter("sensor.grid_import", kind="energy")),
        soc_entity="sensor.old_soc",
        capacity_kwh=10.0,
        prices=lambda _when: _PRICES,
        plan_forecast=lambda _now, _window: None,
        add_price_listener=lambda _listener: lambda: None,
        battery_enabled=battery_enabled,
    )
    with patch(
        "custom_components.emhass_companion.metering.async_track_state_change_event",
        return_value=lambda: None,
    ) as subscribe:
        tracker.async_start()
    entities = subscribe.call_args.args[1]
    assert ("sensor.old_soc" in entities) is battery_enabled
    await tracker.async_shutdown()


async def test_all_meters_stale_builds_a_pending_gap_replay(hass: HomeAssistant, freezer) -> None:
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 8, 10, 8, 0, tzinfo=UTC))
    entry = _entry(hass)
    meters = MeterSpec(
        grid_import=Meter("sensor.grid_import", kind="energy"),
        grid_export=Meter("sensor.grid_export", kind="energy"),
        pv=Meter("sensor.pv", kind="energy"),
    )

    tracker_a = _tracker(hass, entry, meters)
    _set_energy(hass, "sensor.grid_import", 0.0)
    _set_energy(hass, "sensor.grid_export", 0.0)
    _set_energy(hass, "sensor.pv", 0.0)
    await tracker_a.async_load()
    tracker_a.async_start()
    await hass.async_block_till_done()

    freezer.tick(timedelta(minutes=5))
    shutdown_at = dt_util.utcnow()
    _set_energy(hass, "sensor.grid_import", 1.0)
    _set_energy(hass, "sensor.grid_export", 0.0)
    _set_energy(hass, "sensor.pv", 0.5)
    await hass.async_block_till_done()
    await tracker_a.async_shutdown()

    # Down long enough that Meter.restore disowns every meter's baseline.
    freezer.tick(_RESTORE_MAX_GAP + timedelta(minutes=10))
    _set_energy(hass, "sensor.grid_import", 3.0)  # +2.0 kWh across the gap
    _set_energy(hass, "sensor.grid_export", 0.0)  # unchanged
    _set_energy(hass, "sensor.pv", 1.5)  # +1.0 kWh across the gap

    tracker_b = _tracker(hass, entry, meters)
    await tracker_b.async_load()

    pending = tracker_b._pending_gap
    assert pending is not None
    assert set(pending.seeds) == {"grid_import", "grid_export", "pv"}
    assert pending.seeds["grid_import"].last_value == pytest.approx(1.0)
    assert pending.seeds["grid_import"].last_time == shutdown_at
    assert pending.window_start == shutdown_at
    assert pending.window_end == dt_util.utcnow()
    assert pending.missed_kwh == pytest.approx(3.0)  # 2.0 (grid) + 0.0 (export) + 1.0 (pv)
    # Step 3 leaves the existing write-off untouched: the same amount still
    # lands in unpriced_kwh, replay (step 4+) only ever subtracts it back out.
    assert tracker_b.ledger.unpriced_kwh == pytest.approx(3.0)


async def test_a_meter_reconfigured_mid_gap_skips_replay_entirely(
    hass: HomeAssistant, freezer
) -> None:
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 8, 10, 8, 0, tzinfo=UTC))
    entry = _entry(hass)
    meters_a = MeterSpec(
        grid_import=Meter("sensor.grid_import_old", kind="energy"),
        grid_export=Meter("sensor.grid_export", kind="energy"),
    )

    tracker_a = _tracker(hass, entry, meters_a)
    _set_energy(hass, "sensor.grid_import_old", 0.0)
    _set_energy(hass, "sensor.grid_export", 0.0)
    await tracker_a.async_load()
    tracker_a.async_start()
    await hass.async_block_till_done()

    freezer.tick(timedelta(minutes=5))
    _set_energy(hass, "sensor.grid_import_old", 1.0)
    _set_energy(hass, "sensor.grid_export", 0.0)
    await hass.async_block_till_done()
    await tracker_a.async_shutdown()

    freezer.tick(_RESTORE_MAX_GAP + timedelta(minutes=10))

    # The user repointed grid_import at a different entity while Home
    # Assistant was down -- the entity-id check pending_gap shares with
    # restore() exists exactly for this: the stored reading is a plausible
    # number from an unrelated meter and must not be trusted as a seed.
    meters_b = MeterSpec(
        grid_import=Meter("sensor.grid_import_new", kind="energy"),
        grid_export=Meter("sensor.grid_export", kind="energy"),
    )
    _set_energy(hass, "sensor.grid_import_new", 5.0)
    _set_energy(hass, "sensor.grid_export", 0.2)

    tracker_b = _tracker(hass, entry, meters_b)
    await tracker_b.async_load()

    assert tracker_b._pending_gap is None
    # grid_export's own write-off is unaffected by the skipped replay -- it
    # is still disowned into unpriced_kwh exactly as it would be today.
    assert tracker_b.ledger.unpriced_kwh == pytest.approx(0.2)


async def test_a_gap_crossing_midnight_clamps_window_start_to_today(
    hass: HomeAssistant, freezer
) -> None:
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 8, 10, 23, 50, tzinfo=UTC))
    entry = _entry(hass)
    meters = MeterSpec(
        grid_import=Meter("sensor.grid_import", kind="energy"),
        grid_export=Meter("sensor.grid_export", kind="energy"),
    )

    tracker_a = _tracker(hass, entry, meters)
    _set_energy(hass, "sensor.grid_import", 0.0)
    _set_energy(hass, "sensor.grid_export", 0.0)
    await tracker_a.async_load()
    tracker_a.async_start()
    await hass.async_block_till_done()

    freezer.tick(timedelta(minutes=5))  # 2026-08-10 23:55 UTC
    _set_energy(hass, "sensor.grid_import", 1.0)
    _set_energy(hass, "sensor.grid_export", 0.0)
    await hass.async_block_till_done()
    await tracker_a.async_shutdown()

    # Down across midnight, long enough that both meters are disowned.
    freezer.tick(timedelta(hours=1, minutes=30))  # 2026-08-11 01:25 UTC
    _set_energy(hass, "sensor.grid_import", 4.0)
    _set_energy(hass, "sensor.grid_export", 0.0)

    tracker_b = _tracker(hass, entry, meters)
    await tracker_b.async_load()

    pending = tracker_b._pending_gap
    assert pending is not None
    # The seed itself still remembers yesterday's real last reading...
    assert pending.seeds["grid_import"].last_time == datetime(2026, 8, 10, 23, 55, tzinfo=UTC)
    # ...but the replay window is clamped to today, because the ledger above
    # already closed yesterday out on its own rollover rule.
    assert pending.window_start == datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
    assert tracker_b.ledger.day == "2026-08-11"


async def test_fresh_install_has_no_pending_gap(hass: HomeAssistant, freezer) -> None:
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 8, 10, 8, 0, tzinfo=UTC))
    entry = _entry(hass)
    meters = MeterSpec(
        grid_import=Meter("sensor.grid_import", kind="energy"),
        grid_export=Meter("sensor.grid_export", kind="energy"),
    )
    tracker = _tracker(hass, entry, meters)

    await tracker.async_load()

    assert tracker._pending_gap is None
    assert tracker.ledger.unpriced_kwh == 0.0


# -- step 4: async_replay_gap ------------------------------------------------


async def test_a_counter_reset_mid_gap_is_summed_not_dropped(
    recorder_mock, hass: HomeAssistant, freezer
) -> None:
    await hass.config.async_set_time_zone("UTC")
    t0 = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    freezer.move_to(t0)
    seed = GapSeed(
        entity_id="sensor.grid_import", kind="energy", invert=False, last_value=10.0, last_time=t0
    )
    pending = PendingGapReplay(
        window_start=t0,
        window_end=t0 + timedelta(hours=3),
        missed_kwh=13.0,
        seeds={"grid_import": seed},
    )
    fetched = {
        "sensor.grid_import": [
            _energy_state("sensor.grid_import", 10.0, t0),  # the seed's own value
            _energy_state("sensor.grid_import", 15.0, t0 + timedelta(hours=1)),  # +5.0
            # An inverter's daily meter rolling over mid-gap -- the post-reset
            # reading is the energy since the reset, not a loss.
            _energy_state("sensor.grid_import", 2.0, t0 + timedelta(hours=2)),  # +2.0
            _energy_state("sensor.grid_import", 8.0, t0 + timedelta(hours=3)),  # +6.0
        ]
    }
    meters = MeterSpec(grid_import=Meter("sensor.grid_import", kind="energy"))
    ledger = Ledger(day="2026-08-10", unpriced_kwh=pending.missed_kwh)

    with _patched_history(fetched):
        ok = await async_replay_gap(hass, meters, lambda _now: _PRICES, ledger, pending)

    assert ok is True
    assert ledger.imported_kwh == pytest.approx(13.0)  # 5.0 + 2.0 + 6.0, reset included whole
    assert ledger.unpriced_kwh == pytest.approx(0.0)


@pytest.mark.parametrize("seed_value", [19.424, 19.438])
async def test_counter_correction_and_recovery_are_not_double_counted_in_replay(
    recorder_mock, hass: HomeAssistant, freezer, seed_value
) -> None:
    await hass.config.async_set_time_zone("Europe/Stockholm")
    start = datetime(2026, 11, 1, 13, 28, tzinfo=UTC)
    freezer.move_to(start + timedelta(hours=1))
    seed = GapSeed(
        entity_id="sensor.grid_import",
        kind="energy",
        invert=False,
        last_value=seed_value,
        last_time=start,
    )
    missed = 19.463 - seed_value
    pending = PendingGapReplay(
        window_start=start,
        window_end=start + timedelta(hours=1),
        missed_kwh=missed,
        seeds={"grid_import": seed},
    )
    fetched = {
        "sensor.grid_import": [
            _energy_state("sensor.grid_import", value, start + timedelta(seconds=index + 1))
            for index, value in enumerate([19.438, 19.433, 19.435, 19.446, 19.463])
        ]
    }
    meters = MeterSpec(grid_import=Meter("sensor.grid_import", kind="energy"))
    ledger = Ledger(day="2026-11-01", unpriced_kwh=missed)
    prices = Prices(buy=2.9, sell=0)
    with _patched_history(fetched):
        assert await async_replay_gap(hass, meters, lambda _now: prices, ledger, pending)
    assert ledger.imported_kwh == pytest.approx(missed)
    assert ledger.actual_cost == pytest.approx(missed * prices.buy)
    assert ledger.unpriced_kwh == pytest.approx(0)


async def test_live_correction_high_water_survives_tracker_reload(
    hass: HomeAssistant, freezer
) -> None:
    await hass.config.async_set_time_zone("Europe/Stockholm")
    freezer.move_to(datetime(2026, 11, 1, 13, 28, tzinfo=UTC))
    entry = _entry(hass)
    first = _tracker(hass, entry, MeterSpec(grid_import=Meter("sensor.grid_import", kind="energy")))
    _set_energy(hass, "sensor.grid_import", 19.424)
    await first.async_load()
    first.async_start()
    for value in (19.438, 19.433):
        freezer.tick(timedelta(seconds=1))
        _set_energy(hass, "sensor.grid_import", value)
        await hass.async_block_till_done()
    assert first.ledger.imported_kwh == pytest.approx(0.014)
    await first.async_shutdown()
    freezer.tick(timedelta(minutes=1))
    _set_energy(hass, "sensor.grid_import", 19.446)
    resumed = _tracker(
        hass, entry, MeterSpec(grid_import=Meter("sensor.grid_import", kind="energy"))
    )
    await resumed.async_load()
    resumed.async_start()
    assert resumed.ledger.imported_kwh == pytest.approx(0.022)
    assert resumed.ledger.actual_cost == pytest.approx(0.022 * _PRICES.buy)
    await resumed.async_shutdown()


@pytest.mark.parametrize("closing_reading", [19.435, 19.450])
async def test_gap_high_water_is_restored_before_live_tracking_and_delayed_replay(
    recorder_mock, hass: HomeAssistant, freezer, closing_reading
) -> None:
    await hass.config.async_set_time_zone("UTC")
    start = datetime(2026, 11, 1, 13, 0, tzinfo=UTC)
    freezer.move_to(start)
    entry = _entry(hass)
    first = _tracker(hass, entry, MeterSpec(grid_import=Meter("sensor.grid_import", kind="energy")))
    _set_energy(hass, "sensor.grid_import", 19.438)
    await first.async_load()
    first.async_start()
    await first.async_shutdown()
    fetched = {
        "sensor.grid_import": [
            _energy_state("sensor.grid_import", value, start + timedelta(minutes=index + 1))
            for index, value in enumerate([19.433, 19.446, 19.435])
        ]
    }
    freezer.move_to(start + timedelta(minutes=20))
    _set_energy(hass, "sensor.grid_import", closing_reading)
    prices: dict[str, Prices | None] = {"value": None}
    bus = _FakePriceListenerBus()
    resumed = _tracker(
        hass,
        entry,
        MeterSpec(grid_import=Meter("sensor.grid_import", kind="energy")),
        prices=lambda _when: prices["value"],
        add_price_listener=bus.add,
    )
    with _patched_history(fetched) as fetch:
        await resumed.async_load()
        assert fetch.call_count == 1
    accepted = max(19.446, closing_reading)
    replayed_kwh = accepted - 19.438
    assert resumed.meters.grid_import.as_dict()["last_value"] == pytest.approx(accepted)
    resumed.async_start()
    for value in (accepted, accepted + 0.004):
        freezer.tick(timedelta(seconds=1))
        _set_energy(hass, "sensor.grid_import", value)
        await hass.async_block_till_done()
    assert resumed.ledger.imported_kwh == pytest.approx(0.004)
    prices["value"] = _PRICES
    with patch(
        "custom_components.emhass_companion.metering_replay.history.state_changes_during_period",
        side_effect=AssertionError("Prepared replay must reuse its exact baseline history"),
    ):
        bus.listener()
        await hass.async_block_till_done()
    assert resumed._pending_gap is None
    assert resumed.ledger.imported_kwh == pytest.approx(replayed_kwh + 0.004)
    assert resumed.ledger.actual_cost == pytest.approx(replayed_kwh * _PRICES.buy)
    assert resumed.ledger.unpriced_kwh == pytest.approx(0.004)
    await resumed.async_shutdown()


async def test_failed_gap_preparation_cannot_replay_with_unaligned_baselines(
    recorder_mock, hass: HomeAssistant
) -> None:
    start = datetime(2026, 11, 1, 13, 0, tzinfo=UTC)
    seed = GapSeed("sensor.grid_import", "energy", False, 19.438, start)
    pending = PendingGapReplay(start, start + timedelta(hours=1), 0, {"grid_import": seed})
    meters = MeterSpec(grid_import=Meter("sensor.grid_import", kind="energy"))
    with _patched_history({}):
        prepared = await async_prepare_gap(hass, meters, pending)
    assert prepared.prepared
    assert prepared.history is None
    ledger = Ledger(day="2026-11-01")
    with patch(
        "custom_components.emhass_companion.metering_replay.history.state_changes_during_period",
        side_effect=AssertionError("A late history fetch would overlap live tracking"),
    ):
        assert not await async_replay_gap(hass, meters, lambda _when: _PRICES, ledger, prepared)
    assert ledger.imported_kwh == 0


async def test_one_meter_missing_coverage_leaves_the_ledger_untouched(
    recorder_mock, hass: HomeAssistant, freezer
) -> None:
    await hass.config.async_set_time_zone("UTC")
    t0 = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    freezer.move_to(t0)
    seeds = {
        "grid_import": GapSeed(
            entity_id="sensor.grid_import",
            kind="energy",
            invert=False,
            last_value=10.0,
            last_time=t0,
        ),
        "grid_export": GapSeed(
            entity_id="sensor.grid_export",
            kind="energy",
            invert=False,
            last_value=1.0,
            last_time=t0,
        ),
    }
    pending = PendingGapReplay(
        window_start=t0, window_end=t0 + timedelta(hours=1), missed_kwh=5.0, seeds=seeds
    )
    # grid_export has no recorder history reaching back to window_start --
    # purged, disabled, or only added after the gap began.
    fetched = {
        "sensor.grid_import": [
            _energy_state("sensor.grid_import", 10.0, t0),
            _energy_state("sensor.grid_import", 14.0, t0 + timedelta(hours=1)),
        ]
    }
    meters = MeterSpec(
        grid_import=Meter("sensor.grid_import", kind="energy"),
        grid_export=Meter("sensor.grid_export", kind="energy"),
    )
    ledger = Ledger(day="2026-08-10", unpriced_kwh=pending.missed_kwh)
    before = ledger.as_dict()

    with _patched_history(fetched):
        ok = await async_replay_gap(hass, meters, lambda _now: _PRICES, ledger, pending)

    assert ok is False
    assert ledger.as_dict() == before


async def test_a_fully_covered_gap_nets_unpriced_kwh_back_to_zero(
    recorder_mock, hass: HomeAssistant, freezer
) -> None:
    await hass.config.async_set_time_zone("UTC")
    t0 = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    freezer.move_to(t0)
    seeds = {
        "grid_import": GapSeed(
            entity_id="sensor.grid_import",
            kind="energy",
            invert=False,
            last_value=10.0,
            last_time=t0,
        ),
        "grid_export": GapSeed(
            entity_id="sensor.grid_export",
            kind="energy",
            invert=False,
            last_value=2.0,
            last_time=t0,
        ),
    }
    pending = PendingGapReplay(
        window_start=t0, window_end=t0 + timedelta(hours=2), missed_kwh=6.0, seeds=seeds
    )
    fetched = {
        "sensor.grid_import": [
            _energy_state("sensor.grid_import", 10.0, t0),
            _energy_state("sensor.grid_import", 13.0, t0 + timedelta(hours=1)),  # +3.0
            _energy_state("sensor.grid_import", 15.0, t0 + timedelta(hours=2)),  # +2.0
        ],
        "sensor.grid_export": [
            _energy_state("sensor.grid_export", 2.0, t0),
            _energy_state("sensor.grid_export", 2.5, t0 + timedelta(hours=1)),  # +0.5
            _energy_state("sensor.grid_export", 3.0, t0 + timedelta(hours=2)),  # +0.5
        ],
    }
    meters = MeterSpec(
        grid_import=Meter("sensor.grid_import", kind="energy"),
        grid_export=Meter("sensor.grid_export", kind="energy"),
    )
    prices = Prices(buy=2.0, sell=1.0)
    ledger = Ledger(day="2026-08-10", unpriced_kwh=pending.missed_kwh)

    with _patched_history(fetched):
        ok = await async_replay_gap(hass, meters, lambda _now: prices, ledger, pending)

    assert ok is True
    assert ledger.imported_kwh == pytest.approx(5.0)
    assert ledger.exported_kwh == pytest.approx(1.0)
    # (3.0*2.0 - 0.5*1.0) + (2.0*2.0 - 0.5*1.0), hand-computed same as
    # tests/test_savings.py's own IntervalEnergy scenarios.
    assert ledger.actual_cost == pytest.approx(9.0)
    assert ledger.unpriced_kwh == pytest.approx(0.0)


async def test_replay_against_a_real_recorder(recorder_mock, hass: HomeAssistant, freezer) -> None:
    """No stubbing: proves the executor-job/history-API wiring itself.

    The scenarios above pin the fetch/merge/walk logic against hand-built
    states; this is the one case that actually round-trips through a real
    in-memory recorder database, since nothing above exercises
    ``history.state_changes_during_period`` for real.
    """
    await hass.config.async_set_time_zone("UTC")
    t0 = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    freezer.move_to(t0)

    _set_energy(hass, "sensor.grid_import", 5.0)
    await hass.async_block_till_done()
    freezer.tick(timedelta(hours=1))
    _set_energy(hass, "sensor.grid_import", 8.0)  # +3.0
    await hass.async_block_till_done()
    freezer.tick(timedelta(hours=1))
    _set_energy(hass, "sensor.grid_import", 9.5)  # +1.5
    await hass.async_block_till_done()
    # Recorder's own start/end bounds are both exclusive -- a window_end that
    # lands on the dot on the last real change would drop it, which a real
    # restart never does (window_end is "now" at load time, well after the
    # gap's own last meter movement).
    freezer.tick(timedelta(minutes=1))
    window_end = dt_util.utcnow()
    await async_wait_recording_done(hass)

    seed = GapSeed(
        entity_id="sensor.grid_import", kind="energy", invert=False, last_value=5.0, last_time=t0
    )
    pending = PendingGapReplay(
        window_start=t0, window_end=window_end, missed_kwh=4.5, seeds={"grid_import": seed}
    )
    meters = MeterSpec(grid_import=Meter("sensor.grid_import", kind="energy"))
    ledger = Ledger(day="2026-08-10", unpriced_kwh=pending.missed_kwh)

    ok = await async_replay_gap(hass, meters, lambda _now: _PRICES, ledger, pending)

    assert ok is True
    assert ledger.imported_kwh == pytest.approx(4.5)
    assert ledger.unpriced_kwh == pytest.approx(0.0)


# -- step 5: the one-shot listener -------------------------------------------


async def _restart_with_pending_gap(
    hass: HomeAssistant,
    freezer,
    *,
    prices: Callable[[datetime], Prices | None],
    add_price_listener: Callable[[CALLBACK_TYPE], CALLBACK_TYPE],
) -> SavingsTracker:
    """Drive a real restart gap, returning the post-restart tracker.

    ``tracker_a`` (the pre-restart half) always prices for real, matching
    every other scenario in this file -- only ``tracker_b``, the one under
    test, gets the caller's ``prices``/``add_price_listener``.
    """
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 8, 10, 8, 0, tzinfo=UTC))
    entry = _entry(hass)
    meters = MeterSpec(grid_import=Meter("sensor.grid_import", kind="energy"))

    tracker_a = _tracker(hass, entry, meters)
    _set_energy(hass, "sensor.grid_import", 0.0)
    await tracker_a.async_load()
    tracker_a.async_start()
    await hass.async_block_till_done()

    freezer.tick(timedelta(minutes=5))
    _set_energy(hass, "sensor.grid_import", 1.0)
    await hass.async_block_till_done()
    await tracker_a.async_shutdown()

    # Down long enough that Meter.restore disowns the baseline.
    freezer.tick(_RESTORE_MAX_GAP + timedelta(minutes=10))
    _set_energy(hass, "sensor.grid_import", 3.0)

    tracker_b = _tracker(hass, entry, meters, prices=prices, add_price_listener=add_price_listener)
    await tracker_b.async_load()
    assert tracker_b._pending_gap is not None
    return tracker_b


async def test_listener_fires_before_prices_exist_does_nothing(
    hass: HomeAssistant, freezer
) -> None:
    bus = _FakePriceListenerBus()
    price_holder: dict[str, Prices | None] = {"value": None}

    with patch("custom_components.emhass_companion.metering_replay.async_replay_gap") as replay:
        tracker = await _restart_with_pending_gap(
            hass, freezer, prices=lambda _now: price_holder["value"], add_price_listener=bus.add
        )
        pending = tracker._pending_gap
        # async_load's own immediate call already fired once with no price
        # yet -- simulate one more ordinary coordinator refresh, still dry.
        assert bus.listener is not None
        bus.listener()
        await hass.async_block_till_done()

    replay.assert_not_called()
    assert tracker._pending_gap is pending
    assert tracker._gap_replay_unsub is not None
    assert bus.unsub_calls == 0


async def test_listener_fires_after_prices_exist_runs_replay_and_unsubscribes(
    hass: HomeAssistant, freezer
) -> None:
    bus = _FakePriceListenerBus()
    price_holder: dict[str, Prices | None] = {"value": None}
    tracker = await _restart_with_pending_gap(
        hass, freezer, prices=lambda _now: price_holder["value"], add_price_listener=bus.add
    )
    pending = tracker._pending_gap
    assert bus.unsub_calls == 0  # still unresolved after the load-time call

    price_holder["value"] = _PRICES
    with patch("custom_components.emhass_companion.metering_replay.async_replay_gap") as replay:
        replay.return_value = True
        assert bus.listener is not None
        bus.listener()
        await hass.async_block_till_done()

    replay.assert_called_once_with(
        tracker.hass, tracker.meters, tracker._prices, tracker.ledger, pending
    )
    assert tracker._pending_gap is None
    assert bus.unsub_calls == 1


async def test_a_replay_exception_is_caught_and_clears_pending_gap(
    hass: HomeAssistant, freezer
) -> None:
    bus = _FakePriceListenerBus()

    with patch(
        "custom_components.emhass_companion.metering_replay.async_replay_gap",
        side_effect=RuntimeError("boom"),
    ):
        tracker = await _restart_with_pending_gap(
            hass, freezer, prices=lambda _now: _PRICES, add_price_listener=bus.add
        )
        before = tracker.ledger.as_dict()
        await hass.async_block_till_done()

    assert tracker._pending_gap is None
    assert tracker.ledger.as_dict() == before


async def test_shutdown_while_still_pending_unsubscribes(hass: HomeAssistant, freezer) -> None:
    bus = _FakePriceListenerBus()
    tracker = await _restart_with_pending_gap(
        hass, freezer, prices=lambda _now: None, add_price_listener=bus.add
    )
    assert bus.unsub_calls == 0

    await tracker.async_shutdown()

    assert bus.unsub_calls == 1

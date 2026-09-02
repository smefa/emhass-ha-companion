"""Tests for :class:`Meter`'s state-agnostic core.

``take_from``/``take_signed_from`` are the seam a historical replay of a
restart gap walks: the same reset-detection and unit-conversion rules that
drive the live ticker, fed a recorder sample instead of a live
``hass.states.get`` lookup. These tests exercise that seam directly, with
hand-built ``State`` objects, so the live behavior stays pinned as that
replay is built on top of it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.core import State
import pytest

from custom_components.emhass_companion.metering import (
    _RESTORE_MAX_GAP,
    Meter,
    MeterSpec,
    read_meters,
)

T0 = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


def _energy_state(value: float, unit: str | None = "kWh") -> State:
    attrs = {"unit_of_measurement": unit} if unit else {}
    return State("sensor.meter", str(value), attrs)


def _power_state(value: float, unit: str | None = "W") -> State:
    attrs = {"unit_of_measurement": unit} if unit else {}
    return State("sensor.power", str(value), attrs)


# --- energy counters -----------------------------------------------------------


def test_first_energy_sample_only_baselines():
    meter = Meter("sensor.meter", kind="energy")
    assert meter.take_from(_energy_state(10.0), T0) == 0.0


def test_energy_diffs_against_the_previous_sample():
    meter = Meter("sensor.meter", kind="energy")
    meter.take_from(_energy_state(10.0), T0)
    moved = meter.take_from(_energy_state(12.5), T0 + timedelta(minutes=5))
    assert moved == pytest.approx(2.5)


def test_energy_reset_is_the_post_reset_reading():
    # total_increasing counters are allowed to reset (an inverter's daily
    # meter rolling over at local midnight is the common case).
    meter = Meter("sensor.meter", kind="energy")
    meter.take_from(_energy_state(10.0), T0)
    moved = meter.take_from(_energy_state(1.2), T0 + timedelta(minutes=5))
    assert moved == pytest.approx(1.2)


def test_unavailable_energy_reading_holds_the_baseline():
    meter = Meter("sensor.meter", kind="energy")
    meter.take_from(_energy_state(10.0), T0)
    moved = meter.take_from(None, T0 + timedelta(minutes=5))
    assert moved == 0.0
    # The next good sample picks up everything that flowed in between --
    # last_time never advanced, so this is still a diff from 10.0.
    moved = meter.take_from(_energy_state(13.0), T0 + timedelta(minutes=10))
    assert moved == pytest.approx(3.0)


def test_energy_unit_conversion_from_watt_hours():
    meter = Meter("sensor.meter", kind="energy")
    meter.take_from(_energy_state(1000.0, unit="Wh"), T0)
    moved = meter.take_from(_energy_state(1500.0, unit="Wh"), T0 + timedelta(minutes=5))
    assert moved == pytest.approx(0.5)


def test_energy_invert_flips_the_sign():
    meter = Meter("sensor.meter", kind="energy", invert=True)
    meter.take_from(_energy_state(10.0), T0)
    moved = meter.take_from(_energy_state(12.5), T0 + timedelta(minutes=5))
    assert moved == pytest.approx(-2.5)


# --- power sensors ---------------------------------------------------------------


def test_first_power_sample_only_baselines():
    meter = Meter("sensor.power", kind="power")
    assert meter.take_from(_power_state(1000.0), T0) == 0.0


def test_power_integrates_the_held_previous_reading_over_elapsed_time():
    meter = Meter("sensor.power", kind="power")
    meter.take_from(_power_state(2000.0), T0)  # 2 kW held
    moved = meter.take_from(_power_state(1000.0), T0 + timedelta(minutes=6))
    assert moved == pytest.approx(0.2)  # 2 kW for 6 minutes (0.1 h)


def test_power_refuses_to_integrate_across_a_long_gap():
    meter = Meter("sensor.power", kind="power")
    meter.take_from(_power_state(2000.0), T0)
    moved = meter.take_from(
        _power_state(1000.0), T0 + _RESTORE_MAX_GAP + timedelta(minutes=1)
    )
    assert moved == 0.0


# --- take_signed_from --------------------------------------------------------------


def test_take_signed_splits_into_positive_and_negative():
    # take_signed is for a single signed power sensor (a battery's, say) --
    # not an "energy" counter, where a falling reading means a reset rather
    # than a real negative flow.
    meter = Meter("sensor.power", kind="power")
    meter.take_from(_power_state(-2000.0), T0)  # baseline, holding -2 kW (charging)
    positive, negative = meter.take_signed_from(_power_state(1000.0), T0 + timedelta(minutes=6))
    assert positive == 0.0
    assert negative == pytest.approx(0.2)  # 2 kW charge for 6 minutes (0.1 h)


# --- replaying a sequence of historical states ------------------------------------


def test_a_reset_mid_sequence_is_caught_at_the_right_step():
    """The exact mechanic a gap replay depends on: walking several historical
    samples pairwise must catch a reset wherever it actually happened, not
    just when comparing the sequence's first and last values."""
    meter = Meter("sensor.meter", kind="energy")
    samples = [
        (_energy_state(8.0), T0),
        (_energy_state(9.5), T0 + timedelta(minutes=5)),  # +1.5
        (_energy_state(0.4), T0 + timedelta(minutes=10)),  # reset -> +0.4
        (_energy_state(2.0), T0 + timedelta(minutes=15)),  # +1.6
    ]
    moved = [meter.take_from(state, when) for state, when in samples]
    assert moved == [0.0, pytest.approx(1.5), pytest.approx(0.4), pytest.approx(1.6)]


# --- seeded ------------------------------------------------------------------------


def test_seeded_meter_resumes_instead_of_baselining():
    meter = Meter.seeded(
        "sensor.meter", kind="energy", last_value=10.0, last_time=T0
    )
    moved = meter.take_from(_energy_state(12.0), T0 + timedelta(minutes=5))
    assert moved == pytest.approx(2.0)


def test_seeded_meter_with_no_reading_still_baselines_blind():
    meter = Meter.seeded("sensor.meter", kind="energy", last_value=None, last_time=None)
    assert meter.take_from(_energy_state(5.0), T0) == 0.0


# --- pending_gap ---------------------------------------------------------------


def test_pending_gap_is_none_for_a_mismatched_entity_or_kind():
    meter = Meter("sensor.meter", kind="energy")
    data = {"entity_id": "sensor.other", "kind": "energy", "last_value": 1.0, "last_time": T0.isoformat()}
    assert meter.pending_gap(data, now=T0 + timedelta(hours=1)) is None

    data = {"entity_id": "sensor.meter", "kind": "power", "last_value": 1.0, "last_time": T0.isoformat()}
    assert meter.pending_gap(data, now=T0 + timedelta(hours=1)) is None


def test_pending_gap_is_none_under_the_threshold():
    meter = Meter("sensor.meter", kind="energy")
    data = {"entity_id": "sensor.meter", "kind": "energy", "last_value": 1.0, "last_time": T0.isoformat()}
    now = T0 + _RESTORE_MAX_GAP - timedelta(seconds=1)
    assert meter.pending_gap(data, now=now) is None


def test_pending_gap_returns_the_stored_time_once_over_the_threshold():
    meter = Meter("sensor.meter", kind="energy")
    data = {"entity_id": "sensor.meter", "kind": "energy", "last_value": 1.0, "last_time": T0.isoformat()}
    now = T0 + _RESTORE_MAX_GAP + timedelta(seconds=1)
    assert meter.pending_gap(data, now=now) == T0


@pytest.mark.parametrize(
    ("last_time", "now_offset", "expect_gap"),
    [
        (T0.isoformat(), _RESTORE_MAX_GAP - timedelta(minutes=1), False),
        (T0.isoformat(), _RESTORE_MAX_GAP + timedelta(minutes=1), True),
    ],
)
def test_restore_agrees_with_pending_gap(last_time, now_offset, expect_gap):
    """restore() and pending_gap() must never disagree about which branch a
    given stored reading falls into -- replay's bookkeeping (step 3) trusts
    pending_gap() to know exactly which meters restore() is about to disown.
    """
    data = {"entity_id": "sensor.meter", "kind": "energy", "last_value": 1.0, "last_time": last_time}
    now = T0 + now_offset

    probe = Meter("sensor.meter", kind="energy")
    took_gap_branch = probe.pending_gap(data, now=now) is not None
    assert took_gap_branch == expect_gap

    live = Meter("sensor.meter", kind="energy")
    live.restore(_FakeHass(), data, now=now)
    # The gap branch always nulls the baseline; the short-gap branch always
    # re-adopts the stored reading -- so the post-restore baseline state is
    # an independent witness that restore() took the same branch pending_gap
    # predicted.
    assert (live._last_value is None) == expect_gap


class _FakeHass:
    """A stand-in for HomeAssistant.states.get, used only by restore()'s
    energy-kind write-off branch when the gap is over threshold."""

    class states:  # noqa: N801 - mirrors hass.states' shape
        @staticmethod
        def get(entity_id: str) -> State | None:
            return None


# --- read_meters ---------------------------------------------------------------


def _states(mapping: dict[str, State]):
    return lambda entity_id: mapping.get(entity_id)


def test_read_meters_takes_every_configured_meter_once():
    meters = MeterSpec(
        grid_import=Meter("sensor.grid_import", kind="energy"),
        grid_export=Meter("sensor.grid_export", kind="energy"),
        pv=Meter("sensor.pv", kind="energy"),
        battery_charge=Meter("sensor.battery_charge", kind="energy"),
        battery_discharge=Meter("sensor.battery_discharge", kind="energy"),
    )
    # Baseline every meter first (its own first sample never counts).
    read_meters(
        meters,
        T0,
        _states(
            {
                "sensor.grid_import": _energy_state(10.0),
                "sensor.grid_export": _energy_state(1.0),
                "sensor.pv": _energy_state(5.0),
                "sensor.battery_charge": _energy_state(2.0),
                "sensor.battery_discharge": _energy_state(3.0),
            }
        ),
    )
    energy = read_meters(
        meters,
        T0 + timedelta(minutes=5),
        _states(
            {
                "sensor.grid_import": _energy_state(11.5),
                "sensor.grid_export": _energy_state(1.2),
                "sensor.pv": _energy_state(6.0),
                "sensor.battery_charge": _energy_state(2.4),
                "sensor.battery_discharge": _energy_state(3.9),
            }
        ),
    )
    assert energy.imported == pytest.approx(1.5)
    assert energy.exported == pytest.approx(0.2)
    assert energy.pv == pytest.approx(1.0)
    assert energy.battery_charge == pytest.approx(0.4)
    assert energy.battery_discharge == pytest.approx(0.9)
    assert energy.house_load is None  # unconfigured -> derived downstream, not measured here


def test_read_meters_splits_a_single_signed_battery_power_sensor():
    meters = MeterSpec(
        grid_import=Meter("sensor.grid_import", kind="energy"),
        grid_export=Meter("sensor.grid_export", kind="energy"),
        battery_power=Meter("sensor.battery_power", kind="power"),
        house_load=Meter("sensor.house_load", kind="power"),
    )
    read_meters(
        meters,
        T0,
        _states(
            {
                "sensor.grid_import": _energy_state(10.0),
                "sensor.grid_export": _energy_state(1.0),
                "sensor.battery_power": _power_state(-2000.0),  # charging
                "sensor.house_load": _power_state(500.0),
            }
        ),
    )
    energy = read_meters(
        meters,
        T0 + timedelta(minutes=6),
        _states(
            {
                "sensor.grid_import": _energy_state(10.0),
                "sensor.grid_export": _energy_state(1.0),
                "sensor.battery_power": _power_state(0.0),
                "sensor.house_load": _power_state(500.0),
            }
        ),
    )
    # -2 kW held for 6 minutes (0.1 h) -> 0.2 kWh, all charge, no discharge.
    assert energy.battery_charge == pytest.approx(0.2)
    assert energy.battery_discharge == 0.0
    # 0.5 kW held for 6 minutes -> 0.05 kWh, measured directly this time.
    assert energy.house_load == pytest.approx(0.05)

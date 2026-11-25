"""Unit tests for BatteryReserve and MPC ``soc_floor`` payload emission."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.emhass_companion.battery_reserve import (
    STATUS_ACTIVE,
    STATUS_OFF,
    STATUS_SCHEDULED,
    BatteryReserve,
    from_json,
)
from custom_components.emhass_companion.const import ACTION_DAYAHEAD, ACTION_MPC
from custom_components.emhass_companion.models import (
    BatteryConfig,
    GridConfig,
    HybridInverterConfig,
)
from custom_components.emhass_companion.payload import PayloadInputs, build_payload

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def _inputs(
    *,
    action: str = ACTION_MPC,
    backend_version: str | None = "0.19.0",
    battery_enabled: bool = True,
    reserve: BatteryReserve | None = None,
    soc_init: float | None = 0.4,
    soc_final: float | None = 0.2,
    horizon_steps: int = 8,
) -> PayloadInputs:
    return PayloadInputs(
        action=action,
        now=NOW,
        time_step_minutes=60,
        horizon_steps=horizon_steps,
        battery=BatteryConfig(enabled=battery_enabled, capacity_wh=10_000),
        grid=GridConfig(),
        hybrid_inverter=HybridInverterConfig(),
        backend_version=backend_version,
        soc_init=soc_init,
        soc_final=soc_final,
        battery_reserve=reserve,
    )


def test_reserve_validation_and_status() -> None:
    reserve = BatteryReserve(
        target_soc_pct=80,
        start_at=NOW + timedelta(hours=2),
        end_at=NOW + timedelta(hours=6),
        reason="  Storm prep  ",
    )
    assert reserve.reason == "Storm prep"
    assert reserve.status(NOW) == STATUS_SCHEDULED
    assert reserve.status(NOW + timedelta(hours=2)) == STATUS_ACTIVE
    assert reserve.status(NOW + timedelta(hours=5, minutes=59)) == STATUS_ACTIVE
    assert reserve.status(NOW + timedelta(hours=6)) == STATUS_OFF
    assert reserve.is_expired(NOW + timedelta(hours=6))

    with pytest.raises(ValueError):
        BatteryReserve(target_soc_pct=0, start_at=NOW, end_at=NOW + timedelta(hours=1))
    with pytest.raises(ValueError):
        BatteryReserve(target_soc_pct=105, start_at=NOW, end_at=NOW + timedelta(hours=1))
    with pytest.raises(ValueError):
        BatteryReserve(target_soc_pct=80, start_at=NOW, end_at=NOW)


def test_soc_floor_array_has_lead_in_nulls_and_window_fractions() -> None:
    reserve = BatteryReserve(
        target_soc_pct=80,
        start_at=NOW + timedelta(hours=2),
        end_at=NOW + timedelta(hours=5),
    )
    arr = reserve.soc_floor_array(NOW, timedelta(hours=1), 8)
    assert arr == [None, None, 0.8, 0.8, 0.8, None, None, None]

    # Window completely beyond horizon returns None.
    far = BatteryReserve(
        target_soc_pct=80,
        start_at=NOW + timedelta(hours=10),
        end_at=NOW + timedelta(hours=12),
    )
    assert far.soc_floor_array(NOW, timedelta(hours=1), 8) is None


def test_json_round_trip() -> None:
    reserve = BatteryReserve(
        target_soc_pct=85.5,
        start_at=NOW + timedelta(hours=1),
        end_at=NOW + timedelta(hours=4),
        reason="Storm",
    )
    restored = from_json(reserve.to_json())
    assert restored == reserve
    assert from_json({}) is None
    assert from_json(None) is None


def test_payload_emits_soc_floor_on_mpc_019() -> None:
    reserve = BatteryReserve(
        target_soc_pct=80,
        start_at=NOW + timedelta(hours=2),
        end_at=NOW + timedelta(hours=6),
    )
    res = build_payload(_inputs(reserve=reserve))
    assert res.payload["soc_floor"] == [None, None, 0.8, 0.8, 0.8, 0.8, None, None]
    assert res.payload["soc_final"] == 0.2
    assert not any("Battery reserve" in w for w in res.warnings)


def test_payload_keeps_soc_final_when_reserve_reaches_horizon_end() -> None:
    reserve = BatteryReserve(
        target_soc_pct=80,
        start_at=NOW + timedelta(hours=4),
        end_at=NOW + timedelta(hours=12),
    )
    res = build_payload(_inputs(reserve=reserve, soc_final=0.2))
    assert res.payload["soc_floor"] == [None, None, None, None, 0.8, 0.8, 0.8, 0.8]
    assert res.payload["soc_final"] == 0.2


def test_lead_in_start_time_shifts_immediate_start_when_below_target() -> None:
    from custom_components.emhass_companion.battery_reserve import lead_in_start_time

    step = timedelta(minutes=15)
    row_zero = NOW  # 12:00 UTC
    end = NOW + timedelta(hours=6)

    # Below target (30% -> 80%, 1h charge at 0.95 eff => 5 steps = 75 min)
    shifted = lead_in_start_time(
        NOW + timedelta(minutes=5),
        end,
        step=step,
        row_zero=row_zero,
        current_soc=0.30,
        target_soc_pct=80.0,
        hours_to_charge=1.0,
        charge_efficiency=0.95,
    )
    assert shifted == NOW + timedelta(minutes=75)
    reserve = BatteryReserve(target_soc_pct=80.0, start_at=shifted, end_at=end)
    floor = reserve.soc_floor_array(row_zero, step, 8)
    assert floor is not None
    assert floor[:5] == [None, None, None, None, None]
    assert floor[5] == 0.8

    # Already at/above target -> no shift
    same = lead_in_start_time(
        NOW + timedelta(minutes=5),
        end,
        step=step,
        row_zero=row_zero,
        current_soc=0.85,
        target_soc_pct=80.0,
        hours_to_charge=0.0,
    )
    assert same == NOW + timedelta(minutes=5)

    # Future start already past step 0 -> untouched
    future = NOW + timedelta(hours=2)
    assert (
        lead_in_start_time(
            future,
            end,
            step=step,
            row_zero=row_zero,
            current_soc=0.30,
            target_soc_pct=80.0,
            hours_to_charge=1.0,
        )
        == future
    )

    # Short window where full lead-in exceeds end_at -> shifts by 1 step
    short_end = NOW + timedelta(minutes=45)
    assert (
        lead_in_start_time(
            NOW + timedelta(minutes=5),
            short_end,
            step=step,
            row_zero=row_zero,
            current_soc=0.30,
            target_soc_pct=80.0,
            hours_to_charge=2.0,
        )
        == NOW + step
    )


def test_payload_gates_soc_floor_by_version_action_and_battery() -> None:
    reserve = BatteryReserve(
        target_soc_pct=80,
        start_at=NOW + timedelta(hours=1),
        end_at=NOW + timedelta(hours=4),
    )
    old = build_payload(_inputs(backend_version="0.18.11", reserve=reserve))
    assert "soc_floor" not in old.payload
    assert any("0.19.0" in w for w in old.warnings)

    dayahead = build_payload(_inputs(action=ACTION_DAYAHEAD, reserve=reserve))
    assert "soc_floor" not in dayahead.payload

    no_batt = build_payload(_inputs(battery_enabled=False, reserve=reserve, soc_init=None))
    assert "soc_floor" not in no_batt.payload

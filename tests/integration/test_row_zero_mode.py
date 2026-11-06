"""The coordinator learns how EMHASS rounds a launch time onto plan row zero."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import ACTION_MPC, DOMAIN
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.models import Plan, Series


def _coordinator(hass: HomeAssistant) -> EmhassCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"})
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    return EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)


def _plan(first_row: str) -> Plan:
    return Plan.from_response(
        {
            "status": "ok",
            "generated_at": first_row,
            "emhass_schema_version": "1.0",
            "plan": [{"timestamp": first_row, "P_deferrable0": 0.0}],
        }
    )


def _inputs(*, second: int) -> SimpleNamespace:
    return SimpleNamespace(
        now=datetime(2026, 10, 28, 11, 7, second, tzinfo=UTC), time_step_minutes=15
    )


async def test_defaults_to_nearest(hass: HomeAssistant) -> None:
    assert _coordinator(hass)._row_zero_mode == "nearest"


async def test_configured_pv_survives_empty_series_and_missing_prices_warn(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    coordinator.config.pv.key = "pv/generic_attribute"
    with (
        patch.object(coordinator, "_series", new=AsyncMock(return_value=Series.empty())),
        patch.object(coordinator, "_settings", return_value={}),
    ):
        inputs, built = await coordinator._build(ACTION_MPC)
    assert inputs.use_pv is True
    assert inputs.pv is None
    assert built.payload["set_use_pv"] is True
    assert any("No configured buy price forecast" in warning for warning in built.warnings)
    assert any("No configured sell price forecast" in warning for warning in built.warnings)


async def test_a_second_half_run_stamped_with_the_floor_switches_to_first(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)

    coordinator._learn_row_zero_mode(_inputs(second=33), _plan("2026-10-28T11:00:00Z"))

    assert coordinator._row_zero_mode == "first"


async def test_a_second_half_run_stamped_with_the_next_step_keeps_nearest(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    coordinator._row_zero_mode = "first"

    coordinator._learn_row_zero_mode(_inputs(second=33), _plan("2026-10-28T11:15:00Z"))

    assert coordinator._row_zero_mode == "nearest"


async def test_a_first_half_run_says_nothing(hass: HomeAssistant) -> None:
    """11:07:12 rounds to 11:00 under nearest and first alike (last differs)."""
    coordinator = _coordinator(hass)
    coordinator._row_zero_mode = "first"

    coordinator._learn_row_zero_mode(_inputs(second=12), _plan("2026-10-28T11:00:00Z"))

    assert coordinator._row_zero_mode == "first"


async def test_a_launch_that_crossed_the_cut_off_while_preparing_is_ignored(
    hass: HomeAssistant,
) -> None:
    """Prepared at 11:07:29, sent at 11:07:31: EMHASS's clock is ambiguous."""
    coordinator = _coordinator(hass)

    coordinator._learn_row_zero_mode(
        _inputs(second=29),
        _plan("2026-10-28T11:15:00Z"),
        datetime(2026, 10, 28, 11, 7, 31, tzinfo=UTC),
    )

    assert coordinator._row_zero_mode == "nearest"

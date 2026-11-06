"""Tank runtime projection, weather independence and fail-closed solves."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.configuration import ProfileSelection
from custom_components.emhass_companion.const import ACTION_MPC, DOMAIN
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry, live_settings
from custom_components.emhass_companion.models import LastRun, Plan
from custom_components.emhass_companion.water_tank import WaterTankConfig

NOW = datetime(2026, 11, 3, 5, tzinfo=UTC)
SENSOR = "sensor.hot_water_temperature"


def _coordinator(hass: HomeAssistant, **overrides) -> EmhassCoordinator:
    data = {
        "name": "Hot water",
        "thermal_model": "water_tank",
        "nominal_power_w": 2000,
        "temperature_sensor": SENSOR,
        "tank_volume_litres": 200,
        "standby_loss_w": 30,
        "comfort_temperature": 45,
        "setback_temperature": 37,
        "max_temperature": 52,
        "comfort_start": "06:00:00",
        "comfort_end": "22:00:00",
        "draw_start": "07:00:00",
        "draw_end": "08:00:00",
        "draw_energy_kwh": 2,
        **overrides,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[
            {
                "subentry_type": "thermal_load",
                "title": "Hot water",
                "unique_id": "hot-water",
                "data": data,
            }
        ],
    )
    entry.add_to_hass(hass)
    registry = DeferrableRegistry(hass, entry)
    registry.sync()
    coordinator = EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), registry)
    coordinator.backend_version = "0.18.8"
    hass.states.async_set(SENSOR, "45", {"unit_of_measurement": UnitOfTemperature.CELSIUS})
    return coordinator


async def test_tank_projection_uses_live_entity_settings(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    (runtime,) = coordinator.loads.all()
    runtime.nominal_power_w = 3000
    runtime.standby_loss_w = 45
    hass.states.async_set(SENSOR, "46")
    (projected,) = coordinator.deferrable_loads(NOW)
    assert isinstance(projected.thermal, WaterTankConfig)
    assert projected.thermal.current_temperature == 46
    assert projected.thermal.nominal_power_w == 3000
    assert projected.thermal.standby_loss_w == 45
    assert coordinator.loads.has_thermal
    assert not coordinator.loads.needs_outdoor_temperature
    assert "standby_loss_w" in live_settings(runtime)
    assert "heating_rate" not in live_settings(runtime)


@pytest.mark.parametrize("version", [None, "unknown", "dev", "0.18.7", "0.18.8.dev1"])
async def test_unsupported_backend_blocks_tank_solve(hass: HomeAssistant, version) -> None:
    coordinator = _coordinator(hass)
    coordinator.backend_version = version
    with pytest.raises(UpdateFailed, match=r"0\.18\.8"):
        coordinator.deferrable_loads(NOW)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "water_tank_unsupported") is not None
    coordinator.client.async_optimize.assert_not_called()


@pytest.mark.parametrize("version", ["0.18.8", "0.18.8+local", "0.18.9", "1.0.0"])
async def test_known_supported_version_projects_tank(hass: HomeAssistant, version) -> None:
    coordinator = _coordinator(hass)
    coordinator.backend_version = version
    assert coordinator.deferrable_loads(NOW)[0].thermal is not None


@pytest.mark.parametrize(
    "reading", [None, "unknown", "unavailable", "NaN", "inf", "-1", "101", "bad"]
)
async def test_invalid_temperature_blocks_solve_and_names_tank(
    hass: HomeAssistant, reading
) -> None:
    coordinator = _coordinator(hass)
    if reading is None:
        hass.states.async_remove(SENSOR)
    else:
        hass.states.async_set(SENSOR, reading)
    with pytest.raises(UpdateFailed, match="Hot water"):
        coordinator.deferrable_loads(NOW)
    (runtime,) = coordinator.loads.all()
    assert coordinator.water_tank_error(runtime)
    hass.states.async_set(SENSOR, "45")
    assert coordinator.water_tank_error(runtime)
    assert coordinator.deferrable_loads(NOW)[0].thermal is not None


async def test_fahrenheit_tank_sensor_is_converted(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    hass.states.async_set(SENSOR, "113", {"unit_of_measurement": UnitOfTemperature.FAHRENHEIT})
    assert coordinator.deferrable_loads(NOW)[0].thermal.current_temperature == pytest.approx(45)


async def test_disabled_tank_needs_neither_temperature_nor_backend(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.backend_version = None
    (runtime,) = coordinator.loads.all()
    runtime.enabled = False
    hass.states.async_remove(SENSOR)
    (projected,) = coordinator.deferrable_loads(NOW)
    assert projected.thermal is None
    assert not projected.wants_to_run


@pytest.mark.parametrize(
    "overrides",
    [
        {"tank_volume_litres": 0},
        {"draw_energy_kwh": -1},
        {"draw_start": "bad"},
        {"comfort_temperature": 60},
        {"standby_loss_w": -1},
    ],
)
async def test_invalid_tank_settings_block_instead_of_room_fallback(
    hass: HomeAssistant, overrides
) -> None:
    coordinator = _coordinator(hass, **overrides)
    with pytest.raises(UpdateFailed, match="Hot water"):
        coordinator.deferrable_loads(NOW)


async def test_tank_only_build_ignores_unconfigured_weather(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.config.temperature = ProfileSelection(key="missing/weather")
    inputs, built = await coordinator._build(ACTION_MPC)
    assert inputs.outdoor_temperature is None
    assert "outdoor_temperature_forecast" not in built.payload
    assert "thermal_battery" in built.payload["def_load_config"][0]


async def test_old_entry_stays_room_even_with_high_targets(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    (runtime,) = coordinator.loads.all()
    runtime.thermal_model = "room"
    coordinator.backend_version = None
    assert not runtime.is_water_tank
    assert coordinator.loads.needs_outdoor_temperature
    assert (
        coordinator.deferrable_loads(NOW)[0].thermal.to_emhass(NOW, timedelta(minutes=30), 2)[
            "heating_rate"
        ]
        == 3
    )


async def test_successful_solve_releases_invalid_plan_hold(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator._water_tank_plan_blocked = "previous telemetry loss"
    inputs, built = await coordinator._build(ACTION_MPC)
    plan = Plan.from_response(
        {
            "status": "ok",
            "generated_at": NOW.isoformat(),
            "plan": [{"timestamp": NOW.isoformat(), "P_deferrable0": 0}],
            "emhass_schema_version": "1.0",
        }
    )
    coordinator.client.async_optimize.return_value = (LastRun(status="ok"), plan)
    with patch.object(coordinator, "_build", AsyncMock(return_value=(inputs, built))):
        await coordinator._run(ACTION_MPC)
    assert coordinator._water_tank_plan_blocked is None


async def test_tank_telemetry_changes_notify_control_consumers(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    (runtime,) = coordinator.loads.all()
    listener = Mock()
    runtime.add_listener(listener)
    coordinator.loads.async_start()
    try:
        hass.states.async_set(SENSOR, "unavailable")
        await hass.async_block_till_done()
        listener.assert_called_once()
    finally:
        coordinator.loads.async_stop()


async def test_dispatch_detected_telemetry_loss_holds_until_new_solve(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    (runtime,) = coordinator.loads.all()
    hass.states.async_set(SENSOR, "unavailable")
    assert coordinator.water_tank_error(runtime)
    hass.states.async_set(SENSOR, "45")
    assert coordinator.water_tank_error(runtime)


@pytest.mark.parametrize("version", [None, "0.18.7", "0.18.8"])
async def test_local_tank_fault_does_not_arm_forecast_cooldown(
    hass: HomeAssistant, version
) -> None:
    coordinator = _coordinator(hass)
    coordinator.backend_version = version
    coordinator._sent_load_list = False
    hass.states.async_set(SENSOR, "unavailable")
    with pytest.raises(UpdateFailed):
        await coordinator.async_run(ACTION_MPC)
    assert coordinator._no_list_run_failed_at is None
    coordinator.client.async_optimize.assert_not_called()


async def test_period_based_tank_projects_every_period_and_syncs_without_resetting_limits(hass):
    from custom_components.emhass_companion.diagnostics import _loads_section

    await hass.config.async_set_time_zone("Europe/Stockholm")
    periods = [
        {"name": "Morning", "draw_start": "06:00:00", "draw_end": "08:00:00", "draw_energy_kwh": 3},
        {"name": "Evening", "draw_start": "18:00:00", "draw_end": "20:00:00", "draw_energy_kwh": 2},
    ]
    coordinator = _coordinator(hass, hot_water_periods=periods)
    (runtime,) = coordinator.loads.all()
    runtime.comfort_temperature = 46
    (projected,) = coordinator.deferrable_loads(NOW)
    payload = projected.thermal.to_emhass(NOW, timedelta(minutes=30), 48)
    assert sum(payload["draw_off_demand"]) == pytest.approx(5)
    minimums, _ = projected.thermal.comfort_band(NOW, timedelta(minutes=30), 48)
    assert minimums[:4] == [46] * 4
    assert "comfort_start" not in live_settings(runtime)
    entry = coordinator.config_entry
    subentry = entry.subentries[runtime.subentry_id]
    changed = {**subentry.data, "hot_water_periods": periods[:1]}
    hass.config_entries.async_update_subentry(entry, subentry, data=changed)
    coordinator.loads.sync()
    assert runtime.comfort_temperature == 46
    assert len(runtime.thermal_config(45).periods) == 1
    assert _loads_section(coordinator, entry)[0]["tank"]["hot_water_periods"] == periods[:1]


@pytest.mark.parametrize("periods", [None, [], [{}], [False], [{"draw_start": "bad"}]])
async def test_invalid_periods_block_tank_projection(hass, periods):
    coordinator = _coordinator(hass, hot_water_periods=periods)
    (runtime,) = coordinator.loads.all()
    assert coordinator.water_tank_error(runtime) is not None


async def test_a_tank_behind_schedule_gets_a_warning_that_clears(hass: HomeAssistant) -> None:
    from custom_components.emhass_companion.const import ISSUE_TANK_BEHIND_SCHEDULE
    from custom_components.emhass_companion.water_tank import TankShortfall

    coordinator = _coordinator(hass)
    (runtime,) = coordinator.loads.all()
    issue_id = f"{ISSUE_TANK_BEHIND_SCHEDULE}_{runtime.subentry_id}"
    shortfall = TankShortfall(
        moment=datetime(2026, 11, 3, 6, 30, tzinfo=UTC),
        required=45.0,
        achievable=34.25,
        current=30.0,
        power_w=2000.0,
    )

    coordinator._track_tank_behind_issues({runtime.subentry_id: shortfall})

    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == ISSUE_TANK_BEHIND_SCHEDULE
    assert issue.translation_placeholders == {
        "name": "Hot water",
        "current": "30.0",
        "required": "45.0",
        "achievable": "34.2",
        "time": shortfall.moment.strftime("%H:%M"),
        "power": "2.0 kW",
    }

    coordinator._track_tank_behind_issues({})

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_a_deleted_tanks_behind_schedule_warning_is_swept(hass: HomeAssistant) -> None:
    from custom_components.emhass_companion.const import ISSUE_TANK_BEHIND_SCHEDULE

    coordinator = _coordinator(hass)
    orphan = f"{ISSUE_TANK_BEHIND_SCHEDULE}_deleted_tank"
    ir.async_create_issue(
        hass,
        DOMAIN,
        orphan,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_TANK_BEHIND_SCHEDULE,
    )

    coordinator._track_tank_behind_issues({})

    assert ir.async_get(hass).async_get_issue(DOMAIN, orphan) is None

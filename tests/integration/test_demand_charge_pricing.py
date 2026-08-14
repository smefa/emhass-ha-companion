"""Phase 3 of docs/network_tariffs_plan.md: pricing the peak.

``EmhassCoordinator.demand_charge_pricing`` is the single place that decides
whether a network profile's demand charge can be safely priced right now, and
``_build`` is the only consumer that turns that decision into what actually
reaches EMHASS. Both are exercised here against a coordinator built the same
way ``test_deferrable_plan_fallback.py`` does, rather than through a full
config-entry setup -- the version gate and the window-is-all-day gate are pure
decisions over already-resolved configuration, not anything that needs a real
profile or a running meter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import ACTION_DAYAHEAD, ACTION_MPC, DOMAIN
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry
from custom_components.emhass_companion.network_calendar import (
    CapacityLimit,
    DemandCharge,
    DemandMeasure,
    NetworkCalendar,
    Window,
)

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _coordinator(hass: HomeAssistant) -> EmhassCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://localhost:5000"})
    entry.add_to_hass(hass)
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    return EmhassCoordinator(hass, entry, AsyncMock(spec=EmhassClient), loads)


def _demand(*, window: Window | None, rate_per_kw: float | None = 135.0) -> DemandCharge:
    return DemandCharge(
        window=window,
        measure=DemandMeasure(
            interval=timedelta(minutes=60), aggregate="mean_top_n", n=3, distinct_days=True
        ),
        period="month",
        rate_per_kw=rate_per_kw,
        rate_basis="month",
    )


class _StubTracker:
    def __init__(self, floor_kw: float) -> None:
        self.floor_kw = floor_kw


# --- demand_charge_pricing -----------------------------------------------------


async def test_none_with_no_network_profile_at_all(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    assert coordinator.demand_charge_pricing(NOW) is None


async def test_none_with_a_network_profile_that_has_no_demand_charge(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.network_calendar = NetworkCalendar()
    assert coordinator.demand_charge_pricing(NOW) is None


async def test_unconfigured_rate_gives_a_reason_and_no_price(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.network_calendar = NetworkCalendar(
        demand_charge=_demand(window=None, rate_per_kw=None)
    )
    coordinator.backend_version = "0.19.0"
    pricing = coordinator.demand_charge_pricing(NOW)
    assert pricing is not None
    assert pricing.effective_rate_per_kw is None
    assert "rate configured" in pricing.reason


async def test_old_backend_gives_a_reason_and_no_price(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.network_calendar = NetworkCalendar(demand_charge=_demand(window=None))
    coordinator.backend_version = "0.17.9"
    pricing = coordinator.demand_charge_pricing(NOW)
    assert pricing.effective_rate_per_kw is None
    assert "0.18.0" in pricing.reason


async def test_unknown_backend_version_is_treated_as_unsupported(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.network_calendar = NetworkCalendar(demand_charge=_demand(window=None))
    coordinator.backend_version = None
    pricing = coordinator.demand_charge_pricing(NOW)
    assert pricing.effective_rate_per_kw is None


async def test_windowed_demand_charge_gives_a_reason_on_a_backend_with_no_mask(
    hass: HomeAssistant,
) -> None:
    """Göteborg's own shape: a real window, no capacity_charge_window support
    yet -- pricing it unmasked would over-shave every hour outside it."""
    coordinator = _coordinator(hass)
    window = Window.from_dict({"hours": "07:00-20:00"}, {})
    coordinator.network_calendar = NetworkCalendar(demand_charge=_demand(window=window))
    coordinator.backend_version = "0.18.0"
    pricing = coordinator.demand_charge_pricing(NOW)
    assert pricing.effective_rate_per_kw is None
    assert "window" in pricing.reason


async def test_all_day_window_prices_on_a_supported_backend(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.network_calendar = NetworkCalendar(demand_charge=_demand(window=None))
    coordinator.backend_version = "0.18.0"
    pricing = coordinator.demand_charge_pricing(NOW)
    # rate_basis=month, aggregate=mean_top_n, n=3: 135 / 3 = 45, independent
    # of days_in_period -- see test_peaks.py for that arithmetic worked by hand.
    assert pricing.effective_rate_per_kw == 45.0
    assert pricing.reason is None


# --- wired into _build() --------------------------------------------------------


async def test_build_sends_the_priced_peak_and_the_floor_on_mpc(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.network_calendar = NetworkCalendar(demand_charge=_demand(window=None))
    coordinator.backend_version = "0.18.0"
    coordinator.peak_tracker = _StubTracker(floor_kw=3.2)

    inputs, built = await coordinator._build(ACTION_MPC)

    assert inputs.demand_charge_rate_per_kw == 45.0
    assert inputs.current_period_peak_w == 3200.0
    assert built.payload["capacity_cost_per_kw"] == 45.0
    assert built.payload["current_period_peak"] == 3200


async def test_build_zeroes_the_priced_peak_on_dayahead(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.network_calendar = NetworkCalendar(demand_charge=_demand(window=None))
    coordinator.backend_version = "0.18.0"
    coordinator.peak_tracker = _StubTracker(floor_kw=3.2)

    inputs, built = await coordinator._build(ACTION_DAYAHEAD)

    # current_period_peak is MPC-only in EMHASS; capacity_cost_per_kw is
    # forced to 0 on day-ahead by build_payload's own gating on the action.
    assert inputs.current_period_peak_w is None
    assert built.payload["capacity_cost_per_kw"] == 0.0
    assert "current_period_peak" not in built.payload


async def test_build_zeroes_rather_than_pricing_when_not_yet_supported(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.network_calendar = NetworkCalendar(demand_charge=_demand(window=None))
    coordinator.backend_version = "0.17.9"

    inputs, built = await coordinator._build(ACTION_MPC)

    assert inputs.demand_charge_rate_per_kw is None
    assert built.payload["capacity_cost_per_kw"] == 0.0


# --- the windowed hard cap (mechanism 3) --------------------------------------


async def test_build_sends_the_fallback_ceiling_when_the_window_cannot_be_priced(
    hass: HomeAssistant,
) -> None:
    """Göteborg's own shape on a v0.18.0 backend: no window mask, so the
    priced peak is refused (see test_windowed_demand_charge_gives_a_reason_...
    above). The array ceiling is what actually protects the window instead --
    the v0.18.0 fallback docs/network_tariffs_plan.md's "Version gating"
    describes."""
    coordinator = _coordinator(hass)
    window = Window.from_dict({"hours": "07:00-20:00"}, {})
    coordinator.network_calendar = NetworkCalendar(demand_charge=_demand(window=window))
    coordinator.backend_version = "0.18.0"
    coordinator.peak_target_kw = 3.2

    inputs, built = await coordinator._build(ACTION_MPC)

    assert inputs.demand_charge_rate_per_kw is None  # confirmed unpriceable
    assert inputs.demand_fallback_ceiling_w == 3200.0
    assert built.payload["capacity_cost_per_kw"] == 0.0
    assert isinstance(built.payload["maximum_power_from_grid"], list)


async def test_fallback_ceiling_is_not_sent_without_a_peak_target(hass: HomeAssistant) -> None:
    """No number.PeakTargetNumber reading yet (platform not set up, or the
    entity has never been added) -- nothing to fall back to."""
    coordinator = _coordinator(hass)
    window = Window.from_dict({"hours": "07:00-20:00"}, {})
    coordinator.network_calendar = NetworkCalendar(demand_charge=_demand(window=window))
    coordinator.backend_version = "0.18.0"

    inputs, built = await coordinator._build(ACTION_MPC)

    assert inputs.demand_fallback_ceiling_w is None
    assert isinstance(built.payload["maximum_power_from_grid"], int)


async def test_fallback_ceiling_does_not_apply_below_the_min_emhass_version(
    hass: HomeAssistant,
) -> None:
    """The array form of maximum_power_from_grid is only verified from the
    same v0.18.0 that current_period_peak needs -- an older backend either
    rejects it or silently collapses a wrong-length list (see
    _prepare_power_limit_array), so nothing is sent below that version."""
    coordinator = _coordinator(hass)
    window = Window.from_dict({"hours": "07:00-20:00"}, {})
    coordinator.network_calendar = NetworkCalendar(demand_charge=_demand(window=window))
    coordinator.backend_version = "0.17.9"
    coordinator.peak_target_kw = 3.2

    inputs, built = await coordinator._build(ACTION_MPC)

    assert inputs.demand_fallback_ceiling_w is None
    assert isinstance(built.payload["maximum_power_from_grid"], int)


async def test_fallback_ceiling_is_inert_once_the_peak_is_priced(hass: HomeAssistant) -> None:
    """An all-day window prices cleanly (see test_all_day_window_prices_on_a_
    supported_backend above); the fallback ceiling must not also kick in."""
    coordinator = _coordinator(hass)
    coordinator.network_calendar = NetworkCalendar(demand_charge=_demand(window=None))
    coordinator.backend_version = "0.18.0"
    coordinator.peak_target_kw = 3.2

    inputs, built = await coordinator._build(ACTION_MPC)

    assert inputs.demand_charge_rate_per_kw == 45.0
    assert isinstance(built.payload["maximum_power_from_grid"], int)


async def test_build_sends_an_explicit_capacity_limit_array(hass: HomeAssistant) -> None:
    """A subscribed-tier ceiling applies independently of whether the demand
    charge itself is configured or priceable."""
    coordinator = _coordinator(hass)
    window = Window.from_dict({"hours": "15:00-20:00"}, {})
    coordinator.network_calendar = NetworkCalendar(
        capacity_limit=CapacityLimit(subscribed_kw=6.0, window=window, headroom_kw=0.5)
    )
    coordinator.backend_version = "0.18.0"

    inputs, built = await coordinator._build(ACTION_MPC)

    assert inputs.capacity_limit_w == 5500.0
    assert isinstance(built.payload["maximum_power_from_grid"], list)


async def test_capacity_limit_does_not_apply_below_the_min_emhass_version(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    window = Window.from_dict({"hours": "15:00-20:00"}, {})
    coordinator.network_calendar = NetworkCalendar(
        capacity_limit=CapacityLimit(subscribed_kw=6.0, window=window, headroom_kw=0.5)
    )
    coordinator.backend_version = "0.17.9"

    inputs, built = await coordinator._build(ACTION_MPC)

    assert inputs.capacity_limit_w is None
    assert isinstance(built.payload["maximum_power_from_grid"], int)

"""Tests for the runtime-parameter payload builder.

These cover the specific conventions that are easy to get wrong and produce a
plausible-looking but wrong plan rather than an error.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from homeassistant.util import dt as dt_util
import pytest

from custom_components.emhass_companion.const import ACTION_DAYAHEAD, ACTION_MPC
from custom_components.emhass_companion.models import (
    BatteryConfig,
    DeferrableLoad,
    DeferrableLoadGroup,
    GridConfig,
    HybridInverterConfig,
    Point,
    Series,
)
from custom_components.emhass_companion.payload import (
    PayloadInputs,
    build_payload,
    in_window,
    operating_timesteps,
    resolve_grid_limit,
    resolve_load_window,
    window_to_timesteps,
)
from custom_components.emhass_companion.thermal import ThermalConfig

HALF_HOUR = timedelta(minutes=30)
DAY_STEPS = 48  # 24 hours at 30-minute resolution


def _local(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=dt_util.DEFAULT_TIME_ZONE)


# --- window membership -------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "earliest", "latest", "expected"),
    [
        (time(12, 0), time(8, 0), time(18, 0), True),
        (time(7, 0), time(8, 0), time(18, 0), False),
        (time(18, 0), time(8, 0), time(18, 0), False),  # end is exclusive
        # Overnight windows are the case a naive comparison gets wrong.
        (time(23, 0), time(22, 0), time(6, 0), True),
        (time(2, 0), time(22, 0), time(6, 0), True),
        (time(12, 0), time(22, 0), time(6, 0), False),
        (time(6, 0), time(22, 0), time(6, 0), False),
        (time(12, 0), None, None, True),
        (time(12, 0), time(8, 0), None, True),
        (time(4, 0), None, time(6, 0), True),
    ],
)
def test_in_window(moment, earliest, latest, expected):
    assert in_window(moment, earliest, latest) is expected


# --- wall clock to timestep --------------------------------------------------


@pytest.mark.usefixtures("stockholm_timezone")
def test_no_window_is_unconstrained():
    assert window_to_timesteps(None, None, _local(2026, 7, 28, 12), HALF_HOUR, DAY_STEPS) == (0, 0)


@pytest.mark.usefixtures("stockholm_timezone")
def test_future_window_same_day():
    # 12:00 now, window 18:00-20:00 -> opens in 6h (12 steps), closes in 8h (16).
    assert window_to_timesteps(
        time(18, 0), time(20, 0), _local(2026, 7, 28, 12), HALF_HOUR, DAY_STEPS
    ) == (12, 16)


@pytest.mark.usefixtures("stockholm_timezone")
def test_overnight_window_already_open_may_start_now():
    # 23:00 inside a 22:00-06:00 window: the load may run immediately, and the
    # window closes 7 hours out. Pushing the start to tomorrow's 22:00 would
    # waste the cheap overnight hours entirely.
    assert window_to_timesteps(
        time(22, 0), time(6, 0), _local(2026, 7, 28, 23), HALF_HOUR, DAY_STEPS
    ) == (0, 14)


@pytest.mark.usefixtures("stockholm_timezone")
def test_overnight_window_not_yet_open():
    # 20:00, window 22:00-06:00 -> opens in 2h (4 steps), closes in 10h (20).
    assert window_to_timesteps(
        time(22, 0), time(6, 0), _local(2026, 7, 28, 20), HALF_HOUR, DAY_STEPS
    ) == (4, 20)


@pytest.mark.usefixtures("stockholm_timezone")
def test_window_end_beyond_horizon_is_unconstrained():
    # A 6-step horizon cannot reach 20:00, so the end constraint is dropped
    # rather than clamped to something EMHASS would misread.
    _, end = window_to_timesteps(time(18, 0), time(20, 0), _local(2026, 7, 28, 12), HALF_HOUR, 6)
    assert end == 0


@pytest.mark.usefixtures("stockholm_timezone")
def test_window_opening_beyond_horizon_is_dropped():
    assert window_to_timesteps(time(18, 0), time(20, 0), _local(2026, 7, 28, 12), HALF_HOUR, 4) == (
        0,
        0,
    )


@pytest.mark.usefixtures("stockholm_timezone")
def test_absolute_window_alone_becomes_indices():
    """A surplus load's derived window: absolute instants, no wall-clock pair."""
    start = _local(2026, 7, 28, 12)
    window = resolve_load_window(
        name="Pool",
        earliest=None,
        latest=None,
        deadline=None,
        grid_start=start,
        step=HALF_HOUR,
        horizon_steps=DAY_STEPS,
        start_at=start + timedelta(hours=2),
        end_at=start + timedelta(hours=6),
    )
    assert (window.start_index, window.end_index) == (4, 12)


@pytest.mark.usefixtures("stockholm_timezone")
def test_absolute_window_narrows_a_standing_one_but_never_widens_it():
    """Both are hard constraints, so only their intersection is safe.

    A surplus window running past a household's quiet-hours boundary must not
    silently extend it, and a quiet-hours window must not hand a surplus load
    timesteps the sun was never going to fill.
    """
    start = _local(2026, 7, 28, 6)
    window = resolve_load_window(
        name="Pool",
        earliest=time(10, 0),
        latest=time(18, 0),
        deadline=None,
        grid_start=start,
        step=HALF_HOUR,
        horizon_steps=DAY_STEPS,
        # Opens earlier and closes later than the standing window: neither end
        # should move.
        start_at=start + timedelta(hours=1),
        end_at=start + timedelta(hours=16),
    )
    assert (window.start_index, window.end_index) == (8, 24)


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_window_opening_moments_after_now_is_not_pushed_a_whole_step_out():
    """``grid_start`` is a literal run instant, almost never exactly on the
    step grid a derived window's absolute instants sit on. Ceiling any
    fractional remainder up used to discard nearly all of an already-open
    step whenever a run merely happened to launch a few seconds early --
    every run, not just an unlucky one.
    """
    grid_start = _local(2026, 7, 28, 12) - timedelta(seconds=5)
    window = resolve_load_window(
        name="Pool",
        earliest=None,
        latest=None,
        deadline=None,
        grid_start=grid_start,
        step=HALF_HOUR,
        horizon_steps=DAY_STEPS,
        start_at=grid_start + timedelta(seconds=5),  # opens at the clean half hour
        end_at=grid_start + timedelta(hours=4),
    )
    assert window.start_index == 0


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_window_opening_genuinely_mid_step_still_waits_for_the_next_one():
    """The other side of the same fix: rounding, not always-floor either --
    an opening past the halfway point of a step is still closer to the next
    one, and letting EMHASS treat that step as open would mean scheduling
    into a mostly-not-yet-open one."""
    start = _local(2026, 7, 28, 12)
    window = resolve_load_window(
        name="Pool",
        earliest=None,
        latest=None,
        deadline=None,
        grid_start=start,
        step=HALF_HOUR,
        horizon_steps=DAY_STEPS,
        start_at=start + timedelta(minutes=20),  # past the halfway point of a 30 min step
        end_at=start + timedelta(hours=4),
    )
    assert window.start_index == 1


@pytest.mark.usefixtures("stockholm_timezone")
def test_absolute_window_in_the_past_means_may_start_now():
    """The surplus window is derived from a plan that starts before "now"."""
    start = _local(2026, 7, 28, 12)
    window = resolve_load_window(
        name="Pool",
        earliest=None,
        latest=None,
        deadline=None,
        grid_start=start,
        step=HALF_HOUR,
        horizon_steps=DAY_STEPS,
        start_at=start - timedelta(hours=3),
        end_at=start + timedelta(hours=2),
    )
    assert (window.start_index, window.end_index) == (0, 4)


@pytest.mark.usefixtures("stockholm_timezone")
def test_indices_are_relative_to_launch_time():
    """The same wall-clock window yields different indices as the day passes.

    This is the whole reason the conversion is redone on every request instead
    of being computed once and stored.
    """
    early = window_to_timesteps(
        time(18, 0), time(20, 0), _local(2026, 7, 28, 10), HALF_HOUR, DAY_STEPS
    )
    later = window_to_timesteps(
        time(18, 0), time(20, 0), _local(2026, 7, 28, 14), HALF_HOUR, DAY_STEPS
    )
    assert early == (16, 20)
    assert later == (8, 12)


@pytest.mark.usefixtures("stockholm_timezone")
def test_window_across_dst_end_uses_wall_clock():
    """Europe/Stockholm gains an hour on 2026-10-25; 22:00->06:00 spans it.

    Users mean wall-clock times, so the window must still end at 06:00 local --
    which is 9 elapsed hours (18 half-hour steps) that night, not 8.
    """
    start, end = window_to_timesteps(
        time(22, 0), time(6, 0), _local(2026, 10, 24, 22), HALF_HOUR, DAY_STEPS
    )
    assert start == 0
    assert end == 18


# --- full payload ------------------------------------------------------------


def _series(start: datetime, hours: int, value: float) -> Series:
    return Series(Point(start + timedelta(minutes=30 * index), value) for index in range(hours * 2))


def _inputs(**overrides) -> PayloadInputs:
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    defaults = {
        "action": ACTION_MPC,
        "now": now,
        "time_step_minutes": 30,
        "horizon_steps": DAY_STEPS,
        "battery": BatteryConfig(),
        "grid": GridConfig(),
        "hybrid_inverter": HybridInverterConfig(),
    }
    return PayloadInputs(**{**defaults, **overrides})


def test_mpc_sends_prediction_horizon_in_timesteps():
    payload = build_payload(_inputs()).payload
    assert payload["prediction_horizon"] == DAY_STEPS
    assert payload["optimization_time_step"] == 30
    assert "delta_forecast_daily" not in payload


def test_dayahead_sends_delta_forecast_in_days():
    payload = build_payload(_inputs(action=ACTION_DAYAHEAD)).payload
    assert payload["delta_forecast_daily"] == 1
    assert "prediction_horizon" not in payload


def test_cost_fun_defaults_to_profit():
    assert build_payload(_inputs()).payload["costfun"] == "profit"


def test_cost_fun_is_sent_verbatim():
    payload = build_payload(_inputs(cost_fun="self-consumption")).payload
    assert payload["costfun"] == "self-consumption"


def test_forecasts_are_sent_as_timestamp_maps_with_explicit_offsets():
    """Never bare lists: a mapping is resampled and aligned by EMHASS itself."""
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    result = build_payload(_inputs(pv=_series(now, 24, 5000.0)))

    forecast = result.payload["pv_power_forecast"]
    assert isinstance(forecast, dict)
    key = next(iter(forecast))
    assert key == "2026-07-28T10:00:00+00:00"
    assert forecast[key] == 5000.0
    # The forecast method is deliberately not set; EMHASS switches it to "list"
    # by itself when a forecast key is present.
    assert "weather_forecast_method" not in result.payload


def test_mpc_blends_live_pv_into_the_current_forecast_step_only():
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    result = build_payload(_inputs(pv=_series(now, 24, 5000.0), pv_live_w=7000.0))

    forecast = result.payload["pv_power_forecast"]
    values = list(forecast.values())
    assert values[0] == 6000.0  # 0.5 * 5000 + 0.5 * 7000, the default mix_beta
    assert values[1] == 5000.0


def test_mpc_blends_live_load_into_the_current_forecast_step_only():
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    result = build_payload(_inputs(load=_series(now, 24, 1000.0), load_live_w=2000.0))

    forecast = result.payload["load_power_forecast"]
    values = list(forecast.values())
    assert values[0] == 1500.0  # 0.5 * 1000 + 0.5 * 2000, the default mix_beta
    assert values[1] == 1000.0


def test_mpc_blends_live_pv_into_now_even_when_the_series_starts_earlier():
    """Regression test for a real bug: a PV forecast fetched from a live
    profile commonly starts well before "now" (e.g. Solcast's "today" series
    starts at local midnight). Blending must correct the step covering
    "now", not the series' chronologically-first step, or the live reading
    corrects a timestep the MPC solver never looks at again.
    """
    series_start = datetime(2026, 7, 28, 0, 0, tzinfo=UTC)
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    result = build_payload(_inputs(now=now, pv=_series(series_start, 24, 5000.0), pv_live_w=7000.0))

    forecast = result.payload["pv_power_forecast"]
    assert forecast[series_start.isoformat()] == 5000.0  # untouched: it's in the past
    assert forecast[now.isoformat()] == 6000.0  # 0.5 * 5000 + 0.5 * 7000


def test_power_forecasts_are_sent_as_whole_watts():
    """Profile math and the live blend both leave long float tails like
    1575.2129175703624 W. Sub-watt precision means nothing in a forecast and
    only bloats the request, so both power series go out rounded -- including
    the step the live value was blended into."""
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    result = build_payload(
        _inputs(
            now=now,
            pv=_series(now, 24, 5000.4),
            pv_live_w=7000.9,
            load=_series(now, 24, 1575.2129175703624),
            load_live_w=1200.3,
            mix_beta=0.5,
        )
    )
    for key in ("pv_power_forecast", "load_power_forecast"):
        assert all(isinstance(value, int) for value in result.payload[key].values())
    assert result.payload["pv_power_forecast"][now.isoformat()] == 6001  # 0.5*5000.4+0.5*7000.9
    assert result.payload["load_power_forecast"][now.isoformat()] == 1388  # 0.5*1575.2+0.5*1200.3


def test_mpc_without_a_live_value_leaves_the_forecast_untouched():
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    result = build_payload(_inputs(pv=_series(now, 24, 5000.0)))
    assert next(iter(result.payload["pv_power_forecast"].values())) == 5000.0


def test_dayahead_never_blends_even_with_a_live_value_set():
    """A day-ahead forecast starts tomorrow -- there is no "now" to blend in."""
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    result = build_payload(
        _inputs(
            action=ACTION_DAYAHEAD,
            pv=_series(now, 24, 5000.0),
            pv_live_w=7000.0,
        )
    )
    assert next(iter(result.payload["pv_power_forecast"].values())) == 5000.0


def test_mix_beta_controls_how_much_the_live_value_wins():
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    result = build_payload(_inputs(pv=_series(now, 24, 5000.0), pv_live_w=7000.0, mix_beta=1.0))
    assert next(iter(result.payload["pv_power_forecast"].values())) == 7000.0


def test_mpc_sends_alpha_beta_matching_mix_beta():
    """EMHASS applies its own live-value correction to the load forecast
    automatically on every naive-mpc-optim run, for any load profile that
    doesn't hand EMHASS an explicit list (i.e. every built-in profile except
    "Load forecast from an entity" and "Native EMHASS forecaster"). Left
    unset, alpha/beta default to a hard-coded 50/50 split inside EMHASS,
    completely disconnected from the mix_beta number entity -- so these must
    be sent explicitly, matching mix_beta, or the slider does nothing for
    that profile.
    """
    result = build_payload(_inputs(mix_beta=0.8))
    assert result.payload["alpha"] == pytest.approx(0.2)
    assert result.payload["beta"] == pytest.approx(0.8)


def test_dayahead_does_not_send_alpha_beta():
    """EMHASS never applies this correction outside naive-mpc-optim, so a
    day-ahead request has nothing to pin.
    """
    result = build_payload(_inputs(action=ACTION_DAYAHEAD, mix_beta=0.8))
    assert "alpha" not in result.payload
    assert "beta" not in result.payload


def test_short_forecast_produces_a_warning():
    """A short series is silently forward-filled by EMHASS, so we must warn.

    Nothing downstream errors: the plan comes back looking entirely healthy,
    built on a price that flat-lined at its last known value.
    """
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    result = build_payload(_inputs(buy_price=_series(now, 6, 1.5)))

    assert len(result.warnings) == 1
    assert "Buy price" in result.warnings[0]


def test_full_length_forecast_produces_no_warning():
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    result = build_payload(_inputs(buy_price=_series(now, 25, 1.5)))
    assert result.warnings == []


def test_a_forecast_reaching_the_last_timestep_is_not_called_short():
    """``now`` is a raw utcnow(), so the horizon it defines is never on the grid.

    Every forecast series is, which used to make the coverage check fail by
    the seconds ``now`` happened to carry -- reporting "only covers until
    10:00, short of the 10:00 horizon" on every single run, in the same words
    used for a price source that stops tonight. The series here ends exactly
    on the last timestep boundary inside the horizon, which is as far as any
    grid-aligned source can ever reach.
    """
    now = datetime(2026, 7, 28, 10, 0, 37, 123456, tzinfo=UTC)
    step = timedelta(minutes=30)
    last = datetime(2026, 7, 28, 10, 0, tzinfo=UTC) + step * DAY_STEPS
    prices = Series(
        Point(datetime(2026, 7, 28, 10, 0, tzinfo=UTC) + step * index, 1.5)
        for index in range(DAY_STEPS + 1)
    )
    assert prices.end == last

    result = build_payload(_inputs(now=now, buy_price=prices, sell_price=prices))
    assert result.warnings == []


def test_a_genuinely_short_forecast_still_warns_from_an_off_grid_now():
    """Tolerating the final partial step must not swallow a real shortfall.

    A day-ahead price source that stops at midnight is short by hours, not by
    the fraction of a second above -- and that is the case the warning exists
    for.
    """
    now = datetime(2026, 7, 28, 10, 0, 37, 123456, tzinfo=UTC)
    result = build_payload(_inputs(now=now, buy_price=_series(now, 6, 1.5)))

    assert len(result.warnings) == 1
    assert "Buy price" in result.warnings[0]


def test_an_hourly_price_is_extended_onto_the_payloads_own_grid():
    """An hourly source under a 30-minute step would otherwise land short.

    ``extended_with_previous_day`` fills at the series' own spacing unless
    told otherwise, so an hourly price stops on the hour -- up to one whole
    price interval before a horizon that ends on a half hour, which read as a
    coverage failure for a source that had in fact been carried the whole way.
    """
    now = datetime(2026, 7, 28, 10, 30, 11, tzinfo=UTC)
    start = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)
    # Yesterday plus today, hourly, ending at tonight's midnight -- Nord Pool
    # before its afternoon publication, with a day of history to repeat.
    hourly = Series(Point(start + timedelta(hours=index), 1.0 + index % 24) for index in range(48))

    result = build_payload(_inputs(now=now, buy_price=hourly))

    assert len(result.warnings) == 1
    assert "filled in by repeating the previous day" in result.warnings[0]
    assert not any("short of the" in warning for warning in result.warnings)


def test_battery_disabled_sends_only_the_flag():
    payload = build_payload(_inputs()).payload
    assert payload["set_use_battery"] is False
    assert "battery_nominal_energy_capacity" not in payload


def test_battery_enabled_sends_every_limit():
    battery = BatteryConfig(
        enabled=True,
        capacity_wh=25600,
        charge_power_max_w=10600,
        discharge_power_max_w=10600,
        soc_min=0.05,
        soc_max=0.95,
    )
    payload = build_payload(_inputs(battery=battery, soc_init=0.098)).payload
    assert payload["set_use_battery"] is True
    assert payload["battery_nominal_energy_capacity"] == 25600
    assert payload["battery_minimum_state_of_charge"] == 0.05
    # A fraction, matching EMHASS -- not the percentage Home Assistant reports.
    assert payload["soc_init"] == 0.098
    # Sent explicitly so the horizon's net-throughput pin targets the current
    # SOC rather than relying on EMHASS's own soc_init fallback.
    assert payload["soc_final"] == 0.098


def test_battery_export_defaults_to_allowed():
    """EMHASS's own default for set_nodischarge_to_grid is True, which quietly
    blocks the battery from ever selling. An untouched battery must override
    that so export works unless someone turns it off deliberately."""
    battery = BatteryConfig(enabled=True, capacity_wh=25600)
    payload = build_payload(_inputs(battery=battery, soc_init=0.098)).payload
    assert payload["set_nodischarge_to_grid"] is False


def test_battery_export_block_rides_through_to_emhass():
    battery = BatteryConfig(enabled=True, capacity_wh=25600, no_discharge_to_grid=True)
    payload = build_payload(_inputs(battery=battery, soc_init=0.098)).payload
    assert payload["set_nodischarge_to_grid"] is True


def test_battery_new_grid_flags_default_off():
    battery = BatteryConfig(enabled=True, capacity_wh=25600)
    payload = build_payload(_inputs(battery=battery, soc_init=0.098)).payload
    assert payload["set_nocharge_from_grid"] is False
    assert payload["set_battery_first_priority"] is False
    assert payload["set_battery_dynamic"] is False
    assert payload["battery_dynamic_max"] == 0.9
    assert payload["battery_dynamic_min"] == -0.9


def test_battery_new_grid_flags_ride_through_to_emhass():
    battery = BatteryConfig(
        enabled=True,
        capacity_wh=25600,
        no_charge_from_grid=True,
        battery_first_priority=True,
        dynamic_enabled=True,
        dynamic_max=0.5,
        dynamic_min=-0.3,
    )
    payload = build_payload(_inputs(battery=battery, soc_init=0.098)).payload
    assert payload["set_nocharge_from_grid"] is True
    assert payload["set_battery_first_priority"] is True
    assert payload["set_battery_dynamic"] is True
    assert payload["battery_dynamic_max"] == 0.5
    assert payload["battery_dynamic_min"] == -0.3


def test_battery_cycle_costs_default_to_priced_discharge():
    """The shipped discharge weight has to reach EMHASS, or a round trip is
    planned as if the wear it causes were free."""
    battery = BatteryConfig(enabled=True, capacity_wh=25600)
    payload = build_payload(_inputs(battery=battery, soc_init=0.098)).payload
    assert payload["weight_battery_discharge"] == 0.02
    assert payload["weight_battery_charge"] == 0.0


def test_battery_cycle_costs_ride_through_to_emhass():
    battery = BatteryConfig(
        enabled=True,
        capacity_wh=25600,
        weight_battery_discharge=0.06,
        weight_battery_charge=0.01,
    )
    payload = build_payload(_inputs(battery=battery, soc_init=0.098)).payload
    assert payload["weight_battery_discharge"] == 0.06
    assert payload["weight_battery_charge"] == 0.01


def test_battery_soc_and_stress_costs_default_to_the_shipped_values():
    """The deficit pair is live at its default and must arrive as a real
    constraint; the surplus pair and stress cost stay inert (cost 0 means
    EMHASS never builds their constraints at all)."""
    battery = BatteryConfig(enabled=True, capacity_wh=25600)
    payload = build_payload(_inputs(battery=battery, soc_init=0.5)).payload
    assert payload["battery_soc_deficit_threshold"] == 0.10
    assert payload["battery_soc_deficit_cost"] == 0.05
    assert payload["battery_soc_surplus_threshold"] == 0.90
    assert payload["battery_soc_surplus_cost"] == 0.0
    assert payload["battery_stress_cost"] == 0.0
    assert payload["battery_stress_segments"] == 10


def test_battery_soc_and_stress_costs_ride_through_to_emhass():
    battery = BatteryConfig(
        enabled=True,
        capacity_wh=25600,
        soc_deficit_threshold=0.30,
        soc_deficit_cost=0.02,
        soc_surplus_threshold=0.85,
        soc_surplus_cost=0.03,
        stress_cost=0.05,
        stress_segments=16,
    )
    payload = build_payload(_inputs(battery=battery, soc_init=0.5)).payload
    assert payload["battery_soc_deficit_threshold"] == 0.30
    assert payload["battery_soc_deficit_cost"] == 0.02
    assert payload["battery_soc_surplus_threshold"] == 0.85
    assert payload["battery_soc_surplus_cost"] == 0.03
    assert payload["battery_stress_cost"] == 0.05
    assert payload["battery_stress_segments"] == 16


def test_stress_segments_stays_an_int():
    """EMHASS slices PWL segments with it, so a float would be a type error."""
    battery = BatteryConfig(enabled=True, capacity_wh=25600, stress_cost=0.05)
    payload = build_payload(_inputs(battery=battery, soc_init=0.5)).payload
    assert isinstance(payload["battery_stress_segments"], int)


def test_capacity_charge_is_sent_even_without_a_battery():
    """It prices peak *grid* import, which deferrable loads can shave on their
    own -- so it must not be gated behind set_use_battery the way the battery
    cost knobs are."""
    grid = GridConfig(capacity_cost_per_kw=45.0)
    payload = build_payload(_inputs(grid=grid)).payload
    assert payload["set_use_battery"] is False
    assert payload["capacity_cost_per_kw"] == 45.0


def test_capacity_charge_defaults_to_a_no_op():
    payload = build_payload(_inputs()).payload
    assert payload["capacity_cost_per_kw"] == 0.0


def test_network_demand_charge_prices_the_peak_on_mpc():
    """A network profile's effective rate owns the field entirely -- the
    unrelated manual number on GridConfig must not leak through underneath
    it (see network_tariffs_plan.md's "Band adders applied twice" guardrail,
    the same doctrine applied to this field instead)."""
    grid = GridConfig(capacity_cost_per_kw=999.0)
    payload = build_payload(
        _inputs(
            grid=grid,
            network_demand_charge_configured=True,
            demand_charge_rate_per_kw=45.0,
            current_period_peak_w=3200.0,
        )
    ).payload
    assert payload["capacity_cost_per_kw"] == 45.0
    assert payload["current_period_peak"] == 3200


def test_network_demand_charge_is_zeroed_on_dayahead():
    """current_period_peak and the (not-yet-sent) window mask are MPC-only in
    EMHASS, so a day-ahead run would otherwise price the whole horizon's peak
    with neither -- the exact distortion the feature exists to remove."""
    payload = build_payload(
        _inputs(
            action=ACTION_DAYAHEAD,
            network_demand_charge_configured=True,
            demand_charge_rate_per_kw=45.0,
            current_period_peak_w=3200.0,
        )
    ).payload
    assert payload["capacity_cost_per_kw"] == 0.0
    assert "current_period_peak" not in payload


def test_network_demand_charge_zeroed_rather_than_falling_back_when_not_priceable():
    """`demand_charge_rate_per_kw=None` with the flag still set is the
    coordinator's "configured, but not safely priceable right now" case
    (backend too old, or a real window on a backend with no window mask) --
    it must zero the field, not fall back to the manual number."""
    grid = GridConfig(capacity_cost_per_kw=999.0)
    payload = build_payload(
        _inputs(grid=grid, network_demand_charge_configured=True, demand_charge_rate_per_kw=None)
    ).payload
    assert payload["capacity_cost_per_kw"] == 0.0


def test_current_period_peak_omitted_without_a_tracker_reading():
    payload = build_payload(
        _inputs(network_demand_charge_configured=True, demand_charge_rate_per_kw=45.0)
    ).payload
    assert payload["capacity_cost_per_kw"] == 45.0
    assert "current_period_peak" not in payload


def test_compute_curtailment_is_sent_when_configured():
    """EMHASS produces no P_PV_curtailment column without it, so the plan-driven
    branch of decide_curtailment depends on this reaching the run."""
    grid = GridConfig(compute_curtailment=True)
    assert build_payload(_inputs(grid=grid)).payload["compute_curtailment"] is True

    grid = GridConfig(compute_curtailment=False)
    assert build_payload(_inputs(grid=grid)).payload["compute_curtailment"] is False


def test_compute_curtailment_is_omitted_when_unset():
    """Sending False here would override an add-on-side setting the user never
    asked us to touch -- an unset entry must stay silent instead."""
    assert "compute_curtailment" not in build_payload(_inputs()).payload


# --- live grid limits ---------------------------------------------------------


def test_a_live_grid_limit_may_lower_the_configured_one():
    assert resolve_grid_limit(6800.0, 11000.0) == 6800.0


def test_a_live_grid_limit_may_never_raise_the_configured_one():
    """The static number is the connection's physical rating. A sensor built on
    the wrong fuse size may make the plan cautious; it must not be able to plan
    through a fuse."""
    assert resolve_grid_limit(20000.0, 11000.0) == 11000.0


def test_no_live_reading_leaves_the_configured_limit_alone():
    assert resolve_grid_limit(None, 11000.0, floor_w=3000.0) == 11000.0


def test_a_live_grid_limit_is_floored_at_what_must_be_served():
    """Below the load that flows regardless, EMHASS answers "infeasible"
    instead of answering with a smaller plan."""
    assert resolve_grid_limit(200.0, 11000.0, floor_w=3000.0) == 3000.0


def test_the_floor_never_beats_the_ceiling():
    """A house forecast above the connection's rating is a separate problem;
    it must not raise the limit past what the connection can do."""
    assert resolve_grid_limit(500.0, 4000.0, floor_w=9000.0) == 4000.0


def test_grid_limits_are_sent_as_whole_watts():
    """EMHASS keeps these as integers in its own config; a float only shows up
    as noise in its log. Rounded down, since the number is a fuse rating."""
    payload = build_payload(_inputs(grid_import_limit_w=6800.4, grid_export_limit_w=1500.9)).payload
    assert payload["maximum_power_from_grid"] == 6800
    assert payload["maximum_power_to_grid"] == 1500
    assert isinstance(payload["maximum_power_from_grid"], int)
    assert isinstance(payload["maximum_power_to_grid"], int)


def test_a_live_import_limit_reaches_the_payload():
    payload = build_payload(_inputs(grid_import_limit_w=6800.0)).payload
    assert payload["maximum_power_from_grid"] == 6800.0


def test_a_live_export_limit_reaches_the_payload():
    payload = build_payload(_inputs(grid_export_limit_w=1500.0)).payload
    assert payload["maximum_power_to_grid"] == 1500.0


def test_grid_limits_fall_back_to_the_configured_numbers():
    grid = GridConfig(import_max_w=11000.0, export_max_w=10000.0)
    payload = build_payload(_inputs(grid=grid)).payload
    assert payload["maximum_power_from_grid"] == 11000.0
    assert payload["maximum_power_to_grid"] == 10000.0


def test_the_import_floor_comes_from_the_forecast_peak():
    """The peak, not the mean: EMHASS's limit binds one timestep at a time."""
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    load = Series(
        [Point(now, 400.0), Point(now + HALF_HOUR, 4200.0), Point(now + 2 * HALF_HOUR, 700.0)]
    )
    result = build_payload(_inputs(load=load, grid_import_limit_w=100.0))
    assert result.payload["maximum_power_from_grid"] == 4200.0
    assert any("4200 W" in warning for warning in result.warnings)


def test_the_import_floor_ignores_forecast_points_before_the_run():
    """A load series commonly starts at local midnight; a peak that has already
    happened says nothing about what the plan has to serve."""
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    load = Series([Point(now - HALF_HOUR, 9000.0), Point(now, 500.0)])
    payload = build_payload(_inputs(load=load, grid_import_limit_w=800.0)).payload
    assert payload["maximum_power_from_grid"] == 800.0


def test_the_import_floor_uses_the_live_load_when_there_is_no_forecast():
    """The load profiles that let EMHASS build its own forecast hand us no
    series at all, which is exactly when this is the only floor available."""
    payload = build_payload(_inputs(load_live_w=2500.0, grid_import_limit_w=900.0)).payload
    assert payload["maximum_power_from_grid"] == 2500.0


def test_an_unfloored_live_import_limit_is_not_reported():
    """The warning marks a limit that could not be honoured, so a sensor doing
    its job must not produce one on every run."""
    result = build_payload(_inputs(load_live_w=2500.0, grid_import_limit_w=6800.0))
    assert not any("import limit" in warning for warning in result.warnings)


# --- the windowed capacity limit array (network tariffs, mechanism 3) --------


def test_no_capacity_ceiling_configured_keeps_the_plain_scalar():
    """The common case -- no network profile, or one with no capacity concern
    -- must keep sending the scalar every other grid-limit test expects."""
    payload = build_payload(_inputs()).payload
    assert payload["maximum_power_from_grid"] == 9000  # DEFAULT_GRID_IMPORT_MAX
    assert isinstance(payload["maximum_power_from_grid"], int)


def test_capacity_limit_only_lowers_timesteps_inside_its_window():
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    window_start = now + HALF_HOUR
    window_end = now + 3 * HALF_HOUR
    result = build_payload(
        _inputs(
            now=now,
            capacity_limit_w=4000.0,
            capacity_limit_window=lambda when: window_start <= when < window_end,
        )
    )
    array = result.payload["maximum_power_from_grid"]
    assert isinstance(array, list)
    assert len(array) == DAY_STEPS
    assert array[0] == 9000  # before the window: the plain scalar
    assert array[1] == 4000  # inside the window: the ceiling
    assert array[2] == 4000
    assert array[3] == 9000  # after the window


def test_capacity_limit_array_length_matches_horizon_steps_on_mpc():
    result = build_payload(
        _inputs(capacity_limit_w=4000.0, capacity_limit_window=lambda when: True)
    )
    assert len(result.payload["maximum_power_from_grid"]) == DAY_STEPS


def test_capacity_limit_array_length_is_whole_days_on_dayahead():
    """Not horizon_steps -- the array covers delta_forecast_daily whole days,
    which rounds a short horizon up. See docs/network_tariffs_plan.md,
    "Mechanism 3"."""
    result = build_payload(
        _inputs(
            action=ACTION_DAYAHEAD,
            horizon_steps=1,
            capacity_limit_w=4000.0,
            capacity_limit_window=lambda when: True,
        )
    )
    assert (
        len(result.payload["maximum_power_from_grid"]) == DAY_STEPS
    )  # one whole day, 30-min steps


def test_capacity_limit_is_floored_at_its_own_timesteps_forecast_load():
    """The floor is that timestep's own forecast load, not the horizon's peak
    -- a per-timestep cap below one hour's own draw makes the whole run
    infeasible, and EMHASS answers infeasible for the entire problem."""
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    load = Series(
        [Point(now, 1000.0), Point(now + HALF_HOUR, 5000.0), Point(now + 2 * HALF_HOUR, 1000.0)]
    )
    result = build_payload(
        _inputs(
            now=now,
            load=load,
            capacity_limit_w=2000.0,
            capacity_limit_window=lambda when: True,
        )
    )
    array = result.payload["maximum_power_from_grid"]
    assert array[0] == 2000  # 1000 W forecast fits under the 2000 W ceiling
    assert array[1] == 5000  # 5000 W forecast raises this one timestep only
    assert array[2] == 2000
    assert any("raised above its target" in warning for warning in result.warnings)


def test_demand_fallback_ceiling_applies_only_when_the_peak_is_unpriced():
    result = build_payload(
        _inputs(
            network_demand_charge_configured=True,
            demand_charge_rate_per_kw=None,
            demand_fallback_ceiling_w=3000.0,
            demand_window=lambda when: True,
        )
    )
    array = result.payload["maximum_power_from_grid"]
    assert isinstance(array, list)
    assert all(value == 3000 for value in array)


def test_demand_fallback_ceiling_is_inert_once_the_peak_is_priced():
    """Once the priced peak is safe to send, the ceiling fallback must not
    also apply -- the two are mutually exclusive degrees of the same
    mechanism (see docs/network_tariffs_plan.md, "Version gating")."""
    result = build_payload(
        _inputs(
            network_demand_charge_configured=True,
            demand_charge_rate_per_kw=45.0,
            demand_fallback_ceiling_w=3000.0,
            demand_window=lambda when: True,
        )
    )
    assert result.payload["maximum_power_from_grid"] == 9000
    assert isinstance(result.payload["maximum_power_from_grid"], int)


def test_capacity_ceilings_compose_as_the_tighter_of_both():
    """An explicit capacity_limit and the demand-window fallback can both be
    configured at once; the array takes whichever is lower at each timestep."""
    result = build_payload(
        _inputs(
            capacity_limit_w=5000.0,
            capacity_limit_window=lambda when: True,
            network_demand_charge_configured=True,
            demand_charge_rate_per_kw=None,
            demand_fallback_ceiling_w=3000.0,
            demand_window=lambda when: True,
        )
    )
    assert all(value == 3000 for value in result.payload["maximum_power_from_grid"])


def test_battery_cycle_costs_are_omitted_with_no_battery():
    """Nothing battery-shaped is sent when the flag is off, these included."""
    battery = BatteryConfig(enabled=False, weight_battery_discharge=0.06)
    payload = build_payload(_inputs(battery=battery)).payload
    assert "weight_battery_discharge" not in payload
    assert "weight_battery_charge" not in payload
    assert "set_nodischarge_to_grid" not in payload


def test_a_chosen_end_soc_replaces_the_pin_to_start():
    """terminal.decide_end_soc's target rides through as soc_final."""
    battery = BatteryConfig(enabled=True, capacity_wh=25600)
    payload = build_payload(_inputs(battery=battery, soc_init=0.098, soc_final=0.42)).payload
    assert payload["soc_init"] == 0.098
    assert payload["soc_final"] == 0.42


# --- hybrid inverter -----------------------------------------------------------


def test_hybrid_inverter_disabled_sends_only_the_flag():
    """Disabled must still assert the flag -- never leave it to EMHASS's own
    persisted config.json, the same reasoning as the battery flag above."""
    payload = build_payload(_inputs()).payload
    assert payload["inverter_is_hybrid"] is False
    assert "inverter_ac_output_max" not in payload


def test_hybrid_inverter_enabled_sends_every_limit():
    hybrid = HybridInverterConfig(
        enabled=True,
        ac_output_max_w=5000,
        ac_input_max_w=6000,
        efficiency_dc_ac=0.97,
        efficiency_ac_dc=0.98,
    )
    payload = build_payload(_inputs(hybrid_inverter=hybrid)).payload
    assert payload["inverter_is_hybrid"] is True
    assert payload["inverter_ac_output_max"] == 5000
    assert payload["inverter_ac_input_max"] == 6000
    assert payload["inverter_efficiency_dc_ac"] == 0.97
    assert payload["inverter_efficiency_ac_dc"] == 0.98


def test_no_deferrable_loads_is_a_valid_configuration():
    payload = build_payload(_inputs()).payload
    assert payload["number_of_deferrable_loads"] == 0


@pytest.mark.usefixtures("stockholm_timezone")
def test_deferrable_arrays_are_parallel_to_load_order():
    """Every per-load array must line up with ``load_order`` index for index.

    ``load_order[k]`` is what tells the rest of the integration which subentry
    owns EMHASS's ``P_deferrable{k}`` column. If the arrays and that list ever
    disagree, each load silently gets another load's schedule.
    """
    loads = [
        DeferrableLoad(
            subentry_id="dishwasher",
            name="Dishwasher",
            nominal_power_w=2000,
            operating_hours=2,
            semi_continuous=True,
        ),
        DeferrableLoad(
            subentry_id="car",
            name="Car",
            nominal_power_w=11000,
            operating_hours=3,
            semi_continuous=False,
            current_state=True,
        ),
    ]
    result = build_payload(_inputs(loads=loads))
    payload = result.payload

    # build_payload preserves the order it is given; choosing that order is the
    # coordinator's job, so that it stays stable across runs.
    assert result.load_order == ["dishwasher", "car"]
    assert payload["number_of_deferrable_loads"] == 2

    car = result.load_order.index("car")
    assert payload["nominal_power_of_deferrable_loads"][car] == 11000
    assert payload["operating_hours_of_each_deferrable_load"][car] == 3
    assert payload["treat_deferrable_load_as_semi_cont"][car] is False
    assert payload["def_current_state"][car] is True

    for key in (
        "nominal_power_of_deferrable_loads",
        "minimum_power_of_deferrable_loads",
        "operating_hours_of_each_deferrable_load",
        "operating_timesteps_of_each_deferrable_load",
        "start_timesteps_of_each_deferrable_load",
        "end_timesteps_of_each_deferrable_load",
        "treat_deferrable_load_as_semi_cont",
        "set_deferrable_load_single_constant",
        "set_deferrable_startup_penalty",
        "set_deferrable_max_startups",
        "def_minimum_on_time",
        "def_minimum_off_time",
        "def_current_state",
        "def_current_operating_timesteps",
        "def_current_on_timesteps",
        "def_current_off_timesteps",
        "deferrable_load_max_cost",
    ):
        assert len(payload[key]) == len(result.load_order), key


# --- operating hours must land on timestep boundaries ------------------------


@pytest.mark.parametrize(
    ("hours", "step_minutes", "expected_steps"),
    [
        (2, 30, 4),
        (2, 15, 8),
        (0, 30, 0),
        # Rounds to the nearest step ...
        (2.6, 30, 5),
        (0.4, 30, 1),
        # ... except that a non-zero request never rounds down to "never run".
        (0.1, 30, 1),
        (0.01, 60, 1),
    ],
)
def test_operating_hours_become_whole_timesteps(hours, step_minutes, expected_steps):
    assert operating_timesteps(hours, step_minutes) == expected_steps


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_run_time_off_the_timestep_grid_is_quantised_and_reported():
    """EMHASS enforces `sum(p) * dt == nominal * hours` as an equality.

    A semi-continuous load can only draw 0 or exactly its nominal power, so
    2.6 h at a 30-minute step has no solution at all -- EMHASS reports an
    infeasible problem rather than rounding. Quantising here is what keeps that
    from happening, and the warning is what stops it being a silent change.
    """
    loads = [
        DeferrableLoad(
            subentry_id="car",
            name="Car",
            nominal_power_w=11000,
            operating_hours=2.6,
            semi_continuous=True,
        )
    ]
    result = build_payload(_inputs(loads=loads))

    assert result.payload["operating_hours_of_each_deferrable_load"] == [2.5]
    assert result.payload["operating_timesteps_of_each_deferrable_load"] == [5]
    assert any("2.5 h" in warning for warning in result.warnings)


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_run_time_already_on_the_grid_is_not_reported():
    loads = [
        DeferrableLoad(
            subentry_id="dishwasher", name="Dishwasher", nominal_power_w=2000, operating_hours=2
        )
    ]
    result = build_payload(_inputs(loads=loads))

    assert result.payload["operating_hours_of_each_deferrable_load"] == [2.0]
    assert result.warnings == []


@pytest.mark.usefixtures("stockholm_timezone")
def test_exact_timesteps_are_sent_only_for_mpc():
    """EMHASS honours this key only on the branch that takes a prediction
    horizon (utils.treat_runtimeparams); it is absent from associations.csv, so
    the day-ahead request has to make do with the quantised hours."""
    loads = [
        DeferrableLoad(
            subentry_id="dishwasher", name="Dishwasher", nominal_power_w=2000, operating_hours=2
        )
    ]

    mpc = build_payload(_inputs(loads=loads, action=ACTION_MPC)).payload
    dayahead = build_payload(_inputs(loads=loads, action=ACTION_DAYAHEAD)).payload

    assert mpc["operating_timesteps_of_each_deferrable_load"] == [4]
    assert "operating_timesteps_of_each_deferrable_load" not in dayahead
    assert dayahead["operating_hours_of_each_deferrable_load"] == [2.0]


# --- minimum power -----------------------------------------------------------


@pytest.mark.usefixtures("stockholm_timezone")
def test_minimum_power_is_sent_per_load():
    loads = [
        DeferrableLoad(
            subentry_id="car",
            name="Car",
            nominal_power_w=11000,
            minimum_power_w=1380,
            operating_hours=3,
            semi_continuous=False,
        )
    ]
    payload = build_payload(_inputs(loads=loads)).payload
    assert payload["minimum_power_of_deferrable_loads"] == [1380]


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_minimum_power_above_nominal_is_clamped():
    """A floor above the ceiling leaves no feasible power at all, and EMHASS
    reports that as an infeasible problem naming no load in particular."""
    loads = [
        DeferrableLoad(
            subentry_id="car",
            name="Car",
            nominal_power_w=3000,
            minimum_power_w=5000,
            operating_hours=1,
            semi_continuous=False,
        )
    ]
    result = build_payload(_inputs(loads=loads))

    assert result.payload["minimum_power_of_deferrable_loads"] == [3000]
    assert any("minimum power" in warning for warning in result.warnings)


@pytest.mark.usefixtures("stockholm_timezone")
def test_max_startups_is_sent_per_load():
    loads = [
        DeferrableLoad(
            subentry_id="heatpump",
            name="Heat pump",
            nominal_power_w=3000,
            operating_hours=4,
            max_startups=2,
        )
    ]
    payload = build_payload(_inputs(loads=loads)).payload
    assert payload["set_deferrable_max_startups"] == [2]


@pytest.mark.usefixtures("stockholm_timezone")
def test_minimum_on_off_time_is_converted_to_timesteps():
    """Minutes in, timesteps out -- reusing operating_timesteps() gives the
    same 0-stays-0, nonzero-floors-at-1-step semantics as operating hours."""
    loads = [
        DeferrableLoad(
            subentry_id="heatpump",
            name="Heat pump",
            nominal_power_w=3000,
            operating_hours=4,
            minimum_on_time_minutes=45,
            minimum_off_time_minutes=6,
        )
    ]
    payload = build_payload(_inputs(loads=loads, time_step_minutes=30)).payload
    # 45 min at a 30-minute step rounds to 1.5 steps -> 2; 6 min is nonzero so
    # it floors at 1 step rather than vanishing.
    assert payload["def_minimum_on_time"] == [2]
    assert payload["def_minimum_off_time"] == [1]


@pytest.mark.usefixtures("stockholm_timezone")
def test_minimum_on_off_time_zero_stays_zero():
    loads = [
        DeferrableLoad(
            subentry_id="dishwasher", name="Dishwasher", nominal_power_w=2000, operating_hours=2
        )
    ]
    payload = build_payload(_inputs(loads=loads)).payload
    assert payload["def_minimum_on_time"] == [0]
    assert payload["def_minimum_off_time"] == [0]


@pytest.mark.usefixtures("stockholm_timezone")
def test_current_on_off_timesteps_pass_through_per_load():
    loads = [
        DeferrableLoad(
            subentry_id="heatpump",
            name="Heat pump",
            nominal_power_w=3000,
            operating_hours=4,
            current_on_timesteps=3,
            current_off_timesteps=0,
        )
    ]
    payload = build_payload(_inputs(loads=loads)).payload
    assert payload["def_current_on_timesteps"] == [3]
    assert payload["def_current_off_timesteps"] == [0]


# --- deferrable load groups ---------------------------------------------------


@pytest.mark.usefixtures("stockholm_timezone")
def test_load_group_names_resolve_by_load_order_position():
    loads = [
        DeferrableLoad(
            subentry_id="dishwasher", name="Dishwasher", nominal_power_w=2000, operating_hours=2
        ),
        DeferrableLoad(subentry_id="car", name="Car", nominal_power_w=11000, operating_hours=3),
        DeferrableLoad(
            subentry_id="heater", name="Heater", nominal_power_w=1500, operating_hours=1
        ),
    ]
    groups = [
        DeferrableLoadGroup(
            subentry_id="group1",
            name="Fuse box",
            load_subentry_ids=("car", "heater"),
            max_power_w=3500,
        )
    ]
    payload = build_payload(_inputs(loads=loads, load_groups=groups)).payload

    assert payload["deferrable_load_groups"] == [
        {"names": ["deferrable1", "deferrable2"], "mutual_exclusion": False, "max_power": 3500}
    ]


@pytest.mark.usefixtures("stockholm_timezone")
def test_load_group_referencing_a_deleted_load_drops_it_with_a_warning():
    loads = [
        DeferrableLoad(subentry_id="car", name="Car", nominal_power_w=11000, operating_hours=3),
        DeferrableLoad(
            subentry_id="heater", name="Heater", nominal_power_w=1500, operating_hours=1
        ),
    ]
    groups = [
        DeferrableLoadGroup(
            subentry_id="group1",
            name="Fuse box",
            load_subentry_ids=("car", "heater", "no-longer-exists"),
            max_power_w=3500,
        )
    ]
    result = build_payload(_inputs(loads=loads, load_groups=groups))

    assert result.payload["deferrable_load_groups"] == [
        {"names": ["deferrable0", "deferrable1"], "mutual_exclusion": False, "max_power": 3500}
    ]
    assert any("no longer exists" in warning for warning in result.warnings)


@pytest.mark.usefixtures("stockholm_timezone")
def test_load_group_with_fewer_than_two_valid_members_is_dropped():
    loads = [
        DeferrableLoad(subentry_id="car", name="Car", nominal_power_w=11000, operating_hours=3),
    ]
    groups = [
        DeferrableLoadGroup(
            subentry_id="group1",
            name="Fuse box",
            load_subentry_ids=("car", "gone1", "gone2"),
            max_power_w=3500,
        )
    ]
    result = build_payload(_inputs(loads=loads, load_groups=groups))

    assert "deferrable_load_groups" not in result.payload
    assert any("dropping the group" in warning for warning in result.warnings)


@pytest.mark.usefixtures("stockholm_timezone")
def test_load_group_mutual_exclusion_omits_max_power_when_unset():
    loads = [
        DeferrableLoad(subentry_id="car", name="Car", nominal_power_w=11000, operating_hours=3),
        DeferrableLoad(
            subentry_id="heater", name="Heater", nominal_power_w=1500, operating_hours=1
        ),
    ]
    groups = [
        DeferrableLoadGroup(
            subentry_id="group1",
            name="Fuse box",
            load_subentry_ids=("car", "heater"),
            mutual_exclusion=True,
        )
    ]
    payload = build_payload(_inputs(loads=loads, load_groups=groups)).payload

    assert payload["deferrable_load_groups"] == [
        {"names": ["deferrable0", "deferrable1"], "mutual_exclusion": True}
    ]


@pytest.mark.usefixtures("stockholm_timezone")
def test_no_load_groups_configured_sends_no_key_at_all():
    loads = [
        DeferrableLoad(subentry_id="car", name="Car", nominal_power_w=11000, operating_hours=3),
    ]
    payload = build_payload(_inputs(loads=loads)).payload
    assert "deferrable_load_groups" not in payload


@pytest.mark.usefixtures("stockholm_timezone")
def test_deferrable_load_max_cost_is_disabled_but_correctly_sized():
    """Companion has no per-load max-cost feature -- but must still send this.

    EMHASS's optimisation.py indexes this list with no bounds check, unlike
    every sibling array (all `k < len(...)` guarded). Leaving it to whatever is
    persisted in EMHASS's own config is how a load count increase turns into an
    uncaught IndexError and a 500 on every optimisation, rather than the clean
    "unlimited" behaviour a missing key would give.
    """
    loads = [
        DeferrableLoad(
            subentry_id=f"load{i}", name=f"Load {i}", nominal_power_w=1000, operating_hours=1
        )
        for i in range(3)
    ]
    payload = build_payload(_inputs(loads=loads)).payload

    assert payload["deferrable_load_max_cost"] == [0.0, 0.0, 0.0]


def test_a_load_that_wants_nothing_is_parked_rather_than_dropped():
    """Dropping it renumbered every load after it, so no automation or
    dashboard could refer to "deferrable 1" and mean anything durable."""
    loads = [
        DeferrableLoad(subentry_id="a", name="On", nominal_power_w=1000, operating_hours=1),
        DeferrableLoad(
            subentry_id="b",
            name="Off",
            nominal_power_w=2000,
            operating_hours=1,
            wants_to_run=False,
        ),
    ]
    result = build_payload(_inputs(loads=loads))
    payload = result.payload

    assert payload["number_of_deferrable_loads"] == 2
    assert result.load_order == ["a", "b"]
    # Parked: no run time, and nothing left that could make it run anyway.
    assert payload["operating_hours_of_each_deferrable_load"] == [1.0, 0.0]
    assert payload["operating_timesteps_of_each_deferrable_load"] == [2, 0]
    assert payload["def_current_state"] == [False, False]
    assert payload["def_current_operating_timesteps"] == [0, 0]
    # The rating stays truthful; EMHASS wants a positive power per load.
    assert payload["nominal_power_of_deferrable_loads"] == [1000, 2000]


def test_a_parked_load_never_contradicts_its_zero_hours():
    """Telling EMHASS a load is running, or has already run, while demanding it
    total zero energy is a contradiction -- and EMHASS answers a contradiction
    by declaring the whole problem infeasible."""
    loads = [
        DeferrableLoad(
            subentry_id="a",
            name="Off but running",
            nominal_power_w=2000,
            operating_hours=2,
            wants_to_run=False,
            current_state=True,
            current_power_w=2000.0,
            completed_timesteps=3,
            minimum_power_w=1500.0,
            single_constant=True,
        ),
    ]
    payload = build_payload(_inputs(loads=loads)).payload
    assert payload["def_current_state"] == [False]
    assert payload["def_current_operating_timesteps"] == [0]
    assert payload["minimum_power_of_deferrable_loads"] == [0.0]
    assert payload["set_deferrable_load_single_constant"] == [False]
    assert payload["operating_hours_of_each_deferrable_load"] == [0.0]


def test_a_parked_load_produces_no_warnings_about_its_window():
    """Its window is not being honoured, so complaining about it is noise."""
    loads = [
        DeferrableLoad(
            subentry_id="a",
            name="Off",
            nominal_power_w=1000,
            operating_hours=3,
            wants_to_run=False,
            earliest_start=time(22, 0),
            latest_end=time(23, 0),
        ),
    ]
    assert build_payload(_inputs(loads=loads)).warnings == []


# --- a currently-running single-constant load whose window moved on ----------


def test_a_currently_running_single_constant_load_is_not_pinned_to_a_future_window():
    """EMHASS pins a currently-on single-constant load to run from t=0 for its
    full requested duration, widening the window mask to make room -- an
    unbroken block can't be split. That is right when the window still covers
    now, but wrong when it has moved on: the load would be pinned to a fresh
    run it has no business starting immediately. Asking for 0 hours this cycle
    avoids the pin; the later block gets asked for normally once "now" reaches
    it.
    """
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    loads = [
        DeferrableLoad(
            subentry_id="pool",
            name="Pool",
            nominal_power_w=850,
            operating_hours=3,
            single_constant=True,
            current_state=True,
            current_power_w=850.0,
            start_at=now + timedelta(hours=2),
        ),
    ]
    result = build_payload(_inputs(now=now, loads=loads))
    payload = result.payload

    assert payload["operating_hours_of_each_deferrable_load"] == [0.0]
    assert payload["operating_timesteps_of_each_deferrable_load"] == [0]
    assert any("asking for 0 hours" in warning for warning in result.warnings)


def test_a_currently_running_single_constant_load_keeps_its_hours_when_the_window_covers_now():
    """No change for the ordinary case: the window already covers now, so the
    pin is simply continuing the block that opened it."""
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    loads = [
        DeferrableLoad(
            subentry_id="pool",
            name="Pool",
            nominal_power_w=850,
            operating_hours=3,
            single_constant=True,
            current_state=True,
            current_power_w=850.0,
        ),
    ]
    result = build_payload(_inputs(now=now, loads=loads))

    assert result.payload["operating_hours_of_each_deferrable_load"] == [3.0]
    assert result.warnings == []


def test_a_future_window_only_zeroes_hours_for_single_constant_loads():
    """A non-single-constant load has no pin to avoid -- EMHASS's own
    current-power mechanism only ever forces a single timestep for it, not a
    fresh multi-hour block, so its hours are left alone."""
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    loads = [
        DeferrableLoad(
            subentry_id="car",
            name="Car",
            nominal_power_w=3000,
            operating_hours=3,
            single_constant=False,
            current_state=True,
            current_power_w=3000.0,
            start_at=now + timedelta(hours=2),
        ),
    ]
    payload = build_payload(_inputs(now=now, loads=loads)).payload

    assert payload["operating_hours_of_each_deferrable_load"] == [3.0]


def test_a_future_window_only_zeroes_hours_when_actually_running():
    """A load that has not started yet has no pin to avoid either -- it is
    not "currently on", so its future window is the ordinary, correct way to
    ask for it."""
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    loads = [
        DeferrableLoad(
            subentry_id="pool",
            name="Pool",
            nominal_power_w=850,
            operating_hours=3,
            single_constant=True,
            current_state=False,
            start_at=now + timedelta(hours=2),
        ),
    ]
    payload = build_payload(_inputs(now=now, loads=loads)).payload

    assert payload["operating_hours_of_each_deferrable_load"] == [3.0]


def test_a_window_opening_beyond_the_horizon_parks_the_load_instead_of_freeing_it():
    """0 is EMHASS's sentinel for "may start immediately", the same index a
    beyond-horizon window gets clamped to. Passing hours through with that
    index would let a quiet-hours load run mid-day on a short MPC horizon
    that can't see as far as its window -- it must be parked instead."""
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    loads = [
        DeferrableLoad(
            subentry_id="dishwasher",
            name="Dishwasher",
            nominal_power_w=1500,
            operating_hours=2,
            earliest_start=time(22, 0),
            latest_end=time(6, 0),
        ),
    ]
    # 4 steps of 30 minutes reaches 12:00 -- nowhere near tonight's 22:00.
    result = build_payload(_inputs(now=now, loads=loads, horizon_steps=4))
    payload = result.payload

    assert payload["operating_hours_of_each_deferrable_load"] == [0.0]
    assert payload["operating_timesteps_of_each_deferrable_load"] == [0]
    assert any("will not be scheduled to run this cycle" in warning for warning in result.warnings)


def test_profile_settings_can_override_defaults():
    """A "no solar" profile must be able to switch PV modelling off."""
    payload = build_payload(_inputs(extra_settings={"set_use_pv": False})).payload
    assert payload["set_use_pv"] is False


# --- deadlines, and their intersection with the window ------------------------


def _window(**overrides):
    kwargs = {
        "name": "Dishwasher",
        "earliest": None,
        "latest": None,
        "deadline": None,
        "grid_start": _local(2026, 7, 28, 20),
        "step": HALF_HOUR,
        "horizon_steps": DAY_STEPS,
        "operating_steps": 4,
    }
    return resolve_load_window(**{**kwargs, **overrides})


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_deadline_alone_bounds_the_end():
    # Requested at 20:00, within 4h -> may run from now until 00:00 (8 steps).
    window = _window(deadline=_local(2026, 7, 29, 0))
    assert (window.start_index, window.end_index) == (0, 8)
    assert window.warnings == []


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_deadline_and_a_window_intersect():
    """Quiet hours from 22:00 plus "within 6h" of a 20:00 request: the load may
    run between 22:00 and 02:00, and both constraints are real."""
    window = _window(earliest=time(22, 0), latest=time(6, 0), deadline=_local(2026, 7, 29, 2))
    assert (window.start_index, window.end_index) == (4, 12)


@pytest.mark.usefixtures("stockholm_timezone")
def test_the_window_still_binds_when_there_is_no_deadline():
    window = _window(earliest=time(22, 0), latest=time(6, 0))
    assert (window.start_index, window.end_index) == (4, 20)


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_deadline_before_the_window_opens_wins_and_says_so():
    """An explicit, dated request beats a standing preference. A request that
    silently does nothing for two hours is worse than one that breaks quiet
    hours audibly."""
    window = _window(
        earliest=time(22, 0),
        latest=time(6, 0),
        deadline=_local(2026, 7, 28, 21),
        operating_steps=2,
    )
    assert (window.start_index, window.end_index) == (0, 2)
    assert any("before its time window opens" in warning for warning in window.warnings)


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_passed_deadline_is_scheduled_asap_not_whenever():
    """Falling through to EMHASS's 0 would turn a missed deadline into
    "unconstrained", which is the one answer nobody asked for."""
    window = _window(deadline=_local(2026, 7, 28, 19))
    assert window.start_index == 0
    assert window.end_index == 4  # exactly the run length, i.e. as soon as it fits
    assert any("already passed" in warning for warning in window.warnings)


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_deadline_beyond_the_horizon_is_unconstrained():
    """And it starts binding on its own as later runs advance the horizon
    towards it -- which a recurring wall-clock window never does."""
    window = _window(deadline=_local(2026, 7, 30, 20))
    assert (window.start_index, window.end_index) == (0, 0)


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_window_too_narrow_for_the_run_names_the_load():
    """EMHASS reports an over-tight window as an infeasible *problem*, with no
    hint which load caused it -- and one load fails the whole optimisation."""
    window = _window(earliest=time(22, 0), latest=time(23, 0), operating_steps=4)
    assert any("infeasible" in warning for warning in window.warnings)
    assert any("Dishwasher" in warning for warning in window.warnings)


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_wide_enough_window_says_nothing():
    assert _window(earliest=time(22, 0), latest=time(6, 0), operating_steps=4).warnings == []


@pytest.mark.usefixtures("stockholm_timezone")
def test_the_deadline_reaches_the_payload():
    now = _local(2026, 7, 28, 20)
    loads = [
        DeferrableLoad(
            subentry_id="dishwasher",
            name="Dishwasher",
            nominal_power_w=2000,
            operating_hours=2,
            deadline_at=_local(2026, 7, 29, 0),
        ),
    ]
    payload = build_payload(_inputs(now=now, loads=loads)).payload
    assert payload["start_timesteps_of_each_deferrable_load"] == [0]
    assert payload["end_timesteps_of_each_deferrable_load"] == [8]


@pytest.mark.usefixtures("stockholm_timezone")
def test_payload_warnings_name_the_load_whose_window_is_too_tight():
    now = _local(2026, 7, 28, 20)
    loads = [
        DeferrableLoad(
            subentry_id="dishwasher",
            name="Dishwasher",
            nominal_power_w=2000,
            operating_hours=3,
            earliest_start=time(22, 0),
            latest_end=time(23, 0),
        ),
    ]
    result = build_payload(_inputs(now=now, loads=loads))
    assert any("Dishwasher" in warning for warning in result.warnings)


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_deadline_that_has_run_out_of_room_is_relaxed_not_sent_as_is():
    """The live failure this check was added for.

    A dishwasher needing 1 h with "within 1.25 h" has 15 minutes of slack. The
    deadline is anchored at the request, so the room left shrinks as the clock
    advances -- and a run that no longer fits makes the *whole problem*
    infeasible, taking the battery and every other load down with it.
    """
    now = _local(2026, 7, 28, 12, 45)
    window = resolve_load_window(
        name="Dishwasher",
        earliest=None,
        latest=None,
        deadline=_local(2026, 7, 28, 13, 30),  # 45 min left for a 60 min run
        grid_start=now,
        step=timedelta(minutes=15),
        horizon_steps=96,
        operating_steps=4,
    )
    assert window.end_index - window.start_index == 4  # room for the full run
    assert any("no longer leaves room" in warning for warning in window.warnings)
    assert not any("infeasible" in warning for warning in window.warnings)


@pytest.mark.usefixtures("stockholm_timezone")
def test_completed_timesteps_shrink_the_window_not_just_the_energy():
    """A load already partway through its run must not be treated by the
    window logic as if nothing had happened.

    Same shape as the deadline-run-out-of-room case above, but this time half
    the run is already done (completed_timesteps=2 of the 4 required). Only
    45 minutes (3 steps) remain before the deadline -- too little for the
    *full* 60-minute run, but plenty for the 30 minutes actually left.
    Crediting that progress only against def_current_operating_timesteps
    while leaving the window sized for the full run would force it to relax
    the deadline and restart a fresh full-width window pinned to "now" on
    every request, never converging.
    """
    now = _local(2026, 7, 28, 12, 45)
    loads = [
        DeferrableLoad(
            subentry_id="dishwasher",
            name="Dishwasher",
            nominal_power_w=1101,
            operating_hours=1.0,
            deadline_at=_local(2026, 7, 28, 13, 30),  # 45 min left
            completed_timesteps=2,  # 30 min of the 60 min run already done
        ),
    ]
    result = build_payload(_inputs(now=now, loads=loads, time_step_minutes=15))
    start = result.payload["start_timesteps_of_each_deferrable_load"][0]
    end = result.payload["end_timesteps_of_each_deferrable_load"][0]
    assert end - start == 3  # all the room the deadline actually has left
    assert not any("no longer leaves room" in warning for warning in result.warnings)
    # The full (uncredited) target still goes to EMHASS -- it does its own
    # decrement via def_current_operating_timesteps.
    assert result.payload["operating_hours_of_each_deferrable_load"] == [1.0]
    assert result.payload["def_current_operating_timesteps"] == [2]


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_standing_window_too_narrow_is_reported_not_overridden():
    """Unlike a deadline, a too-narrow window is a configuration error.

    Widening it here would breach the user's quiet hours every night without
    them ever finding out why, so it is named and left alone.
    """
    window = _window(earliest=time(22, 0), latest=time(23, 0), operating_steps=4)
    assert window.end_index - window.start_index == 2  # left as configured
    assert any("infeasible" in warning for warning in window.warnings)


# --- thermal loads -----------------------------------------------------------


def _thermal_load(subentry_id: str = "hp", **overrides) -> DeferrableLoad:
    defaults = {
        "subentry_id": subentry_id,
        "name": "Heat pump",
        "nominal_power_w": 3000,
        "operating_hours": 0,
        "thermal": ThermalConfig(current_temperature=19.5),
    }
    return DeferrableLoad(**{**defaults, **overrides})


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_thermal_load_sends_def_load_config_at_its_own_index():
    """The list must cover every load, empty for the ordinary ones -- EMHASS
    overwrites number_of_deferrable_loads with its length."""
    loads = [
        DeferrableLoad(
            subentry_id="dishwasher", name="Dishwasher", nominal_power_w=2000, operating_hours=2
        ),
        _thermal_load(),
    ]
    payload = build_payload(_inputs(loads=loads)).payload

    config = payload["def_load_config"]
    assert len(config) == 2
    assert config[0] == {}
    thermal = config[1]["thermal_config"]
    assert thermal["start_temperature"] == 19.5
    assert len(thermal["min_temperatures"]) == DAY_STEPS
    # A thermal load asks for no run time; its temperature is the demand.
    assert payload["operating_hours_of_each_deferrable_load"][1] == 0


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_disabled_thermal_load_sends_no_temperature_demands():
    """Parked at zero hours *and* stripped of its comfort band: temperature
    targets are exactly the demand a parked load must not carry."""
    payload = build_payload(_inputs(loads=[_thermal_load(wants_to_run=False)])).payload
    assert "def_load_config" not in payload
    assert payload["number_of_deferrable_loads"] == 1


def test_an_outdoor_temperature_forecast_is_sent_when_provided():
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    result = build_payload(_inputs(outdoor_temperature=_series(now, 48, 15.0)))
    forecast = result.payload["outdoor_temperature_forecast"]
    assert isinstance(forecast, dict)
    assert forecast["2026-07-28T10:00:00+00:00"] == 15.0

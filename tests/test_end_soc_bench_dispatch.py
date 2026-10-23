"""The reward-factor scoring in the End SOC backtest harness."""

from __future__ import annotations

import numpy as np
import pytest

from tools.end_soc_bench.dispatch import Dispatcher, Plant


def _dispatcher() -> Dispatcher:
    plant = Plant(
        capacity_wh=10_000,
        charge_power_max_w=5_000,
        discharge_power_max_w=5_000,
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        soc_min=0.0,
        soc_max=1.0,
        wear_charge=0.0,
        wear_discharge=0.0,
        import_max_w=20_000,
        export_max_w=20_000,
    )
    return Dispatcher(plant, step_hours=1.0, levels=11)


def test_a_zero_factor_scores_the_ordinary_two_sided_pin():
    dispatcher = _dispatcher()
    # Holding more costs nothing to reach and saves a lot afterwards, so a
    # reward-free optimiser would drift above the pin; factor 0 must not.
    to_reach = np.linspace(0.0, 1.0, 11)
    to_go = np.linspace(5.0, 0.0, 11)
    total = to_reach + to_go
    assert dispatcher.evaluate_with_reward(
        to_reach, to_go, 0.5, factor=0.0, last_price=2.0, under_penalty=100.0
    ) == pytest.approx(dispatcher.evaluate(total, 0.5))


def test_a_reward_lets_the_plan_end_above_the_pin():
    dispatcher = _dispatcher()
    to_reach = np.linspace(0.0, 1.0, 11)
    to_go = np.full(11, 3.0)
    held = dispatcher.evaluate_with_reward(
        to_reach, to_go, 0.5, factor=1.0, last_price=100.0, under_penalty=1000.0
    )
    # Ending at the top (reach cost 1.0) beats ending on the pin (0.5) once
    # each kWh above it pays 100.
    assert held == pytest.approx(1.0 + 3.0)

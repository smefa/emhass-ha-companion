"""Score an end-of-horizon SOC pin by the money it actually costs.

A terminal-SOC heuristic cannot be judged by looking at the number it returns.
The only thing that matters is what the *whole* run costs once the pin has been
honoured and the world past the horizon has happened -- a pin that looks
miserly is right if the night was cheap, and a pin that looks prudent is wrong
if it bought a battery-full at the evening peak.

So this module does what EMHASS does, offline and with perfect hindsight: a
dynamic program over a discretised SOC that finds the cheapest possible battery
dispatch. Run over a long window it gives the *oracle* cost -- what a controller
that knew the future would have paid. Split at the pin it gives, for every
possible pin value, the cheapest way to reach it and the cheapest way to live
with it afterwards:

    ``total(pin) = cost_to_reach(pin) + cost_to_go(pin)``

Both halves come out of one forward and one backward pass, so every candidate's
answer is a two-element lookup rather than a fresh optimisation, and the
oracle is just ``min(total)``. What a candidate is charged for is exactly its
own mistake:

    ``regret(pin) = total(pin) - min(total)``

which is in kronor and comparable across a winter night and a July afternoon.

The window runs well past the pin (72 h by default, pinned at 24 h) so that the
arbitrary end-of-window residual value sits two days away from the decision
being measured and cannot drive it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# What an import above the connection limit costs per kWh, on top of the price.
# A hard constraint would make whole rows infeasible on a house whose load
# alone can exceed the limit -- and this one's does, repeatedly -- and an
# infeasible row propagates into an all-infinite value function that says
# nothing. A penalty keeps the arithmetic finite. It is deliberately modest:
# large enough that the program will not casually charge a battery through an
# already-peaking connection, small enough that it never becomes the number the
# ranking is actually made of.
OVERLOAD_PENALTY: float = 10.0


@dataclass(frozen=True, slots=True)
class Plant:
    """The physical house: battery, inverter and connection."""

    capacity_wh: float
    charge_power_max_w: float
    discharge_power_max_w: float
    charge_efficiency: float
    discharge_efficiency: float
    soc_min: float
    soc_max: float
    wear_charge: float
    wear_discharge: float
    import_max_w: float
    export_max_w: float


@dataclass(frozen=True, slots=True)
class World:
    """The truth over the evaluation window, at a fixed timestep."""

    step_hours: float
    pv_w: np.ndarray
    load_w: np.ndarray
    buy: np.ndarray
    sell: np.ndarray
    import_max_w: float
    """What the connection may draw before :data:`OVERLOAD_PENALTY` applies.
    Not the configured EMHASS limit on its own: the recorded load is proof of
    what the house actually drew, and an hour it demonstrably imported 11 kW in
    is not one to charge a fictional penalty for."""

    def __len__(self) -> int:
        return len(self.pv_w)


class Dispatcher:
    """One SOC grid and its transition physics, reused across a whole window.

    The physics of a transition depend only on *how far* the SOC moves, never
    on where it started, so the whole (from, to) cost matrix is one short
    vector indexed by the jump -- and the jump is bounded by the inverter, to
    about a fifth of the battery per half hour here. That turns each timestep
    of the program from a 201x201 matrix into a 201x93 sliding window, which is
    what makes replaying two and a half years of half hours a minute's work
    rather than an afternoon's.
    """

    def __init__(self, plant: Plant, step_hours: float, levels: int = 201) -> None:
        self.plant = plant
        self.step_hours = step_hours
        self.soc = np.linspace(plant.soc_min, plant.soc_max, levels)
        level_wh = (plant.soc_max - plant.soc_min) / (levels - 1) * plant.capacity_wh

        reach = (
            max(
                plant.charge_power_max_w * plant.charge_efficiency,
                plant.discharge_power_max_w / plant.discharge_efficiency,
            )
            * step_hours
        )
        self.span = min(levels - 1, int(np.ceil(reach / level_wh)))
        jumps = np.arange(-self.span, self.span + 1)

        delta_wh = jumps * level_wh
        # Losses fall where the energy crosses: charging draws more from AC
        # than it stores, discharging delivers less to AC than it takes out.
        charge_w = np.where(delta_wh > 0, delta_wh / (plant.charge_efficiency * step_hours), 0.0)
        discharge_w = np.where(
            delta_wh < 0, -delta_wh * plant.discharge_efficiency / step_hours, 0.0
        )
        self._p_ac = charge_w - discharge_w
        self._wear = (
            (charge_w * plant.wear_charge + discharge_w * plant.wear_discharge) * step_hours / 1000
        )
        self._blocked = (charge_w > plant.charge_power_max_w + 1e-6) | (
            discharge_w > plant.discharge_power_max_w + 1e-6
        )

    def _cost(self, world: World, index: int) -> np.ndarray:
        """Cost of every jump at one timestep, in currency."""
        plant = self.plant
        grid_w = (world.load_w[index] - world.pv_w[index]) + self._p_ac
        imported = np.maximum(grid_w, 0.0)
        # Surplus above the connection limit is curtailed rather than sold, and
        # so is everything in an hour that pays nothing to export: a house can
        # always throw sun away, and never pays to give it away.
        exported = np.minimum(np.maximum(-grid_w, 0.0), plant.export_max_w)

        cost = imported * world.buy[index] - exported * max(world.sell[index], 0.0)
        cost += np.maximum(imported - world.import_max_w, 0.0) * OVERLOAD_PENALTY
        cost = cost * self.step_hours / 1000 + self._wear
        return np.where(self._blocked, np.inf, cost)

    def _sweep(self, value: np.ndarray, kernel: np.ndarray) -> np.ndarray:
        """One min-plus pass: ``out[i] = min_d kernel[d] + value[i + d]``.

        Padding with infinity is what keeps the battery inside its own range:
        a jump off either end of the grid is simply unaffordable.
        """
        padded = np.full(len(value) + 2 * self.span, np.inf)
        padded[self.span : self.span + len(value)] = value
        window = np.lib.stride_tricks.sliding_window_view(padded, len(kernel))
        return np.min(window + kernel[None, :], axis=1)

    def cost_to_go(self, world: World, start: int, residual_value: float) -> np.ndarray:
        """Cheapest cost from every SOC at ``start`` to the end of the window.

        ``residual_value`` prices whatever is still in the battery at the far
        end, per kWh above ``soc_min``. Without it the program would empty the
        battery into the last hour whatever the hour was worth.
        """
        plant = self.plant
        value = -(self.soc - plant.soc_min) * plant.capacity_wh / 1000 * residual_value
        for index in range(len(world) - 1, start - 1, -1):
            value = self._sweep(value, self._cost(world, index))
        return value

    def cost_to_reach(self, world: World, soc_init: float, end: int) -> np.ndarray:
        """Cheapest cost of arriving at every SOC at ``end``, starting at ``soc_init``."""
        cost = np.full_like(self.soc, np.inf)
        cost[int(np.argmin(np.abs(self.soc - soc_init)))] = 0.0
        for index in range(end):
            # Arriving is the same walk read backwards: where the value pass
            # asks what a jump leads to, this asks what it came from.
            cost = self._sweep(cost, self._cost(world, index)[::-1])
        return cost

    def evaluate(self, total: np.ndarray, pin: float) -> float:
        """What pinning the horizon's end at ``pin`` costs over the window.

        A pin between grid points is interpolated; one the battery cannot
        reach is charged at the nearest it can, which is what EMHASS 0.18 does
        with an unreachable soft target rather than refusing the run.
        """
        reachable = np.isfinite(total)
        if not reachable.any():
            return float("inf")
        soc = self.soc[reachable]
        costs = total[reachable]
        return float(np.interp(pin, soc, costs))

"""Turn this house's recorded history into replayable end-SOC decisions.

Each scenario is one real moment: a real spot curve, a real day of sun and a
real household load, cut into the two halves a terminal-SOC heuristic lives
between --

* what the candidate is *allowed to see* at that moment, which is not the
  truth. Nordpool publishes tomorrow around 13:00 local, so a run at 09:00 has
  a price curve that stops tonight; the borrowed load series stops at the
  horizon; and the load forecast is a rolling average of the last week rather
  than what the house actually did.
* what then *actually happened*, which is what the dispatch program is scored
  against.

Getting that split right is most of the point. A backtest that hands the
candidate the truth measures nothing an operator will ever experience -- and
the whole reason the shipping rule refuses to act on a proxied price is that
the truth is exactly what it does not have.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from custom_components.emhass_companion.models import BatteryConfig, GridConfig, Point, Series

from .dispatch import Plant, World

LOCAL = ZoneInfo("Europe/Stockholm")
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

DATA = Path(__file__).parent / "data" / "house.csv"

# Nordpool's day-ahead auction clears around 13:00 local; before that, a run
# can see no further than the end of today.
PUBLICATION_HOUR = 13

# How much load history the "forecast" averages over. Seven days is what a
# same-hour rolling mean needs to have seen every weekday once.
FORECAST_DAYS = 7


@dataclass(frozen=True, slots=True)
class Tariff:
    """This house's own: buy is a marked-up spot, sell is spot."""

    buy_multiplier: float = 1.25
    buy_adder: float = 0.80
    sell_multiplier: float = 1.0
    sell_adder: float = 0.0

    def buy(self, spot: float) -> float:
        return spot * self.buy_multiplier + self.buy_adder

    def sell(self, spot: float) -> float:
        return spot * self.sell_multiplier + self.sell_adder


@dataclass(slots=True)
class House:
    """Every parameter the live entry carries, in one place."""

    battery: BatteryConfig
    grid: GridConfig
    tariff: Tariff = field(default_factory=Tariff)

    def plant(self) -> Plant:
        battery = self.battery
        return Plant(
            capacity_wh=battery.capacity_wh,
            charge_power_max_w=battery.charge_power_max_w,
            discharge_power_max_w=battery.discharge_power_max_w,
            charge_efficiency=battery.charge_efficiency,
            discharge_efficiency=battery.discharge_efficiency,
            soc_min=battery.soc_min,
            soc_max=battery.soc_max,
            wear_charge=battery.weight_battery_charge,
            wear_discharge=battery.weight_battery_discharge,
            import_max_w=self.grid.import_max_w,
            export_max_w=self.grid.export_max_w,
        )


def live_house() -> House:
    """The Yellow's actual configuration, as of the config entry read 2026-08-12."""
    return House(
        battery=BatteryConfig(
            enabled=True,
            capacity_wh=22_000,
            charge_power_max_w=10_000,
            discharge_power_max_w=10_000,
            soc_min=0.0,
            soc_max=1.0,
            soc_target=0.20,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            weight_battery_charge=0.0,
            weight_battery_discharge=0.04,
        ),
        grid=GridConfig(import_max_w=9_000, export_max_w=9_000),
    )


@dataclass(frozen=True, slots=True)
class Dataset:
    """Hourly truth: spot price, PV production and household load."""

    times: tuple[datetime, ...]
    spot: np.ndarray
    pv_w: np.ndarray
    load_w: np.ndarray

    @classmethod
    def load(cls, path: Path = DATA) -> Dataset:
        times: list[datetime] = []
        spot: list[float] = []
        pv: list[float] = []
        load: list[float] = []
        with path.open() as handle:
            for row in csv.DictReader(handle):
                times.append(datetime.fromisoformat(row["time"]).astimezone(UTC))
                spot.append(float(row["spot"]))
                pv.append(max(0.0, float(row["pv_w"])))
                load.append(max(0.0, float(row["load_w"])))
        return cls(tuple(times), np.array(spot), np.array(pv), np.array(load))

    def index(self) -> dict[datetime, int]:
        return {when: position for position, when in enumerate(self.times)}


@dataclass(slots=True)
class Scenario:
    """One decision moment, with both halves of it."""

    name: str
    now: datetime
    horizon_end: datetime
    step: timedelta
    soc_init: float
    house: House
    world: World
    """The truth over the whole evaluation window, at ``step`` resolution."""
    pv: Series
    load: Series
    buy_price: Series
    sell_price: Series
    """What the candidate is allowed to see."""
    season: str
    tags: tuple[str, ...] = ()

    @property
    def pin_index(self) -> int:
        return int((self.horizon_end - self.now) / self.step)


def _season(when: datetime) -> str:
    month = when.astimezone(LOCAL).month
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 10, 11):
        return "shoulder"
    return "summer"


def _hold(values: np.ndarray, per_hour: int) -> np.ndarray:
    """Hourly values at a finer step, held flat -- the shape a forecast has."""
    return np.repeat(values, per_hour)


def build_scenario(
    dataset: Dataset,
    index: dict[datetime, int],
    *,
    now: datetime,
    house: House,
    soc_init: float,
    horizon: timedelta = timedelta(hours=24),
    window: timedelta = timedelta(hours=72),
    step: timedelta = timedelta(minutes=30),
    pv_forecast_bias: float = 1.0,
    tags: tuple[str, ...] = (),
) -> Scenario | None:
    """One scenario, or ``None`` when the history around ``now`` has a hole."""
    per_hour = int(HOUR / step)
    start = now - FORECAST_DAYS * DAY
    hours = int(window / HOUR)
    needed = [start + i * HOUR for i in range(FORECAST_DAYS * 24 + hours)]
    if any(when not in index for when in needed):
        return None
    positions = np.array([index[when] for when in needed])
    spot = dataset.spot[positions]
    pv = dataset.pv_w[positions]
    load = dataset.load_w[positions]

    offset = FORECAST_DAYS * 24  # where `now` sits in the arrays above
    truth = slice(offset, offset + hours)
    world = World(
        step_hours=step.total_seconds() / 3600,
        pv_w=_hold(pv[truth], per_hour),
        load_w=_hold(load[truth], per_hour),
        buy=_hold(np.array([house.tariff.buy(value) for value in spot[truth]]), per_hour),
        sell=_hold(np.array([house.tariff.sell(value) for value in spot[truth]]), per_hour),
        import_max_w=max(house.grid.import_max_w, float(np.max(load[truth]))),
    )

    local_now = now.astimezone(LOCAL)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    # Tomorrow's curve lands at the day-ahead auction; before it, the run sees
    # only today. Either way the series starts at local midnight, which is how
    # the Nordpool profile actually publishes it.
    published_until = midnight + DAY * (2 if local_now.hour >= PUBLICATION_HOUR else 1)

    def series(at: datetime, values: list[float]) -> Series:
        return Series(Point(at + i * HOUR, value) for i, value in enumerate(values))

    price_hours = int((published_until - midnight) / HOUR)
    price_start_index = offset - int((now - midnight) / HOUR)
    price_spot = spot[price_start_index : price_start_index + price_hours]
    buy_price = series(midnight, [house.tariff.buy(value) for value in price_spot])
    sell_price = series(midnight, [house.tariff.sell(value) for value in price_spot])

    # Solcast publishes four days out, so the PV tail is genuinely covered --
    # but a forecast is not the truth, and `pv_forecast_bias` is how a scenario
    # says "the day was sunnier/duller than the forecast said". It starts at
    # local midnight, like the Solcast profile's own "today" attribute: a rule
    # that compares whole days (the daily-yield template does) would otherwise
    # be handed a today that begins at lunchtime.
    pv_series = series(
        midnight,
        [value * pv_forecast_bias for value in pv[price_start_index : offset + hours]],
    )

    # The live entry forecasts load inside EMHASS, so what reaches the
    # heuristic is last plan's `p_load` column: one horizon long, no tail. Its
    # values are a forecast, so the same-hour mean of the last week stands in
    # for one -- never the truth, and wrong in exactly the direction a real
    # forecast is wrong.
    horizon_hours = int(horizon / HOUR)
    profile = np.array(
        [
            float(
                np.mean([load[offset + hour - 24 * back] for back in range(1, FORECAST_DAYS + 1)])
            )
            for hour in range(horizon_hours)
        ]
    )
    load_series = series(now, list(profile))

    return Scenario(
        name=f"{local_now:%Y-%m-%d %H:%M} soc{soc_init:.0%}",
        now=now,
        horizon_end=now + horizon,
        step=step,
        soc_init=soc_init,
        house=house,
        world=world,
        pv=pv_series,
        load=load_series,
        buy_price=buy_price,
        sell_price=sell_price,
        season=_season(now),
        tags=tags,
    )

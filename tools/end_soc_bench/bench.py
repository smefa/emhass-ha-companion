"""Replay every end-SOC candidate over years of this house's own history.

    ./.venv-test/bin/python -m tools.end_soc_bench.bench --season winter

For each decision moment the dispatch program is solved once, which gives the
cost of *every* possible pin; each candidate then costs a lookup, and the best
pin available is the oracle. What is reported is regret -- kronor lost against
a controller that knew the future -- averaged over the moments, and the tail of
that distribution, because a terminal rule earns its keep by not being badly
wrong on the days that matter rather than by being marginally right on the many
days where the pin barely moves the bill.

Alongside the candidates run the two modes a user can pick instead
(``same_as_start`` and the fixed reserve) and a ``perfect`` row, which is the
oracle's own pin scored through the same path -- it should read zero, and says
so when the harness is wrong.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import statistics as st
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from custom_components.emhass_companion import terminal
from custom_components.emhass_companion.models import Series

from .dispatch import Dispatcher
from .proposals import load as load_proposals
from .variants import load as load_variants
from .world import LOCAL, Dataset, House, Scenario, build_scenario, live_house

BASELINE_START = "mode:same_as_start"
BASELINE_RESERVE = "mode:fixed_reserve"
BASELINE_FULL = "mode:always_full"
ORACLE = "perfect"


@dataclass(slots=True)
class Result:
    scenario: str
    season: str
    hour: int
    soc_init: float
    regret: dict[str, float]
    pins: dict[str, float]
    oracle_pin: float
    oracle_cost: float
    spread: float
    """Cost of the worst pin minus the best -- how much this moment was worth
    getting right at all. Days where it is near zero are days no rule can win
    or lose on, and they drown a mean if left unweighted."""


def moments(
    dataset: Dataset,
    *,
    hours: tuple[int, ...],
    start: datetime | None,
    end: datetime | None,
) -> Iterator[datetime]:
    for when in dataset.times:
        local = when.astimezone(LOCAL)
        if local.hour not in hours or local.minute:
            continue
        if start is not None and when < start:
            continue
        if end is not None and when >= end:
            continue
        yield when


def cost_to_go(scenario: Scenario, dispatcher: Dispatcher) -> np.ndarray:
    """The half of the program that every starting SOC at one moment shares."""
    # What a kWh still in the battery is worth at the far end of the window:
    # the import it would displace at a typical price. Two days past the pin,
    # so the choice moves every candidate together rather than any one of them.
    residual = float(np.median(scenario.world.buy)) * scenario.house.battery.discharge_efficiency
    return dispatcher.cost_to_go(scenario.world, scenario.pin_index, residual)


def evaluate(scenario: Scenario, dispatcher: Dispatcher, to_go: np.ndarray) -> Result:
    world = scenario.world
    pin = scenario.pin_index
    to_reach = dispatcher.cost_to_reach(world, scenario.soc_init, pin)
    total = to_reach + to_go

    tail = terminal._build_tail(
        soc_init=scenario.soc_init,
        battery=scenario.house.battery,
        now=scenario.now,
        horizon_end=scenario.horizon_end,
        step=scenario.step,
        pv=scenario.pv or Series.empty(),
        load=scenario.load,
        buy_price=scenario.buy_price,
        sell_price=scenario.sell_price or Series.empty(),
        grid=scenario.house.grid,
        load_source=terminal.LOAD_SOURCE_PROFILE,
    )
    # decide_end_soc only computes the active candidate, so score every
    # registered one directly against the shared tail.
    pins = {candidate.key: float(candidate.compute(tail).soc) for candidate in terminal.CANDIDATES}
    reserve = scenario.house.battery.soc_target
    pins[BASELINE_START] = scenario.soc_init
    pins[BASELINE_RESERVE] = reserve
    pins[BASELINE_FULL] = scenario.house.battery.soc_max

    reachable = np.isfinite(total)
    best_index = int(np.argmin(np.where(reachable, total, np.inf)))
    oracle_cost = float(total[best_index])
    pins[ORACLE] = float(dispatcher.soc[best_index])
    worst = float(np.max(total[reachable]))

    return Result(
        scenario=scenario.name,
        season=scenario.season,
        hour=scenario.now.astimezone(LOCAL).hour,
        soc_init=scenario.soc_init,
        regret={
            key: dispatcher.evaluate(total, value) - oracle_cost for key, value in pins.items()
        },
        pins=pins,
        oracle_pin=pins[ORACLE],
        oracle_cost=oracle_cost,
        spread=worst - oracle_cost,
    )


def run(
    *,
    house: House,
    hours: tuple[int, ...],
    socs: tuple[float, ...],
    start: datetime | None,
    end: datetime | None,
    limit: int | None,
    season: str | None,
    every: int = 1,
    levels: int = 201,
) -> list[Result]:
    dataset = Dataset.load()
    index = dataset.index()
    dispatcher = Dispatcher(house.plant(), step_hours=0.5, levels=levels)
    results: list[Result] = []
    for count, when in enumerate(moments(dataset, hours=hours, start=start, end=end)):
        if count % every:
            continue
        built = [
            scenario
            for soc in socs
            if (scenario := build_scenario(dataset, index, now=when, house=house, soc_init=soc))
        ]
        if not built or (season and built[0].season != season):
            continue
        shared = cost_to_go(built[0], dispatcher)
        for scenario in built:
            results.append(evaluate(scenario, dispatcher, shared))
        if limit and len(results) >= limit:
            break
    return results


def _calendar_season(result: Result) -> str:
    return CALENDAR_SEASONS[int(result.scenario[5:7])]


CALENDAR_SEASONS = {
    12: "winter",
    1: "winter",
    2: "winter",
    3: "spring",
    4: "spring",
    5: "spring",
    6: "summer",
    7: "summer",
    8: "summer",
    9: "autumn",
    10: "autumn",
    11: "autumn",
}

# How `report` slices the decisions: a label per result and the order to print.
GROUPINGS: dict[str, tuple[Callable[[Result], str], Callable[[list[str]], list[str]]]] = {
    "bench": (
        lambda r: r.season,
        lambda seen: [s for s in ("winter", "shoulder", "summer") if s in seen],
    ),
    "calendar": (
        _calendar_season,
        lambda seen: [s for s in ("winter", "spring", "summer", "autumn") if s in seen],
    ),
    "month": (lambda r: r.scenario[:7], sorted),
}


def report(results: list[Result], *, keys: list[str], by: str = "bench") -> None:
    if not results:
        print("no scenarios")
        return
    label, order = GROUPINGS[by]
    groups: dict[str, list[Result]] = defaultdict(list)
    for result in results:
        groups[label(result)].append(result)
        groups["ALL"].append(result)

    print(f"\n{len(results)} decisions, {len(set(r.scenario[:10] for r in results))} days")
    print(f"mean spread between best and worst pin: {st.mean(r.spread for r in results):.2f} SEK")
    for season in (*order([g for g in groups if g != "ALL"]), "ALL"):
        rows = groups.get(season)
        if not rows:
            continue
        print(f"\n--- {season} ({len(rows)} decisions) ---")
        print(
            f"{'candidate':<22}{'mean':>8}{'median':>8}{'p90':>8}{'p99':>8}{'max':>8}{'>2 SEK':>8}"
        )
        scored = sorted(keys, key=lambda key: st.mean(r.regret[key] for r in rows))
        for key in scored:
            values = sorted(r.regret[key] for r in rows)
            share = sum(1 for value in values if value > 2.0) / len(values)
            print(
                f"{key:<22}{st.mean(values):8.2f}{st.median(values):8.2f}"
                f"{values[int(0.90 * (len(values) - 1))]:8.2f}"
                f"{values[int(0.99 * (len(values) - 1))]:8.2f}"
                f"{values[-1]:8.2f}{share:8.0%}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", default="6,12,18,22", help="local decision hours")
    parser.add_argument("--socs", default="0.25,0.5,0.75", help="starting SOC values")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--season", default=None, choices=["winter", "shoulder", "summer"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--every", type=int, default=1, help="use every Nth decision moment")
    parser.add_argument("--levels", type=int, default=201, help="SOC grid resolution")
    parser.add_argument("--json", default=None, help="write per-decision results here")
    parser.add_argument(
        "--proposals", action="store_true", help="also score everything in proposals/"
    )
    parser.add_argument(
        "--variants", action="store_true", help="also score the parameter sweeps in variants.py"
    )
    parser.add_argument(
        "--by",
        default="bench",
        choices=list(GROUPINGS),
        help="bench: winter/shoulder/summer; calendar: four seasons; month: one table per month",
    )
    args = parser.parse_args()

    if args.proposals:
        terminal.CANDIDATES = terminal.CANDIDATES + load_proposals()
    if args.variants:
        terminal.CANDIDATES = terminal.CANDIDATES + load_variants()

    def moment(value: str | None) -> datetime | None:
        return None if value is None else datetime.fromisoformat(value).replace(tzinfo=UTC)

    results = run(
        house=live_house(),
        hours=tuple(int(value) for value in args.hours.split(",")),
        socs=tuple(float(value) for value in args.socs.split(",")),
        start=moment(args.start),
        end=moment(args.end),
        limit=args.limit,
        season=args.season,
        every=args.every,
        levels=args.levels,
    )
    keys = [candidate.key for candidate in terminal.CANDIDATES] + [
        BASELINE_START,
        BASELINE_RESERVE,
        BASELINE_FULL,
        ORACLE,
    ]
    report(results, keys=keys, by=args.by)
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                [
                    {
                        "scenario": r.scenario,
                        "season": r.season,
                        "hour": r.hour,
                        "soc_init": r.soc_init,
                        "regret": r.regret,
                        "pins": r.pins,
                        "oracle_cost": r.oracle_cost,
                        "spread": r.spread,
                    }
                    for r in results
                ],
                indent=1,
            )
        )


if __name__ == "__main__":
    main()

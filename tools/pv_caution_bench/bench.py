"""Decide whether PV auto-caution is worth building.

    python -m tools.pv_caution_bench.bench [--season winter] [--grid]

A forecast-accuracy test, not a cost test. Three questions, in order:

1. **Bias.** Where does actual PV land between P10 and P50, by season and
   hour? ``pos`` is 0 at P50 and 1 at P10; a calibrated P10 has about 10 % of
   hours below it.
2. **Persistence.** Do days that are behind by noon stay behind in the
   afternoon? That is the controller's whole premise; if the answer is no,
   nothing downstream matters.
3. **Controller replay.** Step through each day an hour at a time, run
   ``pv_caution.advance`` on the hours so far, blend the rest of the day with
   the bias it gives, and score the rest of the day against what happened.
   ``over`` is planned sun that did not come (the costly error), ``under`` is
   sun the plan did not dare count on (the cost of caution). ``trade`` is kWh
   of over-forecast removed per kWh of under-forecast added, relative to P50:
   a fixed blend sets the bar, and auto-caution only earns its keep if it
   trades clearly better than a fixed blend does. ``cost`` is
   ``weight * over + under`` (``--over-weight``, default 2), which ranks the
   ``--grid`` rows.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import contextlib
from dataclasses import dataclass, replace
from datetime import date, timedelta
import importlib.util
import itertools
import math
from pathlib import Path
import statistics as st
import sys
from types import ModuleType

from .data import HOUR, LOCAL, Dataset, Day, build

ROOT = Path(__file__).resolve().parents[2]


def _load_controller() -> ModuleType:
    # By path, not as a package import: the integration's __init__ pulls in
    # Home Assistant, and this bench should run anywhere Python does.
    path = ROOT / "custom_components" / "emhass_companion" / "pv_caution.py"
    spec = importlib.util.spec_from_file_location("pv_caution", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pv_caution = _load_controller()


# -- 1. Bias -----------------------------------------------------------------


def test_bias(data: Dataset) -> None:
    print("\n## 1. Bias (sunny hours, actual normalised)\n")
    groups: dict[tuple[str, int], list] = defaultdict(list)
    for day in data.days:
        for hour in day.hours:
            if day.sunny(hour) and not hour.curtailed:
                groups[(day.season, hour.local.hour)].append(hour)
                groups[(day.season, -1)].append(hour)
                groups[("all", -1)].append(hour)

    print(
        f"{'season':8} {'hour':>4} {'n':>6} {'a/P50':>6} {'a/P10':>6} {'pos':>6}"
        f" {'<P10':>6} {'>P90':>6}"
    )
    for (season, hour), rows in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        p50 = sum(r.p50 for r in rows)
        p10 = sum(r.p10 for r in rows)
        actual = sum(r.actual for r in rows)
        gap = p50 - p10
        pos = (p50 - actual) / gap if gap > 0 else math.nan
        below = sum(r.actual < r.p10 for r in rows) / len(rows)
        above = sum(r.actual > r.p90 for r in rows) / len(rows)
        label = "day" if hour < 0 else f"{hour:02d}"
        print(
            f"{season:8} {label:>4} {len(rows):6d} {actual / p50:6.3f} {actual / max(p10, 1e-9):6.3f}"
            f" {pos:6.2f} {below:6.1%} {above:6.1%}"
        )


# -- 2. Persistence ----------------------------------------------------------


@dataclass(slots=True)
class Split:
    season: str
    morning: float
    afternoon: float
    afternoon_over: float
    """Share of afternoon P50 energy that did not arrive."""


def _split(day: Day) -> Split | None:
    if day.sunrise is None:
        return None
    morning_from = day.sunrise + HOUR
    morning = [
        h
        for h in day.hours
        if day.sunny(h) and not h.curtailed and h.start >= morning_from and h.local.hour < 12
    ]
    afternoon = [h for h in day.hours if day.sunny(h) and not h.curtailed and h.local.hour >= 12]
    if len(morning) < 2 or len(afternoon) < 2:
        return None
    m_plan = sum(h.p50 for h in morning)
    a_plan = sum(h.p50 for h in afternoon)
    return Split(
        season=day.season,
        morning=sum(h.actual for h in morning) / m_plan,
        afternoon=sum(h.actual for h in afternoon) / a_plan,
        afternoon_over=sum(max(h.p50 - h.actual, 0.0) for h in afternoon) / a_plan,
    )


def test_persistence(data: Dataset, behind: float) -> None:
    print(f"\n## 2. Persistence (behind = morning ratio < {behind})\n")
    splits = [s for s in map(_split, data.days) if s is not None]
    by_season: dict[str, list[Split]] = defaultdict(list)
    for split in splits:
        by_season[split.season].append(split)
        by_season["all"].append(split)

    print(
        f"{'season':8} {'days':>5} {'behind':>6}  {'aft ratio behind/rest':>22}"
        f"  {'aft over behind/rest':>21}  {'r(log)':>6}"
    )
    for season, rows in sorted(by_season.items()):
        late = [s for s in rows if s.morning < behind]
        rest = [s for s in rows if s.morning >= behind]
        r = math.nan
        with contextlib.suppress(st.StatisticsError):
            r = st.correlation(
                [math.log(max(s.morning, 1e-3)) for s in rows],
                [math.log(max(s.afternoon, 1e-3)) for s in rows],
            )

        def med(xs: list[float]) -> str:
            return f"{st.median(xs):5.2f}" if xs else "  -  "

        print(
            f"{season:8} {len(rows):5d} {len(late) / len(rows):6.1%}"
            f"  {med([s.afternoon for s in late]):>10} / {med([s.afternoon for s in rest]):<9}"
            f"  {med([s.afternoon_over for s in late]):>10} / {med([s.afternoon_over for s in rest]):<8}"
            f"  {r:6.2f}"
        )
    print("\n(medians; r is the correlation of log morning vs log afternoon ratio)")


# -- 3. Controller replay ----------------------------------------------------


@dataclass(slots=True)
class Score:
    decisions: int = 0
    over_wh: float = 0.0
    under_wh: float = 0.0
    bias_sum: float = 0.0

    def add(self, over: float, under: float, bias: float) -> None:
        self.decisions += 1
        self.over_wh += over
        self.under_wh += under
        self.bias_sum += bias

    @property
    def over(self) -> float:
        return self.over_wh / 1000 / max(self.decisions, 1)

    @property
    def under(self) -> float:
        return self.under_wh / 1000 / max(self.decisions, 1)

    @property
    def mean_bias(self) -> float:
        return self.bias_sum / max(self.decisions, 1)


Strategy = float | pv_caution.CautionConfig  # a fixed bias, or auto-caution


def replay(day: Day, strategy: Strategy, all_: Score, afternoon: Score) -> None:
    """Score one day's hourly decisions under ``strategy``."""
    if day.sunrise is None:
        return
    last_sun = max(h.start for h in day.hours if day.sunny(h))
    samples = [
        pv_caution.Sample(
            start=h.start,
            duration=HOUR,
            planned_w=h.p50,
            actual_w=h.actual,
            curtailed=h.curtailed,
        )
        for h in day.hours
    ]
    state = pv_caution.CautionState()
    for hour in day.hours:
        now = hour.local
        if isinstance(strategy, float):
            bias = strategy
        else:
            # ``advance`` only looks at samples that have ended by ``now``.
            state = pv_caution.advance(
                state, samples, now=now, peak_w=day.peak, sunrise=day.sunrise, config=strategy
            )
            bias = state.bias
        if hour.start < day.sunrise + HOUR or hour.start > last_sun:
            continue
        over = under = 0.0
        for rest in day.hours:
            if rest.start < hour.start or rest.p50 <= 0 or rest.curtailed:
                continue
            estimate = pv_caution.blend(rest.p50, rest.p10, bias)
            over += max(estimate - rest.actual, 0.0)
            under += max(rest.actual - estimate, 0.0)
        all_.add(over, under, bias)
        if now.hour >= 12:
            afternoon.add(over, under, bias)


def _label(strategy: Strategy) -> str:
    if isinstance(strategy, float):
        return {0.0: "P50", 1.0: "P10"}.get(strategy, f"fixed {strategy:.2f}")
    ahead = "never" if math.isinf(strategy.ahead) else f"{strategy.ahead:.2f}"
    return (
        f"auto w{strategy.window / HOUR:.0f}h s{strategy.step:.2f} b{strategy.behind:.2f} a{ahead}"
    )


def _row(label: str, score: Score, base: Score, weight: float) -> str:
    d_over = score.over - base.over
    d_under = score.under - base.under
    trade = -d_over / d_under if d_under > 1e-9 else math.nan
    return (
        f"{label:34} {score.over:6.2f} {score.under:6.2f} {weight * score.over + score.under:6.2f}"
        f" {d_over / max(base.over, 1e-9):+7.1%} {d_under / max(base.under, 1e-9):+7.1%}"
        f" {trade:6.2f} {score.mean_bias:5.2f}"
    )


def test_replay(data: Dataset, strategies: list[Strategy], top: int | None, weight: float) -> None:
    print("\n## 3. Controller replay (kWh per decision, rest of day)\n")
    results: list[tuple[str, Score, Score]] = []
    for strategy in strategies:
        all_, afternoon = Score(), Score()
        for day in data.days:
            replay(day, strategy, all_, afternoon)
        results.append((_label(strategy), all_, afternoon))

    base_all, base_pm = results[0][1], results[0][2]
    header = (
        f"{'strategy':34} {'over':>6} {'under':>6} {'cost':>6} {'dOver':>7} {'dUnder':>7}"
        f" {'trade':>6} {'bias':>5}"
    )
    fixed = [r for r in results if not r[0].startswith("auto")]
    auto = [r for r in results if r[0].startswith("auto")]
    for title, pick, base in (
        ("All decisions", lambda r: r[1], base_all),
        ("Afternoon decisions (from 12:00)", lambda r: r[2], base_pm),
    ):
        print(f"### {title}  (n = {pick(results[0]).decisions})\n")
        print(header)
        for result in fixed:
            print(_row(result[0], pick(result), base, weight))
        ranked = sorted(auto, key=lambda r: weight * pick(r).over + pick(r).under)
        for result in ranked[:top] if top else ranked:
            print(_row(result[0], pick(result), base, weight))
        print()


def _strategies(grid: bool) -> list[Strategy]:
    fixed: list[Strategy] = [0.0, 1.0, 0.25, 0.5]
    base = pv_caution.CautionConfig()
    if not grid:
        return [*fixed, base, replace(base, ahead=math.inf)]
    auto = [
        replace(base, window=timedelta(hours=w), step=s, behind=b, ahead=a)
        for w, s, b, a in itertools.product(
            (2, 3, 4), (0.05, 0.1, 0.2), (0.75, 0.85, 0.9), (1.0, math.inf)
        )
    ]
    return [*fixed, *auto]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solcast", type=Path, help="solcast.json (default: data/)")
    parser.add_argument("--house", type=Path, help="house.csv (default: end_soc_bench/data/)")
    parser.add_argument("--season", choices=["winter", "spring", "summer", "autumn"])
    parser.add_argument("--since", type=date.fromisoformat, help="first local day, YYYY-MM-DD")
    parser.add_argument("--until", type=date.fromisoformat, help="last local day, YYYY-MM-DD")
    parser.add_argument(
        "--curtail-below-spot",
        type=float,
        metavar="PRICE",
        help="treat hours with spot at or below this as curtailed (e.g. 0)",
    )
    parser.add_argument(
        "--scale", type=float, help="actual/forecast scale (default: measured overall ratio)"
    )
    parser.add_argument("--behind", type=float, default=0.85, help="test 2 split")
    parser.add_argument("--grid", action="store_true", help="tune window/step/thresholds")
    parser.add_argument("--top", type=int, default=12, help="auto rows shown with --grid")
    parser.add_argument(
        "--over-weight",
        type=float,
        default=2.0,
        help="cost of 1 kWh over-forecast in kWh of under-forecast (ranks the grid)",
    )
    parser.add_argument("--only", choices=["1", "2", "3"], help="run one test")
    args = parser.parse_args()

    paths = {k: v for k, v in (("solcast", args.solcast), ("house", args.house)) if v}
    data = build(
        scale=args.scale,
        since=args.since,
        until=args.until,
        curtail_below_spot=args.curtail_below_spot,
        **paths,
    )
    if args.season:
        data.days = [day for day in data.days if day.season == args.season]
    first, last = data.days[0].date, data.days[-1].date
    print(f"# PV caution backtest: {len(data.days)} days, {first} .. {last} ({LOCAL})")
    print(f"raw actual/P50 over sunny hours: {data.scale:.3f} (actual divided by this)")
    print(f"dropped days: {data.dropped}")
    print(f"curtailment: {data.curtailment}")

    if args.only in (None, "1"):
        test_bias(data)
    if args.only in (None, "2"):
        test_persistence(data, args.behind)
    if args.only in (None, "3"):
        test_replay(data, _strategies(args.grid), args.top if args.grid else None, args.over_weight)


if __name__ == "__main__":
    main()

"""Line up Solcast's forecast history with what the panels actually made.

Forecast comes from the Solcast integration's own cache
(``/config/solcast_solar/solcast.json``): per site, a list of 30-minute periods
with ``pv_estimate``/``pv_estimate10``/``pv_estimate90`` in kW. Sites are
summed and the periods averaged to hours so they meet the actual PV, which is
the hourly mean of the production sensor in ``end_soc_bench``'s ``house.csv``.

The cache keeps one value per period -- the last forecast issued before it --
so what is replayed here is close to a nowcast, not what a morning plan saw.
Errors are smaller than live and P10 sits closer to P50. See the README.
"""

from __future__ import annotations

from collections import defaultdict
import csv
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
import itertools
import json
from pathlib import Path
from zoneinfo import ZoneInfo

LOCAL = ZoneInfo("Europe/Stockholm")
HOUR = timedelta(hours=1)

HERE = Path(__file__).parent
SOLCAST = HERE / "data" / "solcast.json"
SOC = HERE / "data" / "soc.csv"
HOUSE = HERE.parent / "end_soc_bench" / "data" / "house.csv"

# Planned PV below this share of the day's planned peak is not "real sun":
# dawn, dusk and heavy overcast, where a ratio is noise.
SUN_SHARE = 0.10

SEASONS = {
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


@dataclass(slots=True)
class Hour:
    start: datetime
    """UTC."""
    p50: float
    p10: float
    p90: float
    actual: float
    curtailed: bool = False

    @property
    def local(self) -> datetime:
        return self.start.astimezone(LOCAL)


@dataclass(slots=True)
class Day:
    date: date
    hours: list[Hour]
    peak: float = field(init=False)
    sunrise: datetime | None = field(init=False)

    def __post_init__(self) -> None:
        self.peak = max((hour.p50 for hour in self.hours), default=0.0)
        self.sunrise = next((hour.start for hour in self.hours if hour.p50 > 0), None)

    @property
    def season(self) -> str:
        return SEASONS[self.date.month]

    def sunny(self, hour: Hour) -> bool:
        return hour.p50 > 0 and hour.p50 >= self.peak * SUN_SHARE


def _parse_time(value: str | float) -> datetime:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, UTC)
    when = datetime.fromisoformat(value)
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def load_solcast(path: Path = SOLCAST) -> dict[datetime, tuple[float, float, float]]:
    """Hourly (P50, P10, P90) in W, summed over every site.

    An hour is kept only if every site covers every period in it; a partial sum
    would read as a forecast of less sun.
    """
    raw = json.loads(path.read_text())
    sites = raw.get("siteinfo", raw)
    periods: dict[datetime, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for site in sites.values():
        if not isinstance(site, dict) or "forecasts" not in site:
            continue
        for row in site["forecasts"]:
            slot = periods[_parse_time(row["period_start"])]
            slot[0] += float(row["pv_estimate"])
            slot[1] += float(row["pv_estimate10"])
            slot[2] += float(row["pv_estimate90"])
            slot[3] += 1
    n_sites = max((int(slot[3]) for slot in periods.values()), default=0)
    if not n_sites:
        raise SystemExit(f"No forecasts found in {path}")

    starts = sorted(periods)
    step = min((b - a for a, b in itertools.pairwise(starts) if b > a), default=HOUR)
    per_hour = max(1, round(HOUR / step))

    hours: dict[datetime, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for start, (p50, p10, p90, count) in periods.items():
        if count != n_sites:
            continue
        slot = hours[start.replace(minute=0, second=0, microsecond=0)]
        slot[0] += p50
        slot[1] += p10
        slot[2] += p90
        slot[3] += 1
    return {
        start: (p50 / per_hour * 1000, p10 / per_hour * 1000, p90 / per_hour * 1000)
        for start, (p50, p10, p90, count) in hours.items()
        if count == per_hour
    }


def load_house(path: Path = HOUSE) -> dict[datetime, tuple[float, float]]:
    """Hourly (actual PV in W, spot price) from ``house.csv``."""
    with path.open(newline="") as handle:
        return {
            _parse_time(row["time"]): (float(row["pv_w"]), float(row["spot"]))
            for row in csv.DictReader(handle)
        }


def load_curtailed(path: Path = SOC, threshold: float = 97.0) -> set[datetime] | None:
    """Hours the battery touched ``threshold`` % SOC, or ``None`` without data.

    With the battery full, surplus sun is either exported or, past the export
    limit, thrown away -- so low output in those hours is not a forecast miss.
    Dropping every near-full hour loses some honest misses too; that is the
    cheaper error.
    """
    if not path.exists():
        return None
    with path.open(newline="") as handle:
        return {
            _parse_time(row["time"])
            for row in csv.DictReader(handle)
            if float(row["soc_max"]) >= threshold
        }


@dataclass(slots=True)
class Dataset:
    days: list[Day]
    scale: float
    """Raw actual/P50 over sunny hours; actual has been divided by this."""
    dropped: dict[str, int]
    curtailment: str


def build(
    *,
    solcast: Path = SOLCAST,
    house: Path = HOUSE,
    soc: Path = SOC,
    scale: float | None = None,
    since: date | None = None,
    until: date | None = None,
    curtail_below_spot: float | None = None,
) -> Dataset:
    """Every complete day, with actual PV normalised to the forecast's level.

    The production sensor is DC-side, a few percent above what reaches the AC
    bus the forecast describes. Dividing by the overall actual/P50 ratio (or a
    given ``scale``) removes that offset so what is left is the forecast's
    shape of error, which is what caution can act on.

    ``curtail_below_spot`` marks every hour with spot at or below it as
    curtailed. EMHASS only curtails when export does not pay, and this house
    sells at spot, so hours with spot <= 0 are where curtailment could have
    happened -- a superset, since it also drops hours where it did not.
    """
    forecast = load_solcast(solcast)
    house_rows = load_house(house)
    actual = {start: pv for start, (pv, _) in house_rows.items()}
    curtailed = load_curtailed(soc)

    by_day: dict[date, list[datetime]] = defaultdict(list)
    for start in forecast:
        by_day[start.astimezone(LOCAL).date()].append(start)

    dropped = {"missing_actual": 0, "no_sun": 0, "sensor_dead": 0}
    days: list[Day] = []
    for day, starts in sorted(by_day.items()):
        if (since and day < since) or (until and day > until):
            continue
        starts.sort()
        if not any(forecast[start][0] > 0 for start in starts):
            dropped["no_sun"] += 1
            continue
        if any(forecast[start][0] > 0 and start not in actual for start in starts):
            dropped["missing_actual"] += 1
            continue
        hours = [
            Hour(
                start=start,
                p50=forecast[start][0],
                p10=forecast[start][1],
                p90=forecast[start][2],
                actual=max(actual.get(start, 0.0), 0.0),
                curtailed=(curtailed is not None and start in curtailed)
                or (
                    curtail_below_spot is not None
                    and start in house_rows
                    and house_rows[start][1] <= curtail_below_spot
                ),
            )
            for start in starts
        ]
        planned = sum(hour.p50 for hour in hours)
        made = sum(hour.actual for hour in hours)
        # A whole day at zero against a real forecast is a dead sensor or
        # inverter, not weather; even snow-covered panels leak something.
        if made < 0.01 * planned and planned > 1000:
            dropped["sensor_dead"] += 1
            continue
        days.append(Day(date=day, hours=hours))

    if scale is None:
        sunny = [h for day in days for h in day.hours if day.sunny(h) and not h.curtailed]
        scale = sum(hour.actual for hour in sunny) / max(sum(hour.p50 for hour in sunny), 1e-9)
    for day in days:
        for hour in day.hours:
            hour.actual /= scale
    notes = []
    if curtailed is not None:
        notes.append("near-full SOC hours excluded")
    if curtail_below_spot is not None:
        notes.append(f"hours with spot <= {curtail_below_spot} excluded")
    curtailment = ", ".join(notes) or "unknown (curtailed hours count as misses)"
    return Dataset(days=days, scale=scale, dropped=dropped, curtailment=curtailment)

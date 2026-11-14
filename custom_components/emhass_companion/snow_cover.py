"""Notice snow on the panels and scale the PV forecast down while it lasts.

A PV forecast knows about clouds, not about snow lying on the panels. With the
panels covered the forecast stays high while the output is near zero, and the
plan counts on energy that will not arrive. Blending the live value into the
current step corrects one step; this module keeps a *persistent* state that
scales the forecast for the whole horizon until the panels are clear again.

States:

* ``clear``     -- nothing is done to the forecast.
* ``suspected`` -- snow was seen falling in the cold, or a covered state is
  no longer trusted (thaw, rain). The forecast is scaled by a constant factor
  until daylight measurement confirms either way.
* ``covered``   -- measured output is far below the raw forecast. The forecast
  is scaled by the measured ratio.

What decides:

* Detection is measured: energy-weighted actual over *raw* forecast across the
  qualifying sun of the last few hours (``pv_caution.window_ratio``). The ratio
  is always taken against the raw forecast, never the scaled one, or the
  correction would hide the gap that justified it.
* Detection is gated on the *recent minimum* outdoor temperature, not the
  current one: snow stays on panels after the air warms. Outside the gate a
  shortfall is never treated as snow, so a summer inverter fault or shading
  cannot trigger it.
* With a weather entity, detection also needs snowfall to have been reported
  in the cold since the panels last produced normally. Cold alone is not
  enough: fog the forecast missed looks the same in the measurement. The
  snowfall is remembered, not read from the current weather, so a clear
  morning after a snowy night still counts. Without a weather entity the
  temperature gate stands alone.
* Release is measured too and is not temperature gated -- snow slides off
  abruptly.
* A manual override (``set_mode``) can force ``covered`` or ``clear``.

None of the thresholds below has been backtested; they are first guesses.

Pure computation; reading sensors and persisting the state is the
coordinator's job.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .pv_caution import CautionConfig, Sample, window_ratio

# -- states, modes, sources ------------------------------------------------------

STATE_CLEAR = "clear"
STATE_SUSPECTED = "suspected"
STATE_COVERED = "covered"

MODE_AUTO = "auto"
MODE_COVERED = "covered"
MODE_CLEAR = "clear"
MODES = (MODE_AUTO, MODE_COVERED, MODE_CLEAR)

SOURCE_MEASUREMENT = "measurement"
SOURCE_WEATHER = "weather"
SOURCE_MANUAL = "manual"

# Weather entity conditions that count as snowfall / rain.
SNOW_CONDITIONS = frozenset({"snowy", "snowy-rainy"})
RAIN_CONDITIONS = frozenset({"rainy", "pouring", "lightning-rainy"})

# -- thresholds (untuned: not backtested) ----------------------------------------

COVERED_RATIO = 0.20  # Actual/raw forecast below this over the window means covered.
RELEASE_RATIO = 0.60  # Actual/raw forecast above this over the window means clear.
GATE_TEMPERATURE_C = 2.0  # Detection needs the recent minimum at or below this.
GATE_LOOKBACK = timedelta(hours=48)  # How far back the recent minimum looks.
PRIME_TEMPERATURE_C = 1.0  # Snowfall at or below this primes ``suspected``.
# A weather entity heard from within this long is trusted to report snowfall, so
# an hour of it being unavailable does not fall back to temperature alone.
WEATHER_TRUSTED_FOR = GATE_LOOKBACK
THAW_TEMPERATURE_C = 2.0  # Temperature above this counts as thawing...
THAW_DURATION = timedelta(hours=3)  # ...once it has held this long.
PRIME_QUIET = timedelta(hours=1)  # Snowfall is ignored this soon after output looked healthy.
SUSPECTED_FACTOR = 0.3  # UNTUNED forecast multiplier while only suspected.
MANUAL_COVERED_FACTOR = 0.1  # UNTUNED multiplier for a manual "covered" with no measurement yet.
# Without a weather entity a manual "clear" cannot see snowfall, so it lapses after this.
CLEAR_HOLD_WITHOUT_WEATHER = timedelta(days=3)

SAMPLE_SLOT = timedelta(minutes=15)  # Shortest slot the (raw forecast, actual) samples are kept at.
SAMPLE_KEEP = timedelta(hours=6)  # Samples older than this are dropped.

# Three hours of window, at least one hour of qualifying sun, to call it covered.
DETECT_CONFIG = CautionConfig(window=timedelta(hours=3), min_coverage=timedelta(hours=1))


def _release_config(slot: timedelta) -> CautionConfig:
    """Two whole slots of sun to call the panels clear: 30 minutes at a 15-minute step.

    The window is three slots because the slot in progress never counts, so
    three always hold two finished ones.
    """
    return CautionConfig(window=3 * slot, min_coverage=2 * slot)


@dataclass(frozen=True, slots=True)
class SnowCoverState:
    """What survives between runs, restarts included."""

    state: str = STATE_CLEAR
    source: str | None = None
    since: datetime | None = None
    override: str = MODE_AUTO
    override_at: datetime | None = None
    ratio: float | None = None
    """Last measured actual/raw ratio; kept through the night for the factor."""

    last_working: datetime | None = None
    """When output last looked healthy (above ``RELEASE_RATIO``)."""

    evidence_from: datetime | None = None
    """Samples before this say nothing about the current state."""

    warm_since: datetime | None = None
    """Start of the current unbroken spell above ``THAW_TEMPERATURE_C``."""

    snow_seen: datetime | None = None
    """Last snowfall in the cold that healthy output has not answered since."""

    weather_at: datetime | None = None
    """When a weather entity last reported any condition."""

    samples: tuple[Sample, ...] = ()
    temperatures: tuple[tuple[datetime, float], ...] = ()
    """Outdoor temperature per slot over ``GATE_LOOKBACK``."""

    @property
    def active(self) -> bool:
        return self.state != STATE_CLEAR


def factor(state: SnowCoverState) -> float:
    """Multiplier for the raw PV forecast, 1.0 when nothing is scaled."""
    if state.state == STATE_SUSPECTED:
        return SUSPECTED_FACTOR
    if state.state == STATE_COVERED:
        if state.ratio is None:
            return MANUAL_COVERED_FACTOR
        return min(max(state.ratio, 0.0), 1.0)
    return 1.0


def gate_open(state: SnowCoverState, now: datetime) -> bool:
    """Whether the recent minimum temperature allows treating a shortfall as snow."""
    since = now - GATE_LOOKBACK
    return any(t >= since and value <= GATE_TEMPERATURE_C for t, value in state.temperatures)


def set_mode(state: SnowCoverState, mode: str, now: datetime) -> SnowCoverState:
    """Apply the manual override. ``auto`` removes it and keeps the state as is."""
    if mode not in MODES:
        raise ValueError(f"Unknown snow cover mode: {mode!r}")
    now = _utc(now)
    if mode == MODE_COVERED:
        return replace(
            state,
            override=mode,
            override_at=now,
            state=STATE_COVERED,
            source=SOURCE_MANUAL,
            since=now,
            # What was measured before the user spoke says nothing now: healthy
            # output from before the snowfall must not release it at once, nor
            # set the factor.
            ratio=None,
            evidence_from=now,
        )
    if mode == MODE_CLEAR:
        return replace(
            state,
            override=mode,
            override_at=now,
            state=STATE_CLEAR,
            source=None,
            since=None,
            evidence_from=now,
            snow_seen=None,
        )
    return replace(state, override=MODE_AUTO, override_at=None)


def advance(
    state: SnowCoverState,
    *,
    now: datetime,
    planned_w: float | None,
    actual_w: float | None,
    peak_w: float,
    temperature_c: float | None,
    condition: str | None,
    sunrise: datetime | None = None,
    slot: timedelta = SAMPLE_SLOT,
) -> SnowCoverState:
    """Take one run's step.

    ``planned_w`` is the *raw* forecast at ``now`` and ``actual_w`` the measured
    output; either may be ``None`` (no sample is kept). ``peak_w`` is the raw
    forecast's peak for the day. ``condition`` is the weather entity's state, or
    ``None`` when there is no weather entity (priming is then skipped, and
    detection rests on the temperature gate alone). ``slot``
    is how long one sample stands for: the optimisation step, since a sample
    arrives once per run.
    """
    now = _utc(now)
    slot = max(slot, SAMPLE_SLOT)
    state = _record(state, now, planned_w, actual_w, temperature_c, slot)

    evidence = tuple(
        s for s in state.samples if state.evidence_from is None or s.start >= state.evidence_from
    )
    detect_ratio, _ = window_ratio(
        evidence, now=now, peak_w=peak_w, sunrise=sunrise, config=DETECT_CONFIG
    )
    release_ratio, _ = window_ratio(
        evidence, now=now, peak_w=peak_w, sunrise=sunrise, config=_release_config(slot)
    )
    if detect_ratio is not None:
        state = replace(state, ratio=detect_ratio)
    working = release_ratio is not None and release_ratio >= RELEASE_RATIO
    snowing = condition in SNOW_CONDITIONS
    raining = condition in RAIN_CONDITIONS
    if working:
        state = replace(state, last_working=now)
        # Healthy output answers a snowfall only if all of it was measured
        # afterwards: the release window still holds the sun from before
        # snow that has only just covered the panels.
        answered = _release_config(slot).window
        if state.snow_seen is not None and now - state.snow_seen >= answered:
            state = replace(state, snow_seen=None)
    if condition is not None:
        state = replace(state, weather_at=now)
    if snowing and temperature_c is not None and temperature_c <= GATE_TEMPERATURE_C:
        state = replace(state, snow_seen=now)

    # -- manual override ------------------------------------------------------
    if state.override == MODE_CLEAR:
        expired = (
            condition is None
            and state.override_at is not None
            and now - state.override_at >= CLEAR_HOLD_WITHOUT_WEATHER
        )
        if not (snowing or expired):
            return state
        # What was measured before the snowfall (or the lapse) says nothing now.
        state = replace(state, override=MODE_AUTO, override_at=None, evidence_from=now)
        detect_ratio = None
    elif state.override == MODE_COVERED:
        if not working:
            return state
        return _enter_clear(replace(state, override=MODE_AUTO, override_at=None), now)

    # -- automatic ------------------------------------------------------------
    if working:
        return _enter_clear(state, now) if state.active else state

    if (
        state.state != STATE_COVERED
        and gate_open(state, now)
        and _snowfall_allows(state, now)
        and detect_ratio is not None
        and detect_ratio < COVERED_RATIO
    ):
        return replace(state, state=STATE_COVERED, source=SOURCE_MEASUREMENT, since=now)

    if state.state == STATE_COVERED:
        thawed = state.warm_since is not None and now - state.warm_since >= THAW_DURATION
        if thawed or raining:
            return _enter_suspected(state, now)
        return state

    if state.state == STATE_SUSPECTED:
        if not gate_open(state, now):
            return _enter_clear(state, now)
        return state

    quiet = state.last_working is None or now - state.last_working >= PRIME_QUIET
    if snowing and quiet and temperature_c is not None and temperature_c <= PRIME_TEMPERATURE_C:
        return _enter_suspected(state, now)
    return state


def _snowfall_allows(state: SnowCoverState, now: datetime) -> bool:
    """Whether a shortfall may be snow as far as reported snowfall goes.

    A weather entity that is reporting must have reported snowfall; with no
    weather entity there is nothing to ask.
    """
    if state.snow_seen is not None:
        return True
    return state.weather_at is None or now - state.weather_at > WEATHER_TRUSTED_FOR


def _enter_clear(state: SnowCoverState, now: datetime) -> SnowCoverState:
    return replace(
        state,
        state=STATE_CLEAR,
        source=None,
        since=None,
        evidence_from=now,
    )


def _enter_suspected(state: SnowCoverState, now: datetime) -> SnowCoverState:
    return replace(
        state,
        state=STATE_SUSPECTED,
        source=SOURCE_WEATHER,
        since=now,
        evidence_from=now,
    )


def _record(
    state: SnowCoverState,
    now: datetime,
    planned_w: float | None,
    actual_w: float | None,
    temperature_c: float | None,
    duration: timedelta,
) -> SnowCoverState:
    """Keep this run's reading in its slot, and the thaw clock."""
    slot = slot_start(now, duration)
    samples = state.samples
    if planned_w is not None and actual_w is not None:
        samples = tuple(s for s in samples if s.start != slot)
        samples += (Sample(slot, duration, float(planned_w), float(actual_w)),)
    samples = tuple(sorted((s for s in samples if s.start >= now - SAMPLE_KEEP), key=_by_start))

    temperatures = state.temperatures
    warm_since = state.warm_since
    if temperature_c is not None:
        temperatures = tuple(t for t in temperatures if t[0] != slot)
        temperatures += ((slot, float(temperature_c)),)
        warm_since = (warm_since or now) if temperature_c > THAW_TEMPERATURE_C else None
    temperatures = tuple(
        sorted((t for t in temperatures if t[0] >= now - GATE_LOOKBACK), key=lambda t: t[0])
    )
    return replace(state, samples=samples, temperatures=temperatures, warm_since=warm_since)


def _by_start(sample: Sample) -> datetime:
    return sample.start


def slot_start(now: datetime, duration: timedelta = SAMPLE_SLOT) -> datetime:
    """Start of the slot ``now`` falls in, on a grid counted from the epoch."""
    slot = int(duration.total_seconds())
    return datetime.fromtimestamp(int(now.timestamp()) // slot * slot, UTC)


def _utc(when: datetime) -> datetime:
    if when.tzinfo is None:
        raise ValueError(f"Naive datetime: {when!r}")
    return when.astimezone(UTC)


# -- persistence ---------------------------------------------------------------


def to_json(state: SnowCoverState) -> dict[str, Any]:
    """A JSON-safe snapshot of ``state``."""
    return {
        "state": state.state,
        "source": state.source,
        "since": _dump(state.since),
        "override": state.override,
        "override_at": _dump(state.override_at),
        "ratio": state.ratio,
        "last_working": _dump(state.last_working),
        "evidence_from": _dump(state.evidence_from),
        "warm_since": _dump(state.warm_since),
        "snow_seen": _dump(state.snow_seen),
        "weather_at": _dump(state.weather_at),
        "samples": [
            [s.start.isoformat(), s.duration.total_seconds(), s.planned_w, s.actual_w]
            for s in state.samples
        ],
        "temperatures": [[t.isoformat(), value] for t, value in state.temperatures],
    }


def from_json(data: Any) -> SnowCoverState:
    """Inverse of :func:`to_json`; anything unreadable gives a clear state."""
    if not isinstance(data, dict):
        return SnowCoverState()
    try:
        saved_state = data.get("state", STATE_CLEAR)
        override = data.get("override", MODE_AUTO)
        if (
            saved_state not in (STATE_CLEAR, STATE_SUSPECTED, STATE_COVERED)
            or override not in MODES
        ):
            return SnowCoverState()
        ratio = data.get("ratio")
        return SnowCoverState(
            state=saved_state,
            source=data.get("source"),
            since=_load(data.get("since")),
            override=override,
            override_at=_load(data.get("override_at")),
            ratio=None if ratio is None else float(ratio),
            last_working=_load(data.get("last_working")),
            evidence_from=_load(data.get("evidence_from")),
            warm_since=_load(data.get("warm_since")),
            snow_seen=_load(data.get("snow_seen")),
            weather_at=_load(data.get("weather_at")),
            samples=tuple(
                Sample(
                    datetime.fromisoformat(start),
                    timedelta(seconds=float(seconds)),
                    float(planned),
                    float(actual),
                )
                for start, seconds, planned, actual in data.get("samples", [])
            ),
            temperatures=tuple(
                (datetime.fromisoformat(t), float(value))
                for t, value in data.get("temperatures", [])
            ),
        )
    except (TypeError, ValueError):
        return SnowCoverState()


def _dump(when: datetime | None) -> str | None:
    return when.isoformat() if when else None


def _load(raw: Any) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None

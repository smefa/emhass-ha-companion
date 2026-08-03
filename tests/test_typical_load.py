"""Tests for the "Typical household" load profile's scaling.

Pure and hass-independent, like test_tariff.py -- the module only reads the
bundled reference file and Home Assistant's local-timezone helpers.
"""

from __future__ import annotations

from datetime import date

import pytest

from custom_components.emhass_companion.typical_load import (
    _reference_average_w,
    typical_day_records,
)

FRIDAY_IN_AUGUST = date(2026, 8, 7)


def _value_at(records: list[dict], local_time: str) -> float:
    (match,) = (
        r for r in records if r["time"].startswith(f"{FRIDAY_IN_AUGUST.isoformat()}T{local_time}")
    )
    return match["value"]


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_day_has_48_half_hourly_records_starting_at_local_midnight():
    records = typical_day_records(FRIDAY_IN_AUGUST, average_w=900)
    assert len(records) == 48
    assert records[0]["time"] == "2026-08-07T00:00:00+02:00"
    assert records[1]["time"] == "2026-08-07T00:30:00+02:00"


@pytest.mark.usefixtures("stockholm_timezone")
def test_scaling_to_the_reference_average_reproduces_the_raw_shape():
    """average_w == the bundled household's own average is a no-op scale."""
    records = typical_day_records(FRIDAY_IN_AUGUST, average_w=_reference_average_w())
    assert _value_at(records, "20:00:00") == pytest.approx(287.8, abs=0.1)


@pytest.mark.usefixtures("stockholm_timezone")
def test_scaling_is_linear_in_average_w():
    reference = _reference_average_w()
    base = typical_day_records(FRIDAY_IN_AUGUST, average_w=reference)
    doubled = typical_day_records(FRIDAY_IN_AUGUST, average_w=2 * reference)

    for low, high in zip(base, doubled, strict=True):
        assert high["value"] == pytest.approx(2 * low["value"], rel=0.01)


@pytest.mark.usefixtures("stockholm_timezone")
def test_a_declared_average_load_scales_the_generic_shape():
    """The case this profile exists for: no sensor, just a known average draw."""
    records = typical_day_records(FRIDAY_IN_AUGUST, average_w=1200)
    assert _value_at(records, "20:00:00") == pytest.approx(386.7, abs=0.1)

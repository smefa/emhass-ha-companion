"""The live-settings section of the support bundle.

A subentry only seeds the fields the numbers and selects go on to own, so a
bundle that prints the subentry alone reports values the optimiser stopped
using the first time anyone nudged one from a dashboard -- and says nothing
about having done so.
"""

from __future__ import annotations

from datetime import time
from types import SimpleNamespace

from custom_components.emhass_companion.deferrable import (
    SEEDED_FIELDS,
    DeferrableRuntime,
    live_settings,
)
from custom_components.emhass_companion.diagnostics import _loads_section


def _entry(**data):
    return SimpleNamespace(subentries={"abc": SimpleNamespace(data=data)})


def _coordinator(load):
    return SimpleNamespace(loads=SimpleNamespace(all=lambda: [load]))


def _load(**overrides) -> DeferrableRuntime:
    defaults = {"subentry_id": "abc", "name": "Wallbox", "nominal_power_w": 7500.0}
    return DeferrableRuntime(**{**defaults, **overrides})


def test_the_live_value_is_reported_not_the_seed():
    section = _loads_section(
        _coordinator(_load(nominal_power_w=7500.0, operating_hours=3.0)),
        _entry(nominal_power_w=8500.0, operating_hours=2.0),
    )

    assert section[0]["settings"]["nominal_power_w"] == 7500.0
    assert section[0]["settings"]["operating_hours"] == 3.0


def test_a_field_that_drifted_from_its_seed_is_named():
    section = _loads_section(
        _coordinator(_load(nominal_power_w=7500.0, operating_hours=3.0)),
        _entry(nominal_power_w=8500.0, operating_hours=3.0),
    )

    assert section[0]["diverged_from_seed"] == ["nominal_power_w"]


def test_json_shaped_seeds_do_not_read_as_divergence():
    """Storage hands back 7500 and "22:00:00" where the runtime holds 7500.0
    and a ``time``. Compared raw, every load would report as diverged and the
    flag would carry no information at all."""
    section = _loads_section(
        _coordinator(_load(nominal_power_w=7500.0, earliest_start=time(22, 0))),
        _entry(nominal_power_w=7500, earliest_start="22:00:00"),
    )

    assert section[0]["diverged_from_seed"] == []


def test_a_setting_json_cannot_carry_becomes_text():
    section = _loads_section(_coordinator(_load(earliest_start=time(22, 0))), _entry())

    assert section[0]["settings"]["earliest_start"] == "22:00:00"


def test_every_field_the_map_lists_is_reported():
    """The map sits beside the seeding it mirrors so the two cannot drift; this
    is what checks live_settings actually covers what it lists."""
    assert set(live_settings(_load())) == {key for key, _ in SEEDED_FIELDS}


def test_a_load_that_cannot_be_read_reports_the_error_rather_than_killing_the_download():
    """A support bundle exists to report broken state, so it must still be
    downloadable when the state it is reporting on is what is broken."""
    coordinator = SimpleNamespace(loads=SimpleNamespace(all=lambda: 1 / 0))

    assert "error" in _loads_section(coordinator, _entry())[0]

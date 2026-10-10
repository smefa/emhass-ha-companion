"""Reading EMHASS's stored heat_topology setting."""

from __future__ import annotations

import json

import pytest

from custom_components.emhass_companion.heat import topology_replaces_loads

TOPOLOGY = {"sources": [{"id": "hp"}], "storage": [{"id": "dhw"}], "flows": []}


@pytest.mark.parametrize(
    "raw",
    [None, "", "null", "  null ", {}, [], 0, "not json", "[1, 2]"],
)
def test_no_topology_replaces_nothing(raw: object) -> None:
    assert topology_replaces_loads({"heat_topology": raw}) is False


def test_a_missing_key_replaces_nothing() -> None:
    assert topology_replaces_loads({}) is False


def test_a_topology_in_replace_mode_replaces_the_loads() -> None:
    assert topology_replaces_loads({"heat_topology": TOPOLOGY}) is True


def test_an_explicit_false_is_still_replace_mode() -> None:
    topology = {**TOPOLOGY, "extend_deferrable_loads": False}
    assert topology_replaces_loads({"heat_topology": topology}) is True


def test_extend_mode_keeps_the_loads() -> None:
    topology = {**TOPOLOGY, "extend_deferrable_loads": True}
    assert topology_replaces_loads({"heat_topology": topology}) is False


def test_the_extend_flag_means_nothing_to_a_backend_that_predates_it() -> None:
    topology = {**TOPOLOGY, "extend_deferrable_loads": True}
    config = {"heat_topology": topology}
    assert topology_replaces_loads(config, extend_supported=False) is True
    assert topology_replaces_loads(config, extend_supported=True) is False


def test_a_quoted_json_topology_is_read_too() -> None:
    assert topology_replaces_loads({"heat_topology": json.dumps(TOPOLOGY)}) is True
    extended = json.dumps({**TOPOLOGY, "extend_deferrable_loads": True})
    assert topology_replaces_loads({"heat_topology": extended}) is False

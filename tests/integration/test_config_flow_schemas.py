"""Config flow schemas that need a real Home Assistant event loop to validate.

`cv.template` (which `selector.TemplateSelector` calls) requires a running
event loop to look up `hass` internally; the plain-Python schema-construction
tests in tests/test_config_flow_schemas.py cannot reach it. This is exactly
what let the reported bug slip through: those tests only ever *built* the
schema, never *validated data against it* the way a real form submission does.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
import pytest
import voluptuous as vol

from custom_components.emhass_companion.config_flow import _tariff_side_schema


async def test_tariff_schema_survives_a_previously_persisted_none_template(
    hass: HomeAssistant,
) -> None:
    """Reproduces the reported bug.

    Before the fix, `_collect_tariff` stored `template: None` (rather than
    omitting the key) whenever the field was left blank -- the normal case for
    "linear"/"passthrough" mode. Every time the tariff step was opened again
    afterwards, `_tariff_side_schema`'s `defaults.get(CONF_TEMPLATE, "")` read
    that stored None straight back out (dict.get's fallback only applies when
    the key is missing, not when it's present-but-None), baking `default=None`
    into the schema. Submitting the form without touching the template field
    (any blank optional field commonly gets omitted rather than sent as "")
    made voluptuous validate that None default through `TemplateSelector`,
    raising "template value is None" -- on a submission that never mentioned
    template, changing only cost settings like the multiplier or adder.
    """
    stored = {"mode": "linear", "multiplier": 1.0, "adder": 0.0, "template": None}
    schema = vol.Schema(_tariff_side_schema("buy", stored))

    result = schema({"buy_mode": "linear", "buy_multiplier": 1.5, "buy_adder": 0.1})

    assert result["buy_template"] == ""


# --- export multiplier must never end up at 0 --------------------------------


@pytest.mark.parametrize("stored", [{}, {"multiplier": 0}, {"multiplier": 0.0}])
def test_sell_multiplier_default_is_never_zero(hass: HomeAssistant, stored) -> None:
    """A 0 export multiplier zeroes out the whole sell-price series.

    Covers both a genuinely absent key (a fresh setup) and an already
    persisted 0 -- the same dict.get gotcha as the template bug: `.get(key,
    default)` only falls back when the key is missing, not when it's
    present-but-falsy.
    """
    schema = vol.Schema(_tariff_side_schema("sell", stored))
    result = schema({"sell_mode": "linear"})
    assert result["sell_multiplier"] == 1.0


def test_buy_multiplier_may_be_zero(hass: HomeAssistant) -> None:
    """Only export is special-cased; a 0 import multiplier is left alone."""
    schema = vol.Schema(_tariff_side_schema("buy", {"multiplier": 0}))
    result = schema({"buy_mode": "linear"})
    assert result["buy_multiplier"] == 0

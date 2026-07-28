"""Card serving and Lovelace resource registration.

Registering a resource twice leaves the user with duplicate entries they have
to clean up by hand, and failing to bump the version leaves browsers running a
stale bundle. Both are checked here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.components.lovelace.const import MODE_STORAGE, MODE_YAML
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest

from custom_components.emhass_companion.frontend import (
    CARDS_URL,
    async_setup_frontend,
)


@pytest.fixture
async def lovelace(hass: HomeAssistant):
    """A Lovelace setup with storage-mode resources."""
    assert await async_setup_component(hass, "lovelace", {})
    await hass.async_block_till_done()
    from homeassistant.components.lovelace.const import LOVELACE_DATA

    return hass.data[LOVELACE_DATA]


@pytest.fixture(autouse=True)
def _no_static_paths():
    """The http static path registration is not what these tests are about."""
    with patch(
        "custom_components.emhass_companion.frontend._async_register_static_path",
        AsyncMock(),
    ):
        yield


async def test_the_card_bundle_is_shipped() -> None:
    """A missing bundle would only show up as a broken card in a browser."""
    from pathlib import Path

    import custom_components.emhass_companion.frontend as frontend

    bundle = Path(frontend.__file__).parent / "frontend" / frontend.CARDS_FILENAME
    assert bundle.is_file(), f"card bundle missing at {bundle}"
    text = bundle.read_text(encoding="utf-8")
    # Both cards must actually be defined, or the resource registers a module
    # that silently provides nothing.
    assert 'customElements.define("emhass-plan-card"' in text
    assert 'customElements.define("emhass-deferrable-card"' in text


async def test_resource_is_registered_once(hass: HomeAssistant, lovelace) -> None:
    await async_setup_frontend(hass, "1.0.0")
    await async_setup_frontend(hass, "1.0.0")

    ours = [r for r in lovelace.resources.async_items() if CARDS_URL in r["url"]]
    assert len(ours) == 1
    assert ours[0]["url"] == f"{CARDS_URL}?v=1.0.0"
    # Created with `res_type`, but Lovelace stores it as `type`.
    assert ours[0]["type"] == "module"


async def test_a_new_version_updates_in_place(hass: HomeAssistant, lovelace) -> None:
    """Otherwise browsers keep running the bundle they already cached."""
    await async_setup_frontend(hass, "1.0.0")
    await async_setup_frontend(hass, "1.1.0")

    ours = [r for r in lovelace.resources.async_items() if CARDS_URL in r["url"]]
    assert len(ours) == 1
    assert ours[0]["url"] == f"{CARDS_URL}?v=1.1.0"


async def test_other_resources_are_left_alone(hass: HomeAssistant, lovelace) -> None:
    await lovelace.resources.async_create_item(
        {"res_type": "module", "url": "/local/somebody-elses-card.js"}
    )

    await async_setup_frontend(hass, "1.0.0")

    urls = {r["url"] for r in lovelace.resources.async_items()}
    assert "/local/somebody-elses-card.js" in urls
    assert f"{CARDS_URL}?v=1.0.0" in urls


async def test_yaml_mode_does_not_attempt_to_write(
    hass: HomeAssistant, lovelace, caplog: pytest.LogCaptureFixture
) -> None:
    """YAML-managed resources are read-only; instructions are all we can give."""
    lovelace.resource_mode = MODE_YAML

    await async_setup_frontend(hass, "1.0.0")

    assert CARDS_URL in caplog.text
    assert "resources" in caplog.text


async def test_missing_lovelace_is_not_fatal(hass: HomeAssistant) -> None:
    """Lovelace may not be set up at all; the integration must still work."""
    await async_setup_frontend(hass, "1.0.0")


async def test_storage_mode_is_the_default(hass: HomeAssistant, lovelace) -> None:
    assert lovelace.resource_mode == MODE_STORAGE

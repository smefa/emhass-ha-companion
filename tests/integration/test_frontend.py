"""Card serving and Lovelace resource registration.

Registering a resource twice leaves the user with duplicate entries they have
to clean up by hand, and failing to bump the version leaves browsers running a
stale bundle. Both are checked here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from homeassistant.components.lovelace.const import MODE_STORAGE, MODE_YAML
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest

import custom_components.emhass_companion.frontend as frontend
from custom_components.emhass_companion.frontend import (
    CORE_FILENAME,
    CORE_URL,
    _async_register_static_paths,
    async_setup_frontend,
)

# What each family bundle must define, so a bundle that silently fails to
# provide its card (or its editor) is caught here rather than in a browser.
_EXPECTED_ELEMENTS = {
    "emhass-plan-card.js": ["emhass-plan-card", "emhass-plan-card-editor"],
    "emhass-deferrable-cards.js": [
        "emhass-deferrable-card",
        "emhass-deferrable-card-editor",
        "emhass-deferrable-swipe-card",
        "emhass-deferrable-swipe-card-editor",
        "emhass-deferrable-strip-card",
        "emhass-deferrable-strip-card-editor",
    ],
    "emhass-health-card.js": ["emhass-health-card", "emhass-health-card-editor"],
    "emhass-status-card.js": ["emhass-status-card", "emhass-status-card-editor"],
    "emhass-overview-card.js": ["emhass-overview-card", "emhass-overview-card-editor"],
    "emhass-tariff-card.js": ["emhass-tariff-card", "emhass-tariff-card-editor"],
}


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
        "custom_components.emhass_companion.frontend._async_register_static_paths",
        AsyncMock(),
    ):
        yield


def _bundle_path(filename: str) -> Path:
    return Path(frontend.__file__).parent / "frontend" / filename


async def test_every_family_bundle_defines_its_elements() -> None:
    """A bundle that ships but defines nothing is a silently broken card."""
    assert set(_EXPECTED_ELEMENTS) == {filename for filename, _ in frontend.BUNDLES}
    for filename, tags in _EXPECTED_ELEMENTS.items():
        bundle = _bundle_path(filename)
        assert bundle.is_file(), f"card bundle missing at {bundle}"
        text = bundle.read_text(encoding="utf-8")
        for tag in tags:
            assert f'customElements.define("{tag}"' in text, f"{tag} missing from {filename}"


async def test_family_bundles_import_the_versioned_core() -> None:
    """Each family bundle must ask for core.js by a version-substitutable specifier.

    A relative `import` is cached by the browser under its own literal URL,
    independent of the versioned URL the importing bundle was fetched under.
    Without the `__VERSION__` placeholder in that import, a change to core.js
    would never invalidate a browser's already-cached copy of it.
    """
    for filename in _EXPECTED_ELEMENTS:
        text = _bundle_path(filename).read_text(encoding="utf-8")
        assert "./emhass-core.js?v=__VERSION__" in text, (
            f"{filename} does not import core.js by the versioned placeholder"
        )


async def test_the_core_module_is_shipped() -> None:
    """A missing core module breaks every family bundle that imports it."""
    core = _bundle_path(CORE_FILENAME)
    assert core.is_file(), f"core module missing at {core}"


async def test_every_declared_bundle_exists() -> None:
    """A bundle listed but not shipped registers a resource that 404s.

    Which is worse than not registering it: Lovelace then logs a failed
    module load on every page view, for every user, forever.
    """
    for filename, _ in frontend.BUNDLES:
        assert _bundle_path(filename).is_file(), f"card bundle missing at {filename}"


async def test_resource_is_registered_once(hass: HomeAssistant, lovelace) -> None:
    await async_setup_frontend(hass, "1.0.0")
    await async_setup_frontend(hass, "1.0.0")

    _, url = frontend.BUNDLES[0]
    ours = [r for r in lovelace.resources.async_items() if url in r["url"]]
    assert len(ours) == 1
    assert ours[0]["url"] == f"{url}?v=1.0.0"
    # Created with `res_type`, but Lovelace stores it as `type`.
    assert ours[0]["type"] == "module"


async def test_a_new_version_updates_in_place(hass: HomeAssistant, lovelace) -> None:
    """Otherwise browsers keep running the bundle they already cached."""
    await async_setup_frontend(hass, "1.0.0")
    await async_setup_frontend(hass, "1.1.0")

    _, url = frontend.BUNDLES[0]
    ours = [r for r in lovelace.resources.async_items() if url in r["url"]]
    assert len(ours) == 1
    assert ours[0]["url"] == f"{url}?v=1.1.0"


async def test_each_bundle_gets_its_own_resource(hass: HomeAssistant, lovelace) -> None:
    """One resource per declared bundle, and none mistaken for another.

    Bundle URLs share a prefix, so a prefix match would let a sibling bundle
    look like a stale copy of this one -- and each run would rewrite whichever
    it saw first, leaving exactly one working bundle.
    """
    await async_setup_frontend(hass, "1.0.0")
    await async_setup_frontend(hass, "1.1.0")

    urls = {r["url"] for r in lovelace.resources.async_items()}
    assert urls == {f"{url}?v=1.1.0" for _, url in frontend.BUNDLES}


async def test_core_module_is_never_registered_as_a_resource(hass: HomeAssistant, lovelace) -> None:
    """Nothing loads core.js except as another bundle's `import` -- it must

    not show up as a selectable Lovelace resource of its own.
    """
    await async_setup_frontend(hass, "1.0.0")

    urls = {r["url"].split("?")[0] for r in lovelace.resources.async_items()}
    assert CORE_URL not in urls


async def test_a_withdrawn_bundle_is_unregistered(hass: HomeAssistant, lovelace) -> None:
    """A resource outlives the file it points at, and 404s forever after.

    The single-bundle era (and the briefly-experimental second bundle before
    that) both left URLs written into Lovelace's own storage, where nothing
    else will ever remove them.
    """
    await lovelace.resources.async_create_item(
        {"res_type": "module", "url": "/emhass_companion/emhass-cards.js?v=0.9.5.4"}
    )

    await async_setup_frontend(hass, "1.0.0")

    urls = {r["url"] for r in lovelace.resources.async_items()}
    assert urls == {f"{url}?v=1.0.0" for _, url in frontend.BUNDLES}


async def test_other_resources_are_left_alone(hass: HomeAssistant, lovelace) -> None:
    await lovelace.resources.async_create_item(
        {"res_type": "module", "url": "/local/somebody-elses-card.js"}
    )

    await async_setup_frontend(hass, "1.0.0")

    urls = {r["url"] for r in lovelace.resources.async_items()}
    assert "/local/somebody-elses-card.js" in urls
    for _, url in frontend.BUNDLES:
        assert f"{url}?v=1.0.0" in urls


async def test_yaml_mode_does_not_attempt_to_write(
    hass: HomeAssistant, lovelace, caplog: pytest.LogCaptureFixture
) -> None:
    """YAML-managed resources are read-only; instructions are all we can give."""
    lovelace.resource_mode = MODE_YAML

    await async_setup_frontend(hass, "1.0.0")

    for _, url in frontend.BUNDLES:
        assert url in caplog.text
    assert "resources" in caplog.text


async def test_missing_lovelace_is_not_fatal(hass: HomeAssistant) -> None:
    """Lovelace may not be set up at all; the integration must still work."""
    await async_setup_frontend(hass, "1.0.0")


async def test_storage_mode_is_the_default(hass: HomeAssistant, lovelace) -> None:
    assert lovelace.resource_mode == MODE_STORAGE


async def test_bundle_is_gzipped_when_the_client_allows_it(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """A slow transfer is a slower element registration, on the frontend's own

    fixed timeout (home-assistant/frontend#52960) -- smaller loses that race
    less often.
    """
    # The autouse fixture above patches the module attribute; this test's own
    # import of the function was bound before that ran, so it still calls the
    # real thing.
    assert await async_setup_component(hass, "http", {})
    await _async_register_static_paths(hass, "1.0.0")
    client = await hass_client_no_auth()

    response = await client.get(CORE_URL, headers={"Accept-Encoding": "gzip"})
    assert response.status == 200
    # The wire format, not the client's own view: aiohttp's client transparently
    # decompresses a gzip body on read, so content_length is the only place the
    # actual transfer size -- the thing this is for -- is still visible.
    assert response.headers["Content-Encoding"] == "gzip"
    assert response.headers["Vary"] == "Accept-Encoding"

    raw = _bundle_path(CORE_FILENAME).read_bytes()
    assert await response.read() == raw
    assert response.content_length < len(raw)


async def test_bundle_falls_back_to_plain_text_without_gzip_support(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """A client that never advertised gzip must not be handed compressed bytes."""
    assert await async_setup_component(hass, "http", {})
    await _async_register_static_paths(hass, "1.0.0")
    client = await hass_client_no_auth()

    response = await client.get(CORE_URL, headers={"Accept-Encoding": "identity"})
    assert response.status == 200
    assert "Content-Encoding" not in response.headers

    raw = _bundle_path(CORE_FILENAME).read_bytes()
    assert await response.read() == raw


async def test_family_bundle_gets_the_version_substituted_into_its_core_import(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """The served bytes, not the source file: substitution happens per-request

    version, not on disk.
    """
    assert await async_setup_component(hass, "http", {})
    await _async_register_static_paths(hass, "9.9.9")
    client = await hass_client_no_auth()

    filename, url = frontend.BUNDLES[0]
    response = await client.get(url, headers={"Accept-Encoding": "identity"})
    assert response.status == 200
    body = (await response.read()).decode("utf-8")
    assert "__VERSION__" not in body
    assert "./emhass-core.js?v=9.9.9" in body

    # The source file on disk is untouched -- only the served copy is templated.
    on_disk = _bundle_path(filename).read_text(encoding="utf-8")
    assert "./emhass-core.js?v=__VERSION__" in on_disk

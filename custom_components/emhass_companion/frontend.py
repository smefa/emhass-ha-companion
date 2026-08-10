"""Serve and register the dashboard cards.

The cards ship inside this integration rather than as a separate HACS
"plugin" repository. A HACS repository has exactly one category, so a second
repo would be the only alternative -- and it would then be possible to install
the integration without the cards, or the cards at a version that does not
match. Serving them from here keeps the two in lockstep.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace import LovelaceData
from homeassistant.components.lovelace.const import (
    DOMAIN as LOVELACE_DOMAIN,
)
from homeassistant.components.lovelace.const import (
    MODE_STORAGE,
)
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARDS_FILENAME = "emhass-cards.js"
CARDS_URL = f"/{DOMAIN}/{CARDS_FILENAME}"

# Every card ships in one bundle. There was briefly a second, experimental one
# alongside it; its cards graduated into this one, and the machinery for
# serving several is kept because it costs nothing and the alternative -- a
# prefix match against a single URL -- is the bug that made a sibling bundle
# look like a stale copy of this one.
BUNDLES: tuple[tuple[str, str], ...] = ((CARDS_FILENAME, CARDS_URL),)

_STATIC_PATH_KEY = f"{DOMAIN}_static_path_registered"


async def async_setup_frontend(hass: HomeAssistant, version: str) -> None:
    """Serve the card bundles and register them as Lovelace resources."""
    await _async_register_static_paths(hass)
    for _, url in BUNDLES:
        await _async_register_resource(hass, version, url)
    await _async_drop_retired_resources(hass)


async def _async_drop_retired_resources(hass: HomeAssistant) -> None:
    """Remove resources we registered for a bundle that no longer exists.

    A resource is written into Lovelace's own storage and outlives the file it
    points at, so withdrawing a bundle leaves every page load fetching a 404
    and Lovelace logging a failed module import -- forever, for every user, on
    an install that has done nothing wrong. Only URLs under this integration's
    own path are considered, so somebody else's card is never touched.
    """
    lovelace: LovelaceData | None = hass.data.get(LOVELACE_DOMAIN)
    if lovelace is None or lovelace.resource_mode != MODE_STORAGE:
        return

    resources = lovelace.resources
    await resources.async_get_info()
    ours = {url for _, url in BUNDLES}
    for item in list(resources.async_items()):
        path = item.get("url", "").split("?")[0]
        if not path.startswith(f"/{DOMAIN}/") or path in ours:
            continue
        await resources.async_delete_item(item["id"])
        _LOGGER.info("Removed the retired dashboard card resource %s", path)


async def _async_register_static_paths(hass: HomeAssistant) -> None:
    """Serve the JavaScript files.

    Registering the same path twice raises, and a config entry reload runs this
    again, so it is guarded. The files themselves are served with cache headers
    -- each resource URL carries a version query string, which is what actually
    invalidates a stale copy.
    """
    if hass.data.get(_STATIC_PATH_KEY):
        return

    configs: list[StaticPathConfig] = []
    for filename, url in BUNDLES:
        source = Path(__file__).parent / "frontend" / filename
        if not source.is_file():  # pragma: no cover - packaging error
            _LOGGER.error("Card bundle missing at %s; those cards will not load", source)
            continue
        configs.append(StaticPathConfig(url, str(source), cache_headers=True))

    if not configs:  # pragma: no cover - packaging error
        return

    await hass.http.async_register_static_paths(configs)
    hass.data[_STATIC_PATH_KEY] = True
    _LOGGER.debug("Serving dashboard cards at %s", ", ".join(url for _, url in BUNDLES))


async def _async_register_resource(hass: HomeAssistant, version: str, cards_url: str) -> None:
    """Add the module to Lovelace's resources, if it manages them.

    Only possible in storage mode. With YAML-managed resources the file is
    still served, but the user has to add it themselves -- there is nowhere for
    us to write it.
    """
    versioned_url = f"{cards_url}?v={version}"

    lovelace: LovelaceData | None = hass.data.get(LOVELACE_DOMAIN)
    if lovelace is None:
        _LOGGER.debug("Lovelace is not set up; skipping resource registration")
        return

    if lovelace.resource_mode != MODE_STORAGE:
        _LOGGER.info(
            "Lovelace resources are managed in YAML. Add this to your "
            "configuration to use the EMHASS Companion cards:\n"
            "lovelace:\n  resources:\n    - url: %s\n      type: module",
            versioned_url,
        )
        return

    resources = lovelace.resources
    # Storage-mode resources load lazily, and async_items() returns nothing
    # until something has triggered that load -- which would look like "not
    # registered yet" and add a duplicate on every restart. async_get_info is
    # the public call that forces it.
    await resources.async_get_info()

    for item in resources.async_items():
        url = item.get("url", "")
        # Compared on the path alone rather than by prefix: with more than one
        # bundle served from the same directory, a prefix match is one filename
        # away from treating a sibling bundle as this one's stale copy and
        # rewriting it.
        if url.split("?")[0] != cards_url:
            continue
        if url == versioned_url:
            return
        # Same module, different version: update in place so the browser is
        # forced to refetch instead of silently running the old bundle.
        await resources.async_update_item(item["id"], {"url": versioned_url})
        _LOGGER.info("Updated dashboard card resource to %s", versioned_url)
        return

    await resources.async_create_item({"res_type": "module", "url": versioned_url})
    _LOGGER.info("Registered dashboard card resource %s", versioned_url)

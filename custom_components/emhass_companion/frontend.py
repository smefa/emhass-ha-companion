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

_STATIC_PATH_KEY = f"{DOMAIN}_static_path_registered"


async def async_setup_frontend(hass: HomeAssistant, version: str) -> None:
    """Serve the card bundle and register it as a Lovelace resource."""
    await _async_register_static_path(hass)
    await _async_register_resource(hass, version)


async def _async_register_static_path(hass: HomeAssistant) -> None:
    """Serve the JavaScript file.

    Registering the same path twice raises, and a config entry reload runs this
    again, so it is guarded. The file itself is served with cache headers --
    the resource URL carries a version query string, which is what actually
    invalidates a stale copy.
    """
    if hass.data.get(_STATIC_PATH_KEY):
        return

    source = Path(__file__).parent / "frontend" / CARDS_FILENAME
    if not source.is_file():  # pragma: no cover - packaging error
        _LOGGER.error("Card bundle missing at %s; cards will not load", source)
        return

    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARDS_URL, str(source), cache_headers=True)]
    )
    hass.data[_STATIC_PATH_KEY] = True
    _LOGGER.debug("Serving dashboard cards at %s", CARDS_URL)


async def _async_register_resource(hass: HomeAssistant, version: str) -> None:
    """Add the module to Lovelace's resources, if it manages them.

    Only possible in storage mode. With YAML-managed resources the file is
    still served, but the user has to add it themselves -- there is nowhere for
    us to write it.
    """
    versioned_url = f"{CARDS_URL}?v={version}"

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
        if not url.startswith(CARDS_URL):
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

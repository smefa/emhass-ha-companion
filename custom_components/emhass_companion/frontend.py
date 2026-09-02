"""Serve and register the dashboard cards.

The cards ship inside this integration rather than as a separate HACS
"plugin" repository. A HACS repository has exactly one category, so a second
repo would be the only alternative -- and it would then be possible to install
the integration without the cards, or the cards at a version that does not
match. Serving them from here keeps the two in lockstep.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
import gzip
import logging
from pathlib import Path

from aiohttp import web
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

CONTENT_TYPE = "text/javascript"
# Mirrors homeassistant.components.http.static.CACHE_HEADER (31 days) without
# importing a private module path: each resource URL already carries a
# version query string, so a stale cached copy only matters until the next
# version bump, not until this expires.
CACHE_CONTROL = "public, max-age=2678400"

CORE_FILENAME = "emhass-core.js"
CORE_URL = f"/{DOMAIN}/{CORE_FILENAME}"

# One entry per card family, each its own Lovelace resource. All 14 elements
# used to ship from one module: when the frontend's own element-registration
# race (home-assistant/frontend#52960) is lost, the whole module fails and
# every card on it goes down together. Splitting by family means a lost race
# takes out 1-3 elements instead of 14, and each file is small enough to be
# less likely to lose that race at all. The shared code they all `import`
# from lives in CORE_FILENAME -- served (below) but never registered as a
# resource of its own, since nothing loads it except as a dependency.
BUNDLES: tuple[tuple[str, str], ...] = (
    ("emhass-plan-card.js", f"/{DOMAIN}/emhass-plan-card.js"),
    ("emhass-deferrable-cards.js", f"/{DOMAIN}/emhass-deferrable-cards.js"),
    ("emhass-health-card.js", f"/{DOMAIN}/emhass-health-card.js"),
    ("emhass-status-card.js", f"/{DOMAIN}/emhass-status-card.js"),
    ("emhass-overview-card.js", f"/{DOMAIN}/emhass-overview-card.js"),
    ("emhass-tariff-card.js", f"/{DOMAIN}/emhass-tariff-card.js"),
    ("emhass-economy-card.js", f"/{DOMAIN}/emhass-economy-card.js"),
)

_STATIC_PATH_KEY = f"{DOMAIN}_static_path_registered"


async def async_setup_frontend(hass: HomeAssistant, version: str) -> None:
    """Serve the card bundles and register them as Lovelace resources."""
    await _async_register_static_paths(hass, version)
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


@dataclass(slots=True)
class _Bundle:
    """A card bundle's bytes, kept in both forms so gzip runs once, not per request."""

    raw: bytes
    gzipped: bytes


def _read_and_compress(source: Path, version: str) -> _Bundle:
    """Off the event loop: file I/O, version substitution, and compression.

    Each family bundle imports the shared core module by a literal
    `./emhass-core.js?v=__VERSION__` specifier -- a relative import, so the
    browser resolves and caches it by that exact URL regardless of which
    versioned bundle asked for it. Without the substituted version in that
    URL, core.js would keep its own separate, unversioned cache entry, and a
    change to it would never invalidate for a browser that had already
    fetched an older copy, even after every family bundle moved on.
    """
    raw = source.read_text(encoding="utf-8").replace("__VERSION__", version).encode("utf-8")
    return _Bundle(raw=raw, gzipped=gzip.compress(raw, compresslevel=9))


async def _serve_bundle(bundle: _Bundle, request: web.Request) -> web.Response:
    """Hand back whichever form the client asked for.

    aiohttp's own static-file serving (what HA's StaticPathConfig uses under
    the hood) is a plain FileResponse with no content negotiation, so every
    client downloaded the full, uncommented bundle -- gzip shrinks it by
    roughly three quarters. That matters here specifically: the dashboard
    cards race the frontend's own fixed element-registration timeout
    (home-assistant/frontend#52960), and a slower transfer on a weak
    connection is a slower registration, which is a card more likely to lose
    that race.
    """
    accepts_gzip = "gzip" in request.headers.get("Accept-Encoding", "")
    headers = {"Cache-Control": CACHE_CONTROL, "Vary": "Accept-Encoding"}
    if accepts_gzip:
        headers["Content-Encoding"] = "gzip"
    return web.Response(
        body=bundle.gzipped if accepts_gzip else bundle.raw,
        content_type=CONTENT_TYPE,
        headers=headers,
    )


async def _async_register_static_paths(hass: HomeAssistant, version: str) -> None:
    """Serve the JavaScript files, gzip-compressed when the client allows it.

    Registering the same path twice raises, and a config entry reload runs this
    again, so it is guarded. Each resource URL carries a version query string,
    which is what actually invalidates a stale cached copy. The core module is
    served here too -- every family bundle imports it -- but it is never one of
    `BUNDLES`, since nothing loads it as a Lovelace resource of its own.
    """
    if hass.data.get(_STATIC_PATH_KEY):
        return

    files = ((CORE_FILENAME, CORE_URL), *BUNDLES)
    routes: dict[str, _Bundle] = {}
    for filename, url in files:
        source = Path(__file__).parent / "frontend" / filename
        if not source.is_file():  # pragma: no cover - packaging error
            _LOGGER.error("Card bundle missing at %s; those cards will not load", source)
            continue
        routes[url] = await hass.async_add_executor_job(_read_and_compress, source, version)

    if not routes:  # pragma: no cover - packaging error
        return

    for url, bundle in routes.items():
        hass.http.app.router.add_route("GET", url, partial(_serve_bundle, bundle))

    hass.data[_STATIC_PATH_KEY] = True
    _LOGGER.debug("Serving dashboard cards at %s", ", ".join(routes))


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

"""The EMHASS add-on is only ever installed from a third-party repository, so
Supervisor always prefixes its slug with a hash of that repository's URL --
e.g. "5b918bf2_emhass", never the bare "emhass" the discovery code used to
hardcode. That made discovery silently fail for every real install, falling
back to the "http://localhost:5000" default -- which cannot reach a sibling
add-on's container -- and left the user to find the right address themselves.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.hassio import AddonState
from homeassistant.core import HomeAssistant

from custom_components.emhass_companion.config_flow import _async_addon_url


def _installed_addon(*, slug: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(slug=slug, name=name)


async def test_finds_the_addon_by_name_despite_the_repository_hash_prefix(
    hass: HomeAssistant,
) -> None:
    supervisor_client = SimpleNamespace(
        addons=SimpleNamespace(
            list=AsyncMock(
                return_value=[
                    _installed_addon(slug="a0d7b954_grafana", name="Grafana"),
                    _installed_addon(slug="5b918bf2_emhass", name="EMHASS"),
                ]
            )
        )
    )
    addon_info = SimpleNamespace(state=AddonState.RUNNING, hostname="5b918bf2-emhass")
    addon_manager = MagicMock()
    addon_manager.async_get_addon_info = AsyncMock(return_value=addon_info)

    with (
        patch("homeassistant.components.hassio.is_hassio", return_value=True, create=True),
        patch(
            "homeassistant.components.hassio.get_supervisor_client",
            return_value=supervisor_client,
        ),
        patch(
            "homeassistant.components.hassio.AddonManager",
            return_value=addon_manager,
        ) as addon_manager_cls,
    ):
        url = await _async_addon_url(hass)

    assert url == "http://5b918bf2-emhass:5000"
    # The prefixed slug discovered via `list()`, not the bare "emhass" the
    # old hardcoded constant used.
    assert addon_manager_cls.call_args.args[-1] == "5b918bf2_emhass"


async def test_returns_none_when_no_addon_matches_by_name(hass: HomeAssistant) -> None:
    supervisor_client = SimpleNamespace(
        addons=SimpleNamespace(
            list=AsyncMock(return_value=[_installed_addon(slug="a0d7b954_grafana", name="Grafana")])
        )
    )

    with (
        patch("homeassistant.components.hassio.is_hassio", return_value=True, create=True),
        patch(
            "homeassistant.components.hassio.get_supervisor_client",
            return_value=supervisor_client,
        ),
    ):
        url = await _async_addon_url(hass)

    assert url is None


async def test_returns_none_when_not_a_supervised_install(hass: HomeAssistant) -> None:
    with patch("homeassistant.components.hassio.is_hassio", return_value=False, create=True):
        url = await _async_addon_url(hass)

    assert url is None

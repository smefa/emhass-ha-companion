"""HTTP client for an EMHASS instance (add-on or standalone container)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from .const import (
    ENDPOINT_ACTION,
    ENDPOINT_GET_CONFIG,
    ENDPOINT_HEALTHZ,
    ENDPOINT_LAST_RUN,
    ENDPOINT_PLAN,
    ENDPOINT_SET_CONFIG,
)
from .models import LastRun, Plan

_LOGGER = logging.getLogger(__name__)

# EMHASS solves a MIP; a day-ahead run on a slow host with several deferrable
# loads can take well over the default aiohttp timeout. Its own solver timeout
# defaults to 45s, so this leaves room for fetch + solve + write.
ACTION_TIMEOUT = ClientTimeout(total=300)
READ_TIMEOUT = ClientTimeout(total=30)


class EmhassError(Exception):
    """Base error for EMHASS communication."""


class EmhassConnectionError(EmhassError):
    """EMHASS could not be reached."""


class EmhassApiError(EmhassError):
    """EMHASS rejected a request."""


class EmhassClient:
    """Thin async wrapper over the EMHASS web server.

    EMHASS exposes no authentication on its HTTP port, so there are no
    credentials to manage here.
    """

    def __init__(self, session: ClientSession, base_url: str) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")

    @property
    def base_url(self) -> str:
        return self._base_url

    # -- low level ------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        client_timeout: ClientTimeout = READ_TIMEOUT,
        expect_json: bool = True,
    ) -> Any:
        url = f"{self._base_url}{path}"
        try:
            async with self._session.request(
                method, url, json=json, timeout=client_timeout
            ) as response:
                body = await response.text()
                if response.status >= 400:
                    raise EmhassApiError(
                        f"{method} {path} returned {response.status}: {body.strip()[:300]}"
                    )
                if not expect_json:
                    return body
                try:
                    return await response.json(content_type=None)
                except ValueError as err:
                    raise EmhassApiError(
                        f"{method} {path} returned invalid JSON: {body.strip()[:200]}"
                    ) from err
        except TimeoutError as err:
            raise EmhassConnectionError(f"Timeout calling {url}") from err
        except ClientError as err:
            raise EmhassConnectionError(f"Cannot reach EMHASS at {url}: {err}") from err

    # -- health & version -----------------------------------------------------

    async def async_healthz(self) -> dict[str, Any]:
        """Liveness probe.

        Note this reports ``ok`` whenever *a* run exists -- including one that
        came back infeasible. It answers "is EMHASS alive", never "was the last
        plan usable"; use :meth:`async_get_last_run` for that.
        """
        return await self._request("GET", ENDPOINT_HEALTHZ)

    async def async_get_version(self) -> str | None:
        """Installed EMHASS version, or None if it cannot be determined."""
        health = await self.async_healthz()
        versions = health.get("versions") or {}
        return versions.get("emhass")

    # -- configuration --------------------------------------------------------

    async def async_get_config(self) -> dict[str, Any]:
        return await self._request("GET", ENDPOINT_GET_CONFIG)

    async def async_set_config(self, config: dict[str, Any]) -> None:
        """Write EMHASS's configuration.

        Called once at setup so that ``params.pkl`` exists -- every ``/action/*``
        request fails without it -- and thereafter only on explicit user request.
        Per-run values are sent as runtime parameters instead, which override
        whatever is stored here.
        """
        await self._request("POST", ENDPOINT_SET_CONFIG, json=config, expect_json=False)

    # -- optimisation ---------------------------------------------------------

    async def async_run_action(
        self, action: str, runtime_params: dict[str, Any] | None = None
    ) -> str:
        """Run an EMHASS action.

        Returns the plain-text status line. EMHASS does not return the plan from
        this endpoint -- fetch it with :meth:`async_get_plan` afterwards.
        """
        path = ENDPOINT_ACTION.format(action=action)
        body = await self._request(
            "POST",
            path,
            json=runtime_params or {},
            client_timeout=ACTION_TIMEOUT,
            expect_json=False,
        )
        message = str(body).strip()
        _LOGGER.debug("EMHASS %s: %s", action, message)
        return message

    async def async_get_plan(self) -> Plan | None:
        """Latest optimisation plan, or None if EMHASS has never run."""
        return Plan.from_response(await self._request("GET", ENDPOINT_PLAN))

    async def async_get_last_run(self) -> LastRun:
        return LastRun.from_response(await self._request("GET", ENDPOINT_LAST_RUN))

    # -- convenience ----------------------------------------------------------

    async def async_optimize(
        self, action: str, runtime_params: dict[str, Any]
    ) -> tuple[LastRun, Plan | None]:
        """Run an action and collect its outcome.

        The action endpoint reports transport success; ``last-run`` reports
        whether the solve actually produced anything usable. Both are needed --
        an infeasible solve returns a perfectly successful HTTP 200.
        """
        await self.async_run_action(action, runtime_params)
        last_run, plan = await asyncio.gather(self.async_get_last_run(), self.async_get_plan())
        return last_run, plan

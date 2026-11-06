"""HTTP client for an EMHASS instance (add-on or standalone container)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import logging
import re
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

# EMHASS answers a failed action with its run log as a JSON list of lines, e.g.
# "ERROR - emhass.web_server - No data was retrieved ...". The first lines are
# start-up chatter; the reason is in the ERROR lines and the tail.
_LOG_PREFIX = re.compile(r"^(?:DEBUG|INFO|WARNING|ERROR|CRITICAL) - [\w.]+ - ")
_ERROR_LEVELS = ("ERROR", "CRITICAL")
_ERROR_SUMMARY_LIMIT = 300


def summarise_error_body(body: str) -> str:
    """Boil an EMHASS error response down to the lines that say what went wrong.

    A body that is not a JSON list of strings is returned as it came, trimmed.
    """
    text = body.strip()
    try:
        lines = json.loads(text)
    except ValueError:
        return text[:_ERROR_SUMMARY_LIMIT]
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        return text[:_ERROR_SUMMARY_LIMIT]
    errors = [line for line in lines if line.startswith(_ERROR_LEVELS)]
    chosen = errors or lines[-3:]
    cleaned = [_LOG_PREFIX.sub("", line).strip() for line in chosen]
    summary = "; ".join(dict.fromkeys(line for line in cleaned if line))
    return summary[:_ERROR_SUMMARY_LIMIT] or text[:_ERROR_SUMMARY_LIMIT]


class EmhassError(Exception):
    """Base error for EMHASS communication."""


class EmhassConnectionError(EmhassError):
    """EMHASS could not be reached."""


class EmhassApiError(EmhassError):
    """EMHASS rejected a request."""

    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body

    @property
    def infeasible(self) -> bool:
        """Whether this rejection is EMHASS reporting an infeasible solve.

        EMHASS 0.18 answers one with HTTP 400 and its action log, once its
        relaxed-LP retry has failed too, where older versions answered 200 and
        left ``infeasible`` in last-run.
        """
        return self.status == 400 and "infeasible" in self.body.lower()


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
                        f"{method} {path} returned {response.status}: {summarise_error_body(body)}",
                        status=response.status,
                        body=body,
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
        """Return health details from a responsive EMHASS server.

        EMHASS reports HTTP 503 / ``degraded`` before its first run (or when
        a requested freshness window expires). Accept that health payload for
        discovery without treating it as a usable plan; use
        :meth:`async_get_last_run` for the optimisation result.
        """
        try:
            return await self._request("GET", ENDPOINT_HEALTHZ)
        except EmhassApiError as err:
            if err.status != 503:
                raise
            try:
                health = json.loads(err.body)
            except ValueError:
                raise err from None
            if not isinstance(health, dict) or health.get("status") != "degraded":
                raise
            versions = health.get("versions")
            if not isinstance(versions, dict):
                raise
            version = versions.get("emhass")
            if not isinstance(version, str) or not version.strip():
                raise
            return health

    async def async_get_version(self) -> str | None:
        """Installed EMHASS version, or None if it cannot be determined."""
        health = await self.async_healthz()
        versions = health.get("versions") or {}
        return versions.get("emhass")

    # -- configuration --------------------------------------------------------

    async def async_get_config(self) -> dict[str, Any]:
        return await self._request("GET", ENDPOINT_GET_CONFIG)

    async def async_set_config(self, config: dict[str, Any]) -> None:
        """Write EMHASS's configuration verbatim.

        Called at setup (through :meth:`async_set_config_merged`) so that
        ``params.pkl`` exists -- every ``/action/*`` request fails without it --
        and by the manual settings export.
        Per-run values are sent as runtime parameters instead, which override
        whatever is stored here.

        This is **not** a patch: EMHASS rebuilds its config from its packaged
        defaults and overlays only the keys present in ``config``, so any key
        left out reverts to that default. Prefer
        :meth:`async_set_config_merged` unless a full reset to EMHASS's
        defaults plus exactly these keys is actually what's wanted.
        """
        await self._request("POST", ENDPOINT_SET_CONFIG, json=config, expect_json=False)

    async def async_set_config_merged(self, patch: dict[str, Any]) -> None:
        """Update EMHASS's configuration without clobbering unrelated keys.

        ``/set-config`` rebuilds EMHASS's config from its packaged defaults and
        overlays only the keys given in one request -- it is not a patch. A
        caller that posts a single corrected key silently resets every other
        custom value (a load sensor, a time step, a solar API key, ...) back to
        EMHASS's shipped default. The only safe way to change a subset of keys
        is to fetch the currently *effective* config, overlay the intended
        changes on top of that, and post the whole thing back.
        """
        current = await self.async_get_config()
        current.update(patch)
        await self.async_set_config(current)

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
        an infeasible solve returns a perfectly successful HTTP 200. From
        EMHASS 0.18 it returns 400 instead, which is turned back into the same
        infeasible last-run here, so the caller keeps one path for both.
        """
        try:
            await self.async_run_action(action, runtime_params)
        except EmhassApiError as err:
            if not err.infeasible:
                raise
            _LOGGER.debug("EMHASS %s was infeasible: %s", action, err)
            # Shaped like the 200 path's last-run: status "ok", no
            # error_message. Diagnostics and the cards treat either of those
            # as a failed run and would never show the run as infeasible.
            return (
                LastRun(
                    status="ok",
                    timestamp=datetime.now(UTC),
                    action=action,
                    infeasible=True,
                ),
                None,
            )
        last_run, plan = await asyncio.gather(self.async_get_last_run(), self.async_get_plan())
        return last_run, plan

"""Tests for EmhassClient's health probes and safe config merge.

EMHASS's own ``/set-config`` is not a patch: it rebuilds its configuration
from packaged defaults and overlays only the keys present in one request,
silently resetting everything else back to those defaults. This is what
turned one earlier fix (a stale load sensor) into a second, unrelated
regression (a stale optimisation time step) purely because the second
``/set-config`` call did not repeat the first fix's keys.
``async_set_config_merged`` is the fix: fetch, merge, and post the whole
thing back.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientConnectionError
import pytest

from custom_components.emhass_companion.api import (
    EmhassApiError,
    EmhassClient,
    EmhassConnectionError,
    summarise_error_body,
)
from custom_components.emhass_companion.config_flow import _async_validate_connection


def _client() -> EmhassClient:
    return EmhassClient(AsyncMock(), "http://emhass.local:5000")


def _health_session(status: int, body: str) -> MagicMock:
    response = MagicMock(status=status)
    response.text = AsyncMock(return_value=body)
    response.json = AsyncMock(side_effect=lambda **kwargs: json.loads(body))
    session = MagicMock()
    session.request.return_value.__aenter__.return_value = response
    return session


def _health_payload(version: str = "0.18.8") -> dict:
    return {
        "status": "degraded",
        "boot_ts": "2026-11-04T12:29:26Z",
        "last_run_ts": None,
        "last_run_status": None,
        "versions": {"emhass": version, "python": "3.12.16", "schema_version": "1.0"},
    }


@pytest.mark.parametrize("status", [200, 503])
async def test_health_and_version_accept_responsive_emhass(status):
    payload = _health_payload()
    if status == 200:
        payload.update(status="ok", last_run_ts="2026-11-04T13:00:00Z")
    client = EmhassClient(_health_session(status, json.dumps(payload)), "http://emhass:5000")

    assert await client.async_healthz() == payload
    assert await client.async_get_version() == "0.18.8"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (503, "<html>Service unavailable</html>"),
        (503, "null"),
        (503, "[]"),
        (503, '{"status": "degraded"}'),
        (503, '{"status": "degraded", "versions": []}'),
        (503, '{"status": "degraded", "versions": {"emhass": null}}'),
        (503, '{"status": "degraded", "versions": {"emhass": 18}}'),
        (503, '{"status": "degraded", "versions": {"emhass": " "}}'),
        (503, '{"status": "error", "versions": {"emhass": "0.18.8"}}'),
        (500, json.dumps(_health_payload())),
        (502, json.dumps(_health_payload())),
        (404, json.dumps(_health_payload())),
    ],
)
async def test_health_rejects_unrecognized_error_responses(status, body):
    client = EmhassClient(_health_session(status, body), "http://emhass:5000")

    with pytest.raises(EmhassApiError) as raised:
        await client.async_get_version()

    assert raised.value.status == status
    assert raised.value.body == body


async def test_other_endpoints_still_reject_degraded_503():
    client = EmhassClient(_health_session(503, json.dumps(_health_payload())), "http://emhass:5000")

    with pytest.raises(EmhassApiError, match="returned 503"):
        await client.async_get_config()


@pytest.mark.parametrize("error", [TimeoutError(), ClientConnectionError("unreachable")])
async def test_health_network_errors_still_raise(error):
    session = MagicMock()
    session.request.side_effect = error
    client = EmhassClient(session, "http://emhass:5000")

    with pytest.raises(EmhassConnectionError):
        await client.async_get_version()


@pytest.mark.parametrize(
    ("version", "expected_error"), [("0.18.8", ""), ("0.17.8", "version_too_old")]
)
async def test_connection_validation_accepts_fresh_install_and_checks_version(
    version, expected_error
):
    session = _health_session(503, json.dumps(_health_payload(version)))

    with patch(
        "custom_components.emhass_companion.config_flow.async_get_clientsession",
        return_value=session,
    ):
        result = await _async_validate_connection(MagicMock(), "http://emhass:5000")

    assert result == (version, expected_error)


async def test_merge_preserves_keys_not_in_the_patch():
    client = _client()
    client.async_get_config = AsyncMock(
        return_value={
            "sensor_power_load_no_var_loads": "sensor.house_load",
            "var_model": "sensor.house_load",
            "optimization_time_step": 30,
            "num_lags": 48,
            "solcast_api_key": "unrelated-secret",
        }
    )
    client.async_set_config = AsyncMock()

    await client.async_set_config_merged({"optimization_time_step": 15})

    client.async_set_config.assert_awaited_once()
    (posted,), _kwargs = client.async_set_config.await_args
    assert posted["optimization_time_step"] == 15
    # Nothing outside the patch may be touched, however unrelated or secret.
    assert posted["solcast_api_key"] == "unrelated-secret"
    assert posted["sensor_power_load_no_var_loads"] == "sensor.house_load"
    assert posted["var_model"] == "sensor.house_load"
    assert posted["num_lags"] == 48


async def test_merge_overwrites_only_the_patched_keys():
    client = _client()
    client.async_get_config = AsyncMock(return_value={"a": 1, "b": 2})
    client.async_set_config = AsyncMock()

    await client.async_set_config_merged({"b": 99, "c": 3})

    (posted,), _kwargs = client.async_set_config.await_args
    assert posted == {"a": 1, "b": 99, "c": 3}


async def test_merge_fetches_before_it_writes():
    """A stale read would merge a patch onto outdated data and still clobber."""
    client = _client()
    order: list[str] = []

    async def _get():
        order.append("get")
        return {}

    async def _set(_config):
        order.append("set")

    client.async_get_config = _get
    client.async_set_config = _set

    await client.async_set_config_merged({"x": 1})

    assert order == ["get", "set"]


_INFEASIBLE_BODY = (
    '["INFO - emhass.web_server - >> Performing naive-mpc-optim...",'
    "\"WARNING - emhass.web_server - Optimization failed with status: 'infeasible'. "
    'Retrying with relaxed constraints (Continuous LP)...",'
    '"ERROR - emhass.web_server - Relaxed optimization also failed with status: infeasible"]'
)


async def test_infeasible_400_comes_back_as_an_infeasible_run():
    """EMHASS 0.18 answers an infeasible solve with 400, not 200 + last-run."""
    client = _client()
    client.async_run_action = AsyncMock(
        side_effect=EmhassApiError("returned 400", status=400, body=_INFEASIBLE_BODY)
    )
    client.async_get_last_run = AsyncMock()

    last_run, plan = await client.async_optimize("naive-mpc-optim", {})

    assert last_run.infeasible is True
    # Same shape as the 200 path, so diagnostics and the cards say
    # "infeasible" rather than "run failed".
    assert last_run.ok
    assert last_run.error_message is None
    assert last_run.timestamp is not None
    assert plan is None
    client.async_get_last_run.assert_not_awaited()


async def test_other_400s_still_raise():
    client = _client()
    client.async_run_action = AsyncMock(
        side_effect=EmhassApiError("returned 400", status=400, body="KeyError: 'soc_init'")
    )

    with pytest.raises(EmhassApiError):
        await client.async_optimize("naive-mpc-optim", {})


_STARTUP_CHATTER = [
    "INFO - emhass.web_server - >> Setting input data dict",
    "INFO - emhass.web_server - Setting up needed data",
    "INFO - emhass.web_server - EMHASS 0.18.8 | Python 3.12.16 | CVXPY 1.9.3 (Highs)",
]


def test_error_summary_picks_the_error_lines_over_startup_chatter():
    body = json.dumps(
        [
            *_STARTUP_CHATTER,
            "ERROR - emhass.retrieve_hass - No data was retrieved for any day.",
            "ERROR - emhass.retrieve_hass - No data was retrieved for any day.",
            "ERROR - emhass.web_server - Check sensor names.",
        ]
    )

    assert summarise_error_body(body) == ("No data was retrieved for any day.; Check sensor names.")


def test_error_summary_without_error_lines_keeps_the_tail():
    body = json.dumps([*_STARTUP_CHATTER, "WARNING - emhass.web_server - Slow."])

    summary = summarise_error_body(body)

    assert summary.startswith("Setting up needed data;")
    assert summary.endswith("Slow.")


def test_error_summary_leaves_other_bodies_alone_and_trims_them():
    assert summarise_error_body("KeyError: 'soc_init'") == "KeyError: 'soc_init'"
    assert summarise_error_body("<html>" + "x" * 500) == ("<html>" + "x" * 500)[:300]
    assert summarise_error_body('{"a": 1}') == '{"a": 1}'

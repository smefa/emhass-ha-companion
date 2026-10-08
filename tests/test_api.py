"""Tests for EmhassClient's safe config merge.

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

from unittest.mock import AsyncMock

import pytest

from custom_components.emhass_companion.api import EmhassApiError, EmhassClient


def _client() -> EmhassClient:
    return EmhassClient(AsyncMock(), "http://emhass.local:5000")


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

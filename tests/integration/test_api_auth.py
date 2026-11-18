"""Authentication repairs open the token form and reload only after validation."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import ACTION_MPC, DOMAIN
from custom_components.emhass_companion.coordinator import EmhassCoordinator
from custom_components.emhass_companion.deferrable import DeferrableRegistry


def _session(status):
    session = MagicMock()

    def request(method, url, **kwargs):
        response = MagicMock(status=200 if url.endswith("/healthz") else status)
        body = (
            json.dumps({"versions": {"emhass": "0.18.11"}})
            if url.endswith("/healthz")
            else '{"status": "no-run"}'
        )
        response.text = AsyncMock(return_value=body)
        response.json = AsyncMock(return_value=json.loads(body))
        context = MagicMock()
        context.__aenter__.return_value = response
        return context

    session.request.side_effect = request
    return session


async def test_setup_401_opens_repair_and_reauth_validates_then_reloads(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"url": "http://emhass:5000"}, options={"keep": "options"}
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.emhass_companion.async_get_clientsession", return_value=_session(401)
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    issue_id = f"config_entry_reauth_{DOMAIN}_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue("homeassistant", issue_id)
    assert issue is not None
    flow_id = issue.data["flow_id"]
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1
    flow = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]
    assert flow["step_id"] == "reauth_confirm"

    with patch(
        "custom_components.emhass_companion.config_flow.async_get_clientsession",
        return_value=_session(401),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"api_token": "incorrect-token"}
        )
    assert result["errors"] == {"base": "invalid_auth"}
    assert "api_token" not in entry.data
    # Never put the submitted secret back into the displayed form.
    assert "incorrect-token" not in str(result["data_schema"])

    with (
        patch(
            "custom_components.emhass_companion.config_flow.async_get_clientsession",
            return_value=_session(200),
        ),
        patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload,
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"api_token": "correct-test-token"}
        )
        await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.data["api_token"] == "correct-test-token"
    assert entry.data["url"] == "http://emhass:5000"
    assert entry.options == {"keep": "options"}
    reload.assert_awaited_once_with(entry.entry_id)
    assert ir.async_get(hass).async_get_issue("homeassistant", issue_id) is None


async def test_runtime_401_starts_repair_without_counting_a_failed_solve(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://emhass:5000"})
    entry.add_to_hass(hass)
    client = EmhassClient(
        _session(401), entry.data["url"], on_auth_error=lambda: entry.async_start_reauth(hass)
    )
    loads = DeferrableRegistry(hass, entry)
    loads.sync()
    coordinator = EmhassCoordinator(hass, entry, client, loads)
    previous = coordinator.data

    async def rejected_run(action):
        await client.async_run_action(action, {})

    with (
        patch.object(coordinator, "_run", rejected_run),
        patch.object(coordinator, "_track_run_failed_issue") as run_issue,
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await coordinator.async_run(ACTION_MPC)
    await hass.async_block_till_done()
    assert coordinator.data is previous
    assert coordinator._no_list_run_failed_at is None
    run_issue.assert_not_called()
    assert (
        ir.async_get(hass).async_get_issue(
            "homeassistant", f"config_entry_reauth_{DOMAIN}_{entry.entry_id}"
        )
        is not None
    )


@pytest.mark.parametrize(
    "submitted",
    [{}, {"api_token": ""}, {"api_token": "replacement-token"}, {"clear_api_token": True}],
)
async def test_reconfigure_preserves_replaces_or_clears_token(hass, submitted):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"url": "http://emhass:5000", "api_token": "existing-token"}
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    with (
        patch(
            "custom_components.emhass_companion.config_flow._async_validate_connection",
            AsyncMock(return_value=("0.18.11", "")),
        ) as validate,
        patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"url": "http://emhass:5000", **submitted}
        )
        await hass.async_block_till_done()
    expected = (
        "" if submitted.get("clear_api_token") else submitted.get("api_token", "existing-token")
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["api_token"] == expected
    assert validate.await_args.args[2] == expected


async def test_changed_address_does_not_receive_previous_token(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"url": "http://old:5000", "api_token": "old-server-token"}
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    with (
        patch(
            "custom_components.emhass_companion.config_flow._async_validate_connection",
            AsyncMock(return_value=("", "invalid_auth")),
        ) as validate,
    ):
        for _ in range(2):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"url": "http://new:5000"}
            )
            assert result["errors"] == {"base": "invalid_auth"}
            assert validate.await_args.args[2] == ""
    assert entry.data["api_token"] == "old-server-token"

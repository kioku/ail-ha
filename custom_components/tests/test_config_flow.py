"""Tests for config flow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.ail.config_flow import ConfigFlow, InvalidAuth, MFARequired
from custom_components.ail.const import (
    CONF_MFA_CODE,
    CONF_PASSWORD,
    CONF_SESSION_STATE,
    CONF_USERNAME,
)


@pytest.mark.asyncio
async def test_test_credentials_closes_client_on_success(hass):
    """Ensure config flow closes the client after successful login."""
    flow = ConfigFlow()
    flow.hass = hass

    with patch(
        "custom_components.ail.config_flow.AILEnergyClient.login",
        new=AsyncMock(return_value=True),
    ), patch(
        "custom_components.ail.config_flow.AILEnergyClient.close",
        new=AsyncMock(),
    ) as close_client:
        await flow._test_credentials(
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"}
        )
        close_client.assert_awaited()


@pytest.mark.asyncio
async def test_test_credentials_closes_client_on_invalid_auth(hass):
    """Ensure config flow closes the client when auth fails."""
    flow = ConfigFlow()
    flow.hass = hass

    with patch(
        "custom_components.ail.config_flow.AILEnergyClient.login",
        new=AsyncMock(return_value=False),
    ), patch(
        "custom_components.ail.config_flow.AILEnergyClient.close",
        new=AsyncMock(),
    ) as close_client:
        with pytest.raises(InvalidAuth):
            await flow._test_credentials(
                {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "wrong"}
            )
        close_client.assert_awaited()


@pytest.mark.asyncio
async def test_test_credentials_stores_session_state(hass):
    """Successful auth should persist the reusable session state."""
    flow = ConfigFlow()
    flow.hass = hass
    user_input = {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"}

    with patch(
        "custom_components.ail.config_flow.AILEnergyClient.login",
        new=AsyncMock(return_value=True),
    ), patch(
        "custom_components.ail.config_flow.AILEnergyClient.export_session_state",
        return_value={"token": "token", "meter_id": "1", "cookies": []},
    ), patch(
        "custom_components.ail.config_flow.AILEnergyClient.close",
        new=AsyncMock(),
    ):
        await flow._test_credentials(user_input)

    assert user_input[CONF_SESSION_STATE]["token"] == "token"


@pytest.mark.asyncio
async def test_test_credentials_raises_mfa_required_without_closing_pending_client(
    hass,
):
    """Preserve the in-progress auth client when MFA is required."""
    flow = ConfigFlow()
    flow.hass = hass

    with patch(
        "custom_components.ail.config_flow.AILEnergyClient.login",
        new=AsyncMock(return_value=False),
    ), patch(
        "custom_components.ail.config_flow.AILEnergyClient.is_mfa_pending",
        return_value=True,
    ), patch(
        "custom_components.ail.config_flow.AILEnergyClient.close",
        new=AsyncMock(),
    ) as close_client:
        with pytest.raises(MFARequired):
            await flow._test_credentials(
                {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"}
            )

    close_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_step_user_closes_stale_mfa_client_before_new_login(hass):
    """A new login attempt should close any previous pending MFA client."""
    flow = ConfigFlow()
    flow.hass = hass
    stale_client = SimpleNamespace(close=AsyncMock())
    flow._auth_client = stale_client
    flow.auth_data = {
        CONF_USERNAME: "old@example.com",
        CONF_PASSWORD: "old-secret",
    }

    with patch.object(
        flow, "_test_credentials", new=AsyncMock()
    ) as test_credentials, patch.object(
        flow, "async_step_tariff", new=AsyncMock(return_value={"type": "form"})
    ) as async_step_tariff:
        result = await flow.async_step_user(
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"}
        )

    stale_client.close.assert_awaited_once()
    assert flow._auth_client is None
    test_credentials.assert_awaited_once()
    async_step_tariff.assert_awaited_once()
    assert flow.auth_data == {
        CONF_USERNAME: "user@example.com",
        CONF_PASSWORD: "secret",
    }
    assert result == {"type": "form"}


@pytest.mark.asyncio
async def test_async_step_mfa_closes_client_when_session_is_expired(hass):
    """Expired MFA state should close and clear the pending auth client."""
    flow = ConfigFlow()
    flow.hass = hass
    flow.auth_data = {
        CONF_USERNAME: "user@example.com",
        CONF_PASSWORD: "secret",
    }
    flow._auth_client = SimpleNamespace(
        is_mfa_pending=lambda: False,
        close=AsyncMock(),
    )

    result = await flow.async_step_mfa({CONF_MFA_CODE: "123456"})

    assert result["type"] == "form"
    assert result["step_id"] == "mfa"
    assert result["errors"] == {"base": "mfa_session_expired"}
    assert flow.auth_data is None
    assert flow._auth_client is None


@pytest.mark.asyncio
async def test_reauth_updates_credentials_and_preserves_entry_data(hass):
    """Successful reauth should replace auth state without losing tariff data."""
    flow = ConfigFlow()
    flow.hass = hass
    flow.context = {}
    entry = SimpleNamespace(
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "old-secret",
            CONF_SESSION_STATE: {"token": "old-token"},
            "fixed_tariff": True,
        }
    )

    async def authenticate(user_input):
        user_input[CONF_SESSION_STATE] = {
            "token": "new-token",
            "meter_id": "1",
            "cookies": [],
        }

    with patch.object(flow, "_get_reauth_entry", return_value=entry), patch.object(
        flow, "_test_credentials", new=AsyncMock(side_effect=authenticate)
    ), patch.object(
        flow,
        "async_update_reload_and_abort",
        return_value={"type": "abort", "reason": "reauth_successful"},
    ) as update_entry:
        await flow.async_step_reauth(entry.data)
        result = await flow.async_step_reauth_confirm({CONF_PASSWORD: "new-secret"})

    assert result == {"type": "abort", "reason": "reauth_successful"}
    update_entry.assert_called_once_with(
        entry,
        data_updates={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "new-secret",
            CONF_SESSION_STATE: {
                "token": "new-token",
                "meter_id": "1",
                "cookies": [],
            },
        },
    )


@pytest.mark.asyncio
async def test_reauth_continues_through_mfa(hass):
    """Reauth should retain the entry while an MFA challenge is pending."""
    flow = ConfigFlow()
    flow.hass = hass
    flow.context = {}
    entry = SimpleNamespace(
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "old-secret",
        }
    )

    with patch.object(flow, "_get_reauth_entry", return_value=entry), patch.object(
        flow, "_test_credentials", new=AsyncMock(side_effect=MFARequired)
    ), patch.object(
        flow, "async_step_mfa", new=AsyncMock(return_value={"type": "form"})
    ) as mfa_step:
        await flow.async_step_reauth(entry.data)
        result = await flow.async_step_reauth_confirm({CONF_PASSWORD: "new-secret"})

    assert result == {"type": "form"}
    assert flow.auth_data == {
        CONF_USERNAME: "user@example.com",
        CONF_PASSWORD: "new-secret",
    }
    assert flow._reauth_entry is entry
    mfa_step.assert_awaited_once()


@pytest.mark.asyncio
async def test_mfa_completion_finishes_reauth(hass):
    """A successful MFA submission should update and reload the failed entry."""
    flow = ConfigFlow()
    flow.hass = hass
    flow.context = {}
    entry = SimpleNamespace(data={CONF_USERNAME: "user@example.com"})
    flow._reauth_entry = entry
    flow.auth_data = {
        CONF_USERNAME: "user@example.com",
        CONF_PASSWORD: "new-secret",
    }
    flow._auth_client = SimpleNamespace(
        is_mfa_pending=lambda: True,
        submit_mfa_code=AsyncMock(return_value=True),
        export_session_state=lambda: {
            "token": "new-token",
            "meter_id": "1",
            "cookies": [],
        },
        close=AsyncMock(),
    )

    with patch.object(
        flow,
        "async_update_reload_and_abort",
        return_value={"type": "abort", "reason": "reauth_successful"},
    ) as update_entry:
        result = await flow.async_step_mfa({CONF_MFA_CODE: "123456"})

    assert result == {"type": "abort", "reason": "reauth_successful"}
    update_entry.assert_called_once()
    assert (
        update_entry.call_args.kwargs["data_updates"][CONF_SESSION_STATE]["token"]
        == "new-token"
    )

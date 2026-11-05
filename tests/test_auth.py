from unittest.mock import Mock, patch

from pocket_agent.system.auth import _try_refresh, get_access_token
from pocket_agent.system.constants import (
    DEFAULT_GEMINI_TOKEN_URL,
    DEFAULT_OPENAI_TOKEN_URL,
    OAUTH_TOKEN_URLS,
    PROVIDER_GEMINI_OAUTH,
    PROVIDER_OPENAI,
)


def test_oauth_token_urls_constant():
    """驗證 OAUTH_TOKEN_URLS 常數存在且包含預期之 Provider 端點"""
    assert PROVIDER_OPENAI in OAUTH_TOKEN_URLS
    assert OAUTH_TOKEN_URLS[PROVIDER_OPENAI] == DEFAULT_OPENAI_TOKEN_URL
    assert PROVIDER_GEMINI_OAUTH in OAUTH_TOKEN_URLS
    assert OAUTH_TOKEN_URLS[PROVIDER_GEMINI_OAUTH] == DEFAULT_GEMINI_TOKEN_URL
    assert OAUTH_TOKEN_URLS["gemini-oauth"] == DEFAULT_GEMINI_TOKEN_URL


def test_try_refresh_success():
    """驗證 Token 過期時 _try_refresh 成功刷新流程"""
    fake_token = {"access_token": "old_acc", "refresh_token": "valid_refresh", "expires_at": 1000}
    mock_resp = Mock(status_code=200)
    mock_resp.json.return_value = {"access_token": "new_acc", "expires_in": 3600}

    with (
        patch("httpx.post", return_value=mock_resp) as mock_post,
        patch("pocket_agent.system.auth._save_token") as mock_save,
    ):
        result = _try_refresh("openai", fake_token, "client_id_123")
        assert result is not None
        assert result["access_token"] == "new_acc"
        mock_post.assert_called_once()
        mock_save.assert_called_once_with("openai", result)


def test_try_refresh_network_or_http_error():
    """驗證刷新失敗時回傳 None 且不拋出未捕捉異常"""
    fake_token = {"access_token": "old_acc", "refresh_token": "bad_refresh"}
    mock_resp = Mock(status_code=400)

    with patch("httpx.post", return_value=mock_resp):
        result = _try_refresh("openai", fake_token, "client_id_123")
        assert result is None


def test_get_access_token_expired_refreshes_successfully():
    """驗證過期 Token 會自動觸發 _try_refresh 並回傳新 Token，不觸發瀏覽器流程"""
    expired_token = {"access_token": "old_acc", "refresh_token": "valid_refresh", "expires_at": 0}

    with (
        patch("pocket_agent.system.auth._load_token", return_value=expired_token),
        patch("pocket_agent.system.auth._try_refresh", return_value={"access_token": "refreshed_acc"}),
        patch("pocket_agent.system.auth._browser_oauth") as mock_browser,
    ):
        token = get_access_token(provider="openai")
        assert token == "refreshed_acc"
        mock_browser.assert_not_called()

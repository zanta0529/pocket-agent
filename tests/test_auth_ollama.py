import os
import sys
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from pocket_agent.system.auth import fetch_available_models


def test_fetch_available_models_ollama():
    """測試抓取 Ollama 模型時是否能正確處理 OLLAMA_BASE_URL 與 API 回應"""
    # 模擬 httpx.Client 與其 get 方法
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"models": [{"name": "llama3:8b"}, {"name": "qwen2.5:7b"}]}

    mock_client_instance = MagicMock()
    mock_client_instance.get.return_value = mock_response

    # httpx.Client 當作 context manager 使用 (with httpx.Client() as client:)
    mock_client_class = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client_instance

    with (
        patch("pocket_agent.system.auth.httpx.Client", mock_client_class),
        patch("pocket_agent.system.auth.load_config", return_value={}),
        patch("dotenv.load_dotenv"),
    ):
        with patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://192.168.1.100:11434"}):
            models = fetch_available_models(provider="ollama")

            assert len(models) == 2
            assert "llama3:8b" in models
            assert "qwen2.5:7b" in models

            # 驗證 Client 初始化參數
            mock_client_class.assert_called_with(trust_env=False)

            # 驗證打向的 URL 是否正確組合
            mock_client_instance.get.assert_called_once()
            args, kwargs = mock_client_instance.get.call_args
            assert args[0] == "http://192.168.1.100:11434/api/tags"


def test_fetch_available_models_ollama_v1_suffix():
    """測試若 OLLAMA_BASE_URL 以 /v1 結尾，是否能正確處理 (避免打向 /v1/api/tags)"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"models": [{"name": "gemma:2b"}]}

    mock_client_instance = MagicMock()
    mock_client_instance.get.return_value = mock_response

    mock_client_class = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client_instance

    with (
        patch("pocket_agent.system.auth.httpx.Client", mock_client_class),
        patch("pocket_agent.system.auth.load_config", return_value={}),
        patch("dotenv.load_dotenv"),
    ):
        with patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://192.168.1.100:11434/v1"}):
            models = fetch_available_models(provider="ollama")

            assert models == ["gemma:2b"]

            args, kwargs = mock_client_instance.get.call_args
            assert args[0] == "http://192.168.1.100:11434/api/tags"


def test_fetch_available_models_ollama_exception():
    """測試若 Ollama 連線失敗，不應拋出例外，應回傳空陣列"""
    mock_client_instance = MagicMock()
    mock_client_instance.get.side_effect = Exception("Connection Timeout")

    mock_client_class = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client_instance

    with (
        patch("pocket_agent.system.auth.httpx.Client", mock_client_class),
        patch("pocket_agent.system.auth.load_config", return_value={}),
        patch("dotenv.load_dotenv"),
    ):
        models = fetch_available_models(provider="ollama")
        assert models == []


def test_explicit_ollama_client_uses_env():
    from pocket_agent.system.auth import get_openai_client

    with (
        patch(
            "pocket_agent.system.config.config.get",
            side_effect=lambda k, d=None: "http://environment:11434" if k == "ollama_base_url" else "ollama",
        ),
        patch("dotenv.load_dotenv"),
        patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://environment:11434"}),
        patch("pocket_agent.system.auth.httpx.AsyncClient") as http,
        patch("openai.AsyncOpenAI") as client,
    ):
        result = get_openai_client(provider="ollama", model="test-model")
        http.assert_called_once_with(trust_env=False, timeout=ANY)
        client.assert_called_once_with(
            api_key="ollama",
            base_url="http://environment:11434/v1",
            default_headers={},
            http_client=http.return_value,
            timeout=ANY,
        )
        assert result is client.return_value


def test_explicit_openai_client_preserves_headers():
    from pocket_agent.system.auth import CODEX_BASE_URL, get_openai_client

    with (
        patch("pocket_agent.system.config.config.get", return_value="openai"),
        patch("pocket_agent.system.auth.get_access_token", return_value="mock-token") as token,
        patch("pocket_agent.system.auth._extract_account_id", return_value="mock-account"),
        patch("openai.AsyncOpenAI") as client,
    ):
        get_openai_client(provider="openai")
        token.assert_called_once_with(provider="openai")
        client.assert_called_once_with(
            api_key="mock-token",
            base_url=CODEX_BASE_URL,
            http_client=None,
            default_headers={
                "originator": "pocket-agent",
                "OpenAI-Beta": "responses=experimental",
                "chatgpt-account-id": "mock-account",
            },
            timeout=ANY,
        )


def test_explicit_ollama_environment_fallback():
    from pocket_agent.system.auth import get_openai_client
    from pocket_agent.system.config import config

    with (
        patch.object(config.json_provider, "get", return_value=None),
        patch("dotenv.load_dotenv"),
        patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://environment:11434/"}),
        patch("pocket_agent.system.auth.httpx.AsyncClient"),
        patch("openai.AsyncOpenAI") as client,
    ):
        # Allow ConfigManager to do its job reading from EnvConfigProvider
        get_openai_client(provider="ollama")
        assert client.call_args.kwargs["base_url"] == "http://environment:11434/v1"

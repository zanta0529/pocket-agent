"""測試通用 OpenAI-Compatible 供應商（OpenAICompatibleCustomProvider）與引擎整合。"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pocket_agent.engines.chat_engine import StandardChatCompletionsEngine
from pocket_agent.engines.factory import EngineFactory
from pocket_agent.system.config import AuthException, EnvConfigProvider, MissingKeyException
from pocket_agent.system.constants import PROVIDER_OPENAI_COMPATIBLE, SENSITIVE_ENV_EXACT_KEYS
from pocket_agent.system.providers import PROVIDER_STRATEGIES, SUPPORTED_PROVIDERS, get_provider_strategy
from pocket_agent.system.providers.openai_compatible_provider import (
    OpenAICompatibleCustomProvider,
    _is_loopback_url,
    _is_valid_http_url,
)


def test_registration_and_constants():
    assert PROVIDER_OPENAI_COMPATIBLE == "openai-compatible"
    assert PROVIDER_OPENAI_COMPATIBLE in PROVIDER_STRATEGIES
    assert PROVIDER_OPENAI_COMPATIBLE in SUPPORTED_PROVIDERS
    assert isinstance(get_provider_strategy(PROVIDER_OPENAI_COMPATIBLE), OpenAICompatibleCustomProvider)
    assert "OPENAI_COMPATIBLE_API_KEY" in SENSITIVE_ENV_EXACT_KEYS
    assert EnvConfigProvider.ENV_MAP["providers.openai_compatible.base_url"] == "OPENAI_COMPATIBLE_BASE_URL"
    assert EnvConfigProvider.ENV_MAP["providers.openai_compatible.api_key"] == "OPENAI_COMPATIBLE_API_KEY"


def test_engine_factory_creates_standard_chat_engine():
    dummy_client = MagicMock()
    engine = EngineFactory.create(PROVIDER_OPENAI_COMPATIBLE, dummy_client, "llama-3.3-70b")
    assert isinstance(engine, StandardChatCompletionsEngine)
    assert engine.provider_name == PROVIDER_OPENAI_COMPATIBLE


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:1234/v1", True),
        ("http://127.0.0.1:8000/v1", True),
        ("http://[::1]:8080/v1", True),
        ("https://gateway.corp.internal/v1", False),
        ("https://api.example.com/openai/v1", False),
        ("", False),
    ],
)
def test_is_loopback_url(url: str, expected: bool):
    assert _is_loopback_url(url) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:1234/v1", True),
        ("https://gateway.corp.internal/openai/v1", True),
        ("ftp://example.com/v1", False),
        ("localhost:1234/v1", False),
        ("", False),
    ],
)
def test_is_valid_http_url(url: str, expected: bool):
    assert _is_valid_http_url(url) is expected


def test_get_client_missing_base_url_raises():
    provider = OpenAICompatibleCustomProvider()
    with patch("pocket_agent.system.providers.openai_compatible_provider.config") as mock_cfg:
        mock_cfg.get.return_value = ""
        with pytest.raises(ValueError, match="OpenAI-Compatible"):
            provider.get_client()


def test_get_client_local_without_api_key_uses_placeholder_and_disables_proxy():
    provider = OpenAICompatibleCustomProvider()

    def fake_get(key: str, default=None):
        if key == "providers.openai_compatible.base_url":
            return "http://localhost:1234/v1/"
        if key == "providers.openai_compatible.api_key":
            raise MissingKeyException("missing")
        return default

    captured_http_kwargs: dict = {}
    captured_openai_kwargs: dict = {}

    def fake_async_http_client(**kwargs):
        captured_http_kwargs.update(kwargs)
        return MagicMock()

    def fake_async_openai(**kwargs):
        captured_openai_kwargs.update(kwargs)
        return MagicMock()

    with (
        patch("pocket_agent.system.providers.openai_compatible_provider.config") as mock_cfg,
        patch(
            "pocket_agent.system.providers.openai_compatible_provider.httpx.AsyncClient",
            side_effect=fake_async_http_client,
        ),
        patch(
            "pocket_agent.system.providers.openai_compatible_provider._DefaultAsyncOpenAI",
            side_effect=fake_async_openai,
        ),
    ):
        mock_cfg.get.side_effect = fake_get
        provider.get_client()

    assert captured_http_kwargs["trust_env"] is False
    assert captured_openai_kwargs["base_url"] == "http://localhost:1234/v1"
    assert captured_openai_kwargs["api_key"] == "not-needed"


def test_get_client_remote_gateway_with_api_key_enables_proxy():
    provider = OpenAICompatibleCustomProvider()

    def fake_get(key: str, default=None):
        if key == "providers.openai_compatible.base_url":
            return "https://llm-gateway.corp.internal/openai/v1"
        if key == "providers.openai_compatible.api_key":
            return "sk-corp-secret"
        return default

    captured_http_kwargs: dict = {}
    captured_openai_kwargs: dict = {}

    def fake_async_http_client(**kwargs):
        captured_http_kwargs.update(kwargs)
        return MagicMock()

    def fake_async_openai(**kwargs):
        captured_openai_kwargs.update(kwargs)
        return MagicMock()

    with (
        patch("pocket_agent.system.providers.openai_compatible_provider.config") as mock_cfg,
        patch(
            "pocket_agent.system.providers.openai_compatible_provider.httpx.AsyncClient",
            side_effect=fake_async_http_client,
        ),
        patch(
            "pocket_agent.system.providers.openai_compatible_provider._DefaultAsyncOpenAI",
            side_effect=fake_async_openai,
        ),
    ):
        mock_cfg.get.side_effect = fake_get
        provider.get_client()

    assert captured_http_kwargs["trust_env"] is True
    assert captured_openai_kwargs["base_url"] == "https://llm-gateway.corp.internal/openai/v1"
    assert captured_openai_kwargs["api_key"] == "sk-corp-secret"


def test_get_client_propagates_auth_exception_when_vault_locked():
    provider = OpenAICompatibleCustomProvider()

    def fake_get(key: str, default=None):
        if key == "providers.openai_compatible.base_url":
            return "https://llm-gateway.corp.internal/v1"
        if key == "providers.openai_compatible.api_key":
            raise AuthException("vault locked")
        return default

    with patch("pocket_agent.system.providers.openai_compatible_provider.config") as mock_cfg:
        mock_cfg.get.side_effect = fake_get
        with pytest.raises(AuthException, match="vault locked"):
            provider.get_client()


def test_fetch_models_without_api_key_omits_auth_header():
    provider = OpenAICompatibleCustomProvider()

    def fake_get(key: str, default=None):
        if key == "providers.openai_compatible.base_url":
            return "http://127.0.0.1:1234/v1/"
        if key == "providers.openai_compatible.api_key":
            raise MissingKeyException("missing")
        return default

    fake_resp = SimpleNamespace(
        status_code=200,
        json=lambda: {"data": [{"id": "local-llama"}, {"id": "local-mistral"}]},
    )
    mock_http_client = MagicMock()
    mock_http_client.__enter__.return_value = mock_http_client
    mock_http_client.get.return_value = fake_resp

    with (
        patch("pocket_agent.system.providers.openai_compatible_provider.config") as mock_cfg,
        patch(
            "pocket_agent.system.providers.openai_compatible_provider.httpx.Client",
            return_value=mock_http_client,
        ) as mock_client_cls,
    ):
        mock_cfg.get.side_effect = fake_get
        models = provider.fetch_models()

    assert models == ["local-llama", "local-mistral"]
    mock_client_cls.assert_called_once_with(trust_env=False)
    mock_http_client.get.assert_called_once_with(
        "http://127.0.0.1:1234/v1/models",
        headers={},
        timeout=10,
    )


def test_fetch_models_with_api_key_sends_bearer_header():
    provider = OpenAICompatibleCustomProvider()

    def fake_get(key: str, default=None):
        if key == "providers.openai_compatible.base_url":
            return "https://gateway.corp.internal/v1"
        if key == "providers.openai_compatible.api_key":
            return "sk-test-token"
        return default

    fake_resp = SimpleNamespace(
        status_code=200,
        json=lambda: {"data": [{"id": "corp-gpt"}]},
    )
    mock_http_client = MagicMock()
    mock_http_client.__enter__.return_value = mock_http_client
    mock_http_client.get.return_value = fake_resp

    with (
        patch("pocket_agent.system.providers.openai_compatible_provider.config") as mock_cfg,
        patch(
            "pocket_agent.system.providers.openai_compatible_provider.httpx.Client",
            return_value=mock_http_client,
        ) as mock_client_cls,
    ):
        mock_cfg.get.side_effect = fake_get
        models = provider.fetch_models()

    assert models == ["corp-gpt"]
    mock_client_cls.assert_called_once_with(trust_env=True)
    mock_http_client.get.assert_called_once_with(
        "https://gateway.corp.internal/v1/models",
        headers={"Authorization": "Bearer sk-test-token"},
        timeout=10,
    )

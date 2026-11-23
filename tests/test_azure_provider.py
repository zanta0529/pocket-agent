"""測試 Azure OpenAI / Azure AI Foundry 供應商（AzureOpenAIProvider）與雙模式整合。"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pocket_agent.engines.chat_engine import StandardChatCompletionsEngine
from pocket_agent.engines.factory import EngineFactory
from pocket_agent.system.config import AuthException, EnvConfigProvider, MissingKeyException
from pocket_agent.system.constants import PROVIDER_AZURE, SENSITIVE_ENV_EXACT_KEYS
from pocket_agent.system.providers import PROVIDER_STRATEGIES, SUPPORTED_PROVIDERS, get_provider_strategy
from pocket_agent.system.providers.azure_provider import (
    AzureOpenAIProvider,
    _is_v1_endpoint,
    _normalize_v1_base_url,
)


def test_azure_registration_and_constants():
    assert PROVIDER_AZURE == "azure"
    assert PROVIDER_AZURE in PROVIDER_STRATEGIES
    assert PROVIDER_AZURE in SUPPORTED_PROVIDERS
    assert isinstance(get_provider_strategy(PROVIDER_AZURE), AzureOpenAIProvider)
    assert "AZURE_OPENAI_API_KEY" in SENSITIVE_ENV_EXACT_KEYS
    assert EnvConfigProvider.ENV_MAP["providers.azure.endpoint"] == "AZURE_OPENAI_ENDPOINT"
    assert EnvConfigProvider.ENV_MAP["providers.azure.api_key"] == "AZURE_OPENAI_API_KEY"
    assert EnvConfigProvider.ENV_MAP["providers.azure.api_version"] == "AZURE_OPENAI_API_VERSION"


def test_engine_factory_creates_and_resolves_azure():
    dummy_client = MagicMock()
    dummy_client.base_url = "https://my-resource.openai.azure.com/openai/v1"
    engine = EngineFactory.create(PROVIDER_AZURE, dummy_client, "gpt-4o-deploy")
    assert isinstance(engine, StandardChatCompletionsEngine)
    assert engine.provider_name == PROVIDER_AZURE
    assert EngineFactory.resolve_provider(dummy_client) == PROVIDER_AZURE

    foundry_client = MagicMock()
    foundry_client.base_url = "https://my-proj.services.ai.azure.com/openai/v1"
    assert EngineFactory.resolve_provider(foundry_client) == PROVIDER_AZURE


@pytest.mark.parametrize(
    ("endpoint", "is_v1", "normalized"),
    [
        (
            "https://my-res.openai.azure.com/",
            False,
            "https://my-res.openai.azure.com/openai/v1",
        ),
        (
            "https://my-res.openai.azure.com/openai/v1/",
            True,
            "https://my-res.openai.azure.com/openai/v1",
        ),
        (
            "https://my-proj.services.ai.azure.com/models",
            True,
            "https://my-proj.services.ai.azure.com/models",
        ),
    ],
)
def test_v1_endpoint_normalization(endpoint: str, is_v1: bool, normalized: str):
    assert _is_v1_endpoint(endpoint) is is_v1
    assert _normalize_v1_base_url(endpoint) == normalized


def test_get_client_missing_endpoint_raises():
    provider = AzureOpenAIProvider()
    with patch("pocket_agent.system.providers.azure_provider.config") as mock_cfg:
        mock_cfg.get.return_value = ""
        with pytest.raises(ValueError, match="Azure"):
            provider.get_client()


def test_get_client_v1_mode_when_api_version_blank():
    provider = AzureOpenAIProvider()

    def fake_get(key: str, default=None):
        mapping = {
            "providers.azure.endpoint": "https://my-res.openai.azure.com/",
            "providers.azure.api_key": "az-key-123",
            "providers.azure.api_version": "",
        }
        return mapping.get(key, default)

    captured_kwargs: dict = {}

    def fake_async_openai(**kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    with (
        patch("pocket_agent.system.providers.azure_provider.config") as mock_cfg,
        patch(
            "pocket_agent.system.providers.azure_provider._DefaultAsyncOpenAI",
            side_effect=fake_async_openai,
        ),
    ):
        mock_cfg.get.side_effect = fake_get
        provider.get_client()

    assert captured_kwargs["base_url"] == "https://my-res.openai.azure.com/openai/v1"
    assert captured_kwargs["api_key"] == "az-key-123"


def test_get_client_legacy_mode_when_api_version_set():
    provider = AzureOpenAIProvider()

    def fake_get(key: str, default=None):
        mapping = {
            "providers.azure.endpoint": "https://my-res.openai.azure.com/",
            "providers.azure.api_key": "az-key-legacy",
            "providers.azure.api_version": "2024-10-21",
        }
        return mapping.get(key, default)

    captured_kwargs: dict = {}

    def fake_async_azure(**kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    with (
        patch("pocket_agent.system.providers.azure_provider.config") as mock_cfg,
        patch(
            "pocket_agent.system.providers.azure_provider._DefaultAsyncAzureOpenAI",
            side_effect=fake_async_azure,
        ),
    ):
        mock_cfg.get.side_effect = fake_get
        provider.get_client()

    assert captured_kwargs["azure_endpoint"] == "https://my-res.openai.azure.com"
    assert captured_kwargs["api_key"] == "az-key-legacy"
    assert captured_kwargs["api_version"] == "2024-10-21"


def test_get_client_propagates_auth_and_missing_key_exceptions():
    provider = AzureOpenAIProvider()

    def fake_locked(key: str, default=None):
        if key == "providers.azure.endpoint":
            return "https://my-res.openai.azure.com"
        if key == "providers.azure.api_key":
            raise AuthException("vault locked")
        return default

    with patch("pocket_agent.system.providers.azure_provider.config") as mock_cfg:
        mock_cfg.get.side_effect = fake_locked
        with pytest.raises(AuthException, match="vault locked"):
            provider.get_client()

    def fake_missing(key: str, default=None):
        if key == "providers.azure.endpoint":
            return "https://my-res.openai.azure.com"
        if key == "providers.azure.api_key":
            raise MissingKeyException("missing key")
        return default

    with patch("pocket_agent.system.providers.azure_provider.config") as mock_cfg:
        mock_cfg.get.side_effect = fake_missing
        with pytest.raises(MissingKeyException, match="missing key"):
            provider.get_client()


def test_fetch_models_v1_and_legacy_modes():
    provider = AzureOpenAIProvider()

    # 1. v1 mode
    def fake_get_v1(key: str, default=None):
        mapping = {
            "providers.azure.endpoint": "https://my-res.openai.azure.com",
            "providers.azure.api_key": "az-token",
            "providers.azure.api_version": "",
        }
        return mapping.get(key, default)

    fake_resp = SimpleNamespace(
        status_code=200,
        json=lambda: {"data": [{"id": "gpt-4o"}, {"id": "o3-mini"}]},
    )
    mock_http = MagicMock()
    mock_http.__enter__.return_value = mock_http
    mock_http.get.return_value = fake_resp

    with (
        patch("pocket_agent.system.providers.azure_provider.config") as mock_cfg,
        patch(
            "pocket_agent.system.providers.azure_provider.httpx.Client",
            return_value=mock_http,
        ),
    ):
        mock_cfg.get.side_effect = fake_get_v1
        models = provider.fetch_models()

    assert models == ["gpt-4o", "o3-mini"]
    mock_http.get.assert_called_once_with(
        "https://my-res.openai.azure.com/openai/v1/models",
        headers={"api-key": "az-token", "Authorization": "Bearer az-token"},
        params=None,
        timeout=10,
    )

    # 2. Legacy mode with api_version
    mock_http.get.reset_mock()

    def fake_get_legacy(key: str, default=None):
        mapping = {
            "providers.azure.endpoint": "https://my-res.openai.azure.com",
            "providers.azure.api_key": "az-token",
            "providers.azure.api_version": "2024-10-21",
        }
        return mapping.get(key, default)

    with (
        patch("pocket_agent.system.providers.azure_provider.config") as mock_cfg,
        patch(
            "pocket_agent.system.providers.azure_provider.httpx.Client",
            return_value=mock_http,
        ),
    ):
        mock_cfg.get.side_effect = fake_get_legacy
        models_legacy = provider.fetch_models()

    assert models_legacy == ["gpt-4o", "o3-mini"]
    mock_http.get.assert_called_once_with(
        "https://my-res.openai.azure.com/openai/models",
        headers={"api-key": "az-token", "Authorization": "Bearer az-token"},
        params={"api-version": "2024-10-21"},
        timeout=10,
    )

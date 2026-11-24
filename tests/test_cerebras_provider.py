"""測試 Cerebras Cloud 供應商（CerebrasProvider）與 CerebrasEngine 轉接器整合。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocket_agent.engines.base import (
    EventText,
    EventThinking,
    EventToolCallDone,
    EventUsage,
)
from pocket_agent.engines.cerebras_engine import CerebrasEngine
from pocket_agent.engines.factory import EngineFactory
from pocket_agent.system.config import DEFAULT_CONFIG, AuthException, EnvConfigProvider, MissingKeyException
from pocket_agent.system.constants import PROVIDER_CEREBRAS, PROVIDER_OLLAMA, SENSITIVE_ENV_EXACT_KEYS
from pocket_agent.system.i18n import set_locale
from pocket_agent.system.providers import PROVIDER_STRATEGIES, SUPPORTED_PROVIDERS, get_provider_strategy
from pocket_agent.system.providers.cerebras_provider import CEREBRAS_BASE_URL, CerebrasProvider


class _AsyncChunkStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for chunk in self._chunks:
            yield chunk

    async def close(self):
        self.closed = True


def test_cerebras_registration_and_constants():
    assert PROVIDER_CEREBRAS == "cerebras"
    assert PROVIDER_CEREBRAS in PROVIDER_STRATEGIES
    assert PROVIDER_CEREBRAS in SUPPORTED_PROVIDERS
    assert isinstance(get_provider_strategy(PROVIDER_CEREBRAS), CerebrasProvider)
    assert "CEREBRAS_API_KEY" in SENSITIVE_ENV_EXACT_KEYS
    assert DEFAULT_CONFIG["providers"]["cerebras"] == {
        "api_key": "",
        "base_url": CEREBRAS_BASE_URL,
    }
    assert EnvConfigProvider.ENV_MAP["providers.cerebras.api_key"] == "CEREBRAS_API_KEY"
    assert EnvConfigProvider.ENV_MAP["providers.cerebras.base_url"] == "CEREBRAS_BASE_URL"


def test_engine_factory_creates_and_resolves_cerebras():
    dummy_client = MagicMock()
    dummy_client.base_url = CEREBRAS_BASE_URL
    engine = EngineFactory.create(PROVIDER_CEREBRAS, dummy_client, "gpt-oss-120b")
    assert isinstance(engine, CerebrasEngine)
    assert engine.provider_name == PROVIDER_CEREBRAS
    assert engine.supports_image_generation is False
    assert engine.supports_stream_usage is False
    assert EngineFactory.resolve_provider(dummy_client) == PROVIDER_CEREBRAS

    spoof_client = MagicMock()
    spoof_client.base_url = "https://api.cerebras.ai.evil.com/v1"
    assert EngineFactory.resolve_provider(spoof_client) == PROVIDER_OLLAMA


def test_get_client_uses_default_or_custom_base_url():
    provider = CerebrasProvider()

    def fake_get_default(key: str, default=None):
        mapping = {
            "providers.cerebras.api_key": "csk-test-default",
            "providers.cerebras.base_url": "",
        }
        return mapping.get(key, default)

    captured_kwargs: dict = {}

    def fake_async_openai(**kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    with (
        patch("pocket_agent.system.config.config.get", side_effect=fake_get_default),
        patch("openai.AsyncOpenAI", side_effect=fake_async_openai),
    ):
        provider.get_client()

    assert captured_kwargs["api_key"] == "csk-test-default"
    assert captured_kwargs["base_url"] == CEREBRAS_BASE_URL

    captured_kwargs.clear()

    def fake_get_custom(key: str, default=None):
        mapping = {
            "providers.cerebras.api_key": "csk-test-custom",
            "providers.cerebras.base_url": "https://cerebras-proxy.corp.internal/v1/",
        }
        return mapping.get(key, default)

    with (
        patch("pocket_agent.system.config.config.get", side_effect=fake_get_custom),
        patch("openai.AsyncOpenAI", side_effect=fake_async_openai),
    ):
        provider.get_client()

    assert captured_kwargs["api_key"] == "csk-test-custom"
    assert captured_kwargs["base_url"] == "https://cerebras-proxy.corp.internal/v1"


def test_get_client_invalid_url_and_missing_key_i18n():
    provider = CerebrasProvider()

    def fake_invalid_url(key: str, default=None):
        mapping = {
            "providers.cerebras.api_key": "csk-test",
            "providers.cerebras.base_url": "not-a-valid-url",
        }
        return mapping.get(key, default)

    with patch("pocket_agent.system.config.config.get", side_effect=fake_invalid_url):
        with pytest.raises(ValueError, match="Base URL"):
            provider.get_client()

    def fake_empty_key(key: str, default=None):
        return ""

    with patch("pocket_agent.system.config.config.get", side_effect=fake_empty_key):
        set_locale("zh_TW")
        with pytest.raises(ValueError, match="尚未設定 Cerebras API Key"):
            provider.get_client()

        set_locale("en_US")
        with pytest.raises(ValueError, match="Cerebras API Key is not configured"):
            provider.get_client()


def test_get_client_propagates_auth_and_missing_key_exceptions():
    provider = CerebrasProvider()

    with patch("pocket_agent.system.config.config.get", side_effect=AuthException("vault locked")):
        with pytest.raises(AuthException, match="vault locked"):
            provider.get_client()

    with patch("pocket_agent.system.config.config.get", side_effect=MissingKeyException("missing key")):
        with pytest.raises(MissingKeyException, match="missing key"):
            provider.get_client()


def test_setup_wizard_prompts_base_url_with_retry_and_api_key():
    fake_cfg = MagicMock()
    fake_cfg.get.side_effect = lambda key, default="": CEREBRAS_BASE_URL if key == "providers.cerebras.base_url" else ""

    with (
        patch("rich.prompt.Prompt.ask", side_effect=["invalid-url", "https://custom.cerebras.local/v1/", "csk-new"]),
        patch("rich.console.Console.print"),
    ):
        CerebrasProvider.setup_wizard(fake_cfg)

    fake_cfg.set.assert_any_call("providers.cerebras.base_url", "https://custom.cerebras.local/v1")
    fake_cfg.set.assert_any_call("providers.cerebras.api_key", "csk-new")


def test_fetch_models_success_auth_error_fallback_and_missing_key():
    provider = CerebrasProvider()

    def fake_get(key: str, default=None):
        mapping = {
            "providers.cerebras.api_key": "csk-token",
            "providers.cerebras.base_url": "https://custom.cerebras.local/v1/",
        }
        return mapping.get(key, default)

    ok_resp = SimpleNamespace(status_code=200, json=lambda: {"data": [{"id": "gpt-oss-120b"}, {"id": "qwen-3.8-27b"}]})
    mock_http = MagicMock()
    mock_http.__enter__.return_value = mock_http
    mock_http.get.return_value = ok_resp

    with (
        patch("pocket_agent.system.config.config.get", side_effect=fake_get),
        patch("pocket_agent.system.providers.base.httpx.Client", return_value=mock_http),
    ):
        models = provider.fetch_models()

    assert models == ["gpt-oss-120b", "qwen-3.8-27b"]
    mock_http.get.assert_called_once_with(
        "https://custom.cerebras.local/v1/models",
        headers={"Authorization": "Bearer csk-token"},
        timeout=10,
    )

    auth_resp = SimpleNamespace(status_code=401, text="Unauthorized")
    mock_http.get.return_value = auth_resp
    with (
        patch("pocket_agent.system.config.config.get", side_effect=fake_get),
        patch("pocket_agent.system.providers.base.httpx.Client", return_value=mock_http),
    ):
        assert provider.fetch_models() == []

    mock_http.get.side_effect = RuntimeError("network down")
    with (
        patch("pocket_agent.system.config.config.get", side_effect=fake_get),
        patch("pocket_agent.system.providers.base.httpx.Client", return_value=mock_http),
    ):
        assert provider.fetch_models() == list(CerebrasProvider.fallback_models)

    with patch("pocket_agent.system.config.config.get", side_effect=MissingKeyException("missing")):
        assert provider.fetch_models() == []


def test_cerebras_engine_reasoning_params_and_extra_body():
    engine = CerebrasEngine(client=MagicMock(), model="gpt-oss-120b")

    assert engine.format_reasoning_param("auto") is None
    assert engine.format_reasoning_param(None) is None
    assert engine._build_extra_body(None) is None

    for level in ("low", "medium", "high"):
        param = engine.format_reasoning_param(level)
        assert param == level
        assert engine._build_extra_body(param) == {
            "reasoning_effort": level,
            "reasoning_format": "parsed",
        }

    none_param = engine.format_reasoning_param("none")
    assert none_param == "none"
    assert engine._build_extra_body(none_param) == {"reasoning_effort": "none"}


def test_cerebras_engine_stream_chat_reasoning_usage_and_tools():
    async def _run():
        chunks = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(tool_calls=None, content=None, reasoning="thinking..."),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call_c1",
                                    function=SimpleNamespace(name="read_file", arguments='{"path": "a.py"}'),
                                )
                            ],
                            content="done",
                        ),
                        finish_reason="tool_calls",
                    )
                ],
                usage=SimpleNamespace(total_tokens=150, prompt_tokens=120),
            ),
        ]
        fake_stream = _AsyncChunkStream(chunks)
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_stream)

        engine = CerebrasEngine(client=mock_client, model="gpt-oss-120b")
        events = [
            ev
            async for ev in engine.stream_chat(
                input_items=[{"role": "user", "content": "hi"}],
                instructions="sys",
                tools=[],
                reasoning_param="high",
            )
        ]

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert "stream_options" not in call_kwargs
        assert call_kwargs["extra_body"] == {"reasoning_effort": "high", "reasoning_format": "parsed"}

        assert any(isinstance(e, EventThinking) and e.delta == "thinking..." for e in events)
        assert any(isinstance(e, EventText) and e.delta == "done" for e in events)
        assert any(isinstance(e, EventToolCallDone) and e.call_id == "call_c1" for e in events)
        assert any(isinstance(e, EventUsage) and e.total_tokens == 150 and e.input_tokens == 120 for e in events)
        assert fake_stream.closed is True

    asyncio.run(_run())


def test_cerebras_engine_fetch_context_window_and_cache():
    async def _run():
        mock_client = MagicMock()
        mock_client.base_url = CEREBRAS_BASE_URL
        engine = CerebrasEngine(client=mock_client, model="gpt-oss-120b")
        cache_key = engine._cache_key()
        CerebrasEngine._context_window_cache.pop(cache_key, None)

        try:
            ok_resp = SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "data": [
                        {"id": "qwen-3.8-27b", "limits": {"max_context_length": 65536}},
                        {"id": "gpt-oss-120b", "limits": {"max_context_length": 131072}},
                    ]
                },
            )
            with patch.object(engine, "_http_get", new=AsyncMock(return_value=ok_resp)) as mock_get:
                ctx = await engine.fetch_context_window()

            assert ctx == 131072
            assert engine.context_window() == 131072
            mock_get.assert_awaited_once_with("https://api.cerebras.ai/public/v1/models", timeout=10.0)

            CerebrasEngine._context_window_cache.pop(cache_key, None)
            missing_resp = SimpleNamespace(status_code=200, json=lambda: {"data": []})
            with patch.object(engine, "_http_get", new=AsyncMock(return_value=missing_resp)):
                assert await engine.fetch_context_window() is None

            with patch.object(engine, "_http_get", new=AsyncMock(side_effect=RuntimeError("timeout"))):
                assert await engine.fetch_context_window() is None
        finally:
            CerebrasEngine._context_window_cache.pop(cache_key, None)

    asyncio.run(_run())


def test_cerebras_engine_parse_api_error_429_headers():
    engine = CerebrasEngine(client=MagicMock(), model="gpt-oss-120b")

    exc_retry_after = SimpleNamespace(
        status_code=429,
        response=SimpleNamespace(headers={"Retry-After": "45"}),
    )
    hint_retry = engine.parse_api_error(exc_retry_after)
    assert hint_retry is not None
    assert "45" in hint_retry

    exc_duration = SimpleNamespace(
        status_code=429,
        response=SimpleNamespace(headers={"X-RateLimit-Reset-Tokens-Minute": "1m30s"}),
    )
    hint_duration = engine.parse_api_error(exc_duration)
    assert hint_duration is not None
    assert "1" in hint_duration and "30" in hint_duration

    exc_ts = SimpleNamespace(
        status_code=429,
        response=SimpleNamespace(headers={"X-RateLimit-Reset": "1800000000"}),
    )
    hint_ts = engine.parse_api_error(exc_ts)
    assert hint_ts is not None

    exc_non_429 = SimpleNamespace(
        status_code=400,
        response=SimpleNamespace(headers={"Retry-After": "45"}),
    )
    assert engine.parse_api_error(exc_non_429) is None

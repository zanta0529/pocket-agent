"""測試共用限流標頭解析與 Groq / Azure / Mistral / SambaNova 薄引擎轉接器。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pocket_agent.engines.azure_engine import AzureEngine
from pocket_agent.engines.chat_engine import StandardChatCompletionsEngine
from pocket_agent.engines.factory import EngineFactory
from pocket_agent.engines.groq_engine import GroqEngine
from pocket_agent.engines.mistral_engine import MistralEngine
from pocket_agent.engines.sambanova_engine import SambaNovaEngine
from pocket_agent.system.config import EnvConfigProvider
from pocket_agent.system.constants import (
    PROVIDER_AZURE,
    PROVIDER_GROQ,
    PROVIDER_MISTRAL,
    PROVIDER_SAMBANOVA,
)


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


def test_env_map_includes_all_api_key_providers():
    assert EnvConfigProvider.ENV_MAP["providers.gemini.api_key"] == "GEMINI_API_KEY"
    assert EnvConfigProvider.ENV_MAP["providers.groq.api_key"] == "GROQ_API_KEY"
    assert EnvConfigProvider.ENV_MAP["providers.mistral.api_key"] == "MISTRAL_API_KEY"
    assert EnvConfigProvider.ENV_MAP["providers.sambanova.api_key"] == "SAMBANOVA_API_KEY"


def test_engine_factory_creates_dedicated_cloud_engines():
    dummy_client = MagicMock()

    groq_engine = EngineFactory.create(PROVIDER_GROQ, dummy_client, "llama-3.3-70b-versatile")
    assert isinstance(groq_engine, GroqEngine)
    assert groq_engine.supports_stream_usage is True

    azure_engine = EngineFactory.create(PROVIDER_AZURE, dummy_client, "o4-mini")
    assert isinstance(azure_engine, AzureEngine)
    assert azure_engine.supports_stream_usage is True

    mistral_engine = EngineFactory.create(PROVIDER_MISTRAL, dummy_client, "mistral-large-latest")
    assert isinstance(mistral_engine, MistralEngine)
    assert mistral_engine.supports_stream_usage is False

    samba_engine = EngineFactory.create(PROVIDER_SAMBANOVA, dummy_client, "Meta-Llama-3.3-70B-Instruct")
    assert isinstance(samba_engine, SambaNovaEngine)
    assert samba_engine.supports_stream_usage is True


def test_shared_parse_rate_limit_headers_handles_ms_duration_and_timestamps():
    engine = StandardChatCompletionsEngine(client=MagicMock(), model="test-model")

    exc_ms = SimpleNamespace(status_code=429, response=SimpleNamespace(headers={"Retry-After-Ms": "2500"}))
    hint_ms = engine.parse_api_error(exc_ms)
    assert hint_ms is not None
    assert "2" in hint_ms

    exc_req = SimpleNamespace(
        status_code=429,
        response=SimpleNamespace(headers={"X-RateLimit-Reset-Requests": "7.66s"}),
    )
    hint_req = engine.parse_api_error(exc_req)
    assert hint_req is not None
    assert "7" in hint_req

    exc_tok = SimpleNamespace(
        status_code=429,
        response=SimpleNamespace(headers={"X-RateLimit-Reset-Tokens": "2m59s"}),
    )
    hint_tok = engine.parse_api_error(exc_tok)
    assert hint_tok is not None
    assert "2" in hint_tok and "59" in hint_tok

    exc_ts_ms = SimpleNamespace(
        status_code=429,
        response=SimpleNamespace(headers={"X-RateLimit-Reset": "1800000000000"}),
    )
    assert engine.parse_api_error(exc_ts_ms) is not None

    exc_400 = SimpleNamespace(status_code=400, response=SimpleNamespace(headers={"Retry-After": "10"}))
    assert engine.parse_api_error(exc_400) is None


def test_groq_engine_reasoning_and_fetch_context_window():
    async def _run():
        mock_client = MagicMock()
        mock_client.api_key = "gsk-test"
        mock_client.base_url = "https://api.groq.com/openai/v1"
        engine = GroqEngine(client=mock_client, model="openai/gpt-oss-120b")

        assert engine.format_reasoning_param("auto") is None
        assert engine._build_extra_body(None) is None
        assert engine._build_extra_body(engine.format_reasoning_param("none")) == {"reasoning_effort": "none"}
        assert engine._build_extra_body(engine.format_reasoning_param("high")) == {
            "reasoning_effort": "high",
            "reasoning_format": "parsed",
        }

        cache_key = engine._cache_key()
        GroqEngine._context_window_cache.pop(cache_key, None)
        try:
            ok_resp = SimpleNamespace(
                status_code=200,
                json=lambda: {"data": [{"id": "openai/gpt-oss-120b", "context_window": 131072}]},
            )
            with patch.object(engine, "_http_get", new=AsyncMock(return_value=ok_resp)) as mock_get:
                ctx = await engine.fetch_context_window()

            assert ctx == 131072
            assert engine.context_window() == 131072
            mock_get.assert_awaited_once_with(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": "Bearer gsk-test"},
                timeout=10.0,
            )
        finally:
            GroqEngine._context_window_cache.pop(cache_key, None)

    asyncio.run(_run())


def test_mistral_engine_fetch_context_window():
    async def _run():
        mock_client = MagicMock()
        mock_client.api_key = "msk-test"
        mock_client.base_url = "https://api.mistral.ai/v1"
        engine = MistralEngine(client=mock_client, model="mistral-large-latest")

        cache_key = engine._cache_key()
        MistralEngine._context_window_cache.pop(cache_key, None)
        try:
            ok_resp = SimpleNamespace(
                status_code=200,
                json=lambda: {"data": [{"id": "mistral-large-latest", "max_context_length": 262144}]},
            )
            with patch.object(engine, "_http_get", new=AsyncMock(return_value=ok_resp)) as mock_get:
                ctx = await engine.fetch_context_window()

            assert ctx == 262144
            assert engine.context_window() == 262144
            mock_get.assert_awaited_once_with(
                "https://api.mistral.ai/v1/models",
                headers={"Authorization": "Bearer msk-test"},
                timeout=10.0,
            )
        finally:
            MistralEngine._context_window_cache.pop(cache_key, None)

    asyncio.run(_run())


def test_azure_and_sambanova_engines_reasoning_and_stream_options():
    async def _run():
        fake_stream = _AsyncChunkStream(
            [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(tool_calls=None, content="ok"),
                            finish_reason="stop",
                        )
                    ],
                    usage=SimpleNamespace(total_tokens=20, prompt_tokens=15),
                )
            ]
        )
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_stream)

        azure = AzureEngine(client=mock_client, model="o4-mini")
        assert azure.format_reasoning_param("auto") is None
        assert azure.format_reasoning_param("none") is None
        assert azure._build_extra_body(azure.format_reasoning_param("medium")) == {"reasoning_effort": "medium"}

        events = [
            ev
            async for ev in azure.stream_chat(
                input_items=[{"role": "user", "content": "hi"}],
                instructions="",
                tools=[],
                reasoning_param="medium",
            )
        ]
        assert events
        kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert kwargs["stream_options"] == {"include_usage": True}
        assert kwargs["extra_body"] == {"reasoning_effort": "medium"}

        samba = SambaNovaEngine(client=MagicMock(), model="GLM-5.3")
        assert samba.supports_stream_usage is True
        assert samba.format_reasoning_param("auto") is None
        assert samba.format_reasoning_param("none") is None
        assert samba._build_extra_body(samba.format_reasoning_param("high")) == {"reasoning_effort": "high"}

    asyncio.run(_run())

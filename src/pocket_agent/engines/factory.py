import logging
from typing import Any

from pocket_agent.engines.base import BaseEngine
from pocket_agent.engines.chat_engine import StandardChatCompletionsEngine
from pocket_agent.engines.ollama_engine import OllamaChatEngine
from pocket_agent.engines.openai_engine import OpenAIResponsesEngine
from pocket_agent.engines.openrouter_engine import OpenRouterEngine
from pocket_agent.system.constants import (
    PROVIDER_GEMINI,
    PROVIDER_GEMINI_OAUTH,
    PROVIDER_GROQ,
    PROVIDER_MISTRAL,
    PROVIDER_OLLAMA,
    PROVIDER_OLLAMACLOUD,
    PROVIDER_OPENAI,
    PROVIDER_OPENROUTER,
    PROVIDER_SAMBANOVA,
)

log = logging.getLogger("pocket-agent")

ENGINE_MAP = {
    PROVIDER_OLLAMA: OllamaChatEngine,
    PROVIDER_OLLAMACLOUD: OllamaChatEngine,
    PROVIDER_OPENROUTER: OpenRouterEngine,
    PROVIDER_OPENAI: OpenAIResponsesEngine,
    PROVIDER_GEMINI: StandardChatCompletionsEngine,
    PROVIDER_GEMINI_OAUTH: StandardChatCompletionsEngine,
    PROVIDER_GROQ: StandardChatCompletionsEngine,
    PROVIDER_MISTRAL: StandardChatCompletionsEngine,
    PROVIDER_SAMBANOVA: StandardChatCompletionsEngine,
}


class EngineFactory:
    @staticmethod
    def resolve_provider(client: Any) -> str:
        """未明確指定 provider 時的唯一後備：依 client 的 base_url 推論（向下相容用）。"""
        url_str = str(getattr(client, "base_url", ""))
        if "openai.com" in url_str or "chatgpt.com" in url_str:
            return PROVIDER_OPENAI
        if "openrouter.ai" in url_str:
            return PROVIDER_OPENROUTER
        return PROVIDER_OLLAMA

    @staticmethod
    def create(provider: str, client: Any, model: str) -> BaseEngine:
        p = provider.lower()
        engine_cls = ENGINE_MAP.get(p)
        if not engine_cls:
            raise ValueError(f"EngineFactory 不支援此 Provider: {provider}")

        log.debug("Using %s for %s", engine_cls.__name__, model)
        return engine_cls(client=client, model=model)

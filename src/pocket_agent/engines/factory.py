import logging
from typing import Any
from urllib.parse import urlparse

from pocket_agent.engines.azure_engine import AzureEngine
from pocket_agent.engines.base import BaseEngine
from pocket_agent.engines.cerebras_engine import CerebrasEngine
from pocket_agent.engines.chat_engine import StandardChatCompletionsEngine
from pocket_agent.engines.groq_engine import GroqEngine
from pocket_agent.engines.mistral_engine import MistralEngine
from pocket_agent.engines.ollama_engine import OllamaChatEngine
from pocket_agent.engines.openai_engine import OpenAIResponsesEngine
from pocket_agent.engines.openrouter_engine import OpenRouterEngine
from pocket_agent.engines.sambanova_engine import SambaNovaEngine
from pocket_agent.system.constants import (
    PROVIDER_AZURE,
    PROVIDER_CEREBRAS,
    PROVIDER_GEMINI,
    PROVIDER_GEMINI_OAUTH,
    PROVIDER_GROQ,
    PROVIDER_MISTRAL,
    PROVIDER_OLLAMA,
    PROVIDER_OLLAMACLOUD,
    PROVIDER_OPENAI,
    PROVIDER_OPENAI_API,
    PROVIDER_OPENAI_COMPATIBLE,
    PROVIDER_OPENROUTER,
    PROVIDER_SAMBANOVA,
)

log = logging.getLogger("pocket-agent")

ENGINE_MAP = {
    PROVIDER_OLLAMA: OllamaChatEngine,
    PROVIDER_OLLAMACLOUD: OllamaChatEngine,
    PROVIDER_OPENROUTER: OpenRouterEngine,
    PROVIDER_OPENAI: OpenAIResponsesEngine,
    PROVIDER_OPENAI_API: OpenAIResponsesEngine,
    PROVIDER_GEMINI: StandardChatCompletionsEngine,
    PROVIDER_GEMINI_OAUTH: StandardChatCompletionsEngine,
    PROVIDER_GROQ: GroqEngine,
    PROVIDER_MISTRAL: MistralEngine,
    PROVIDER_SAMBANOVA: SambaNovaEngine,
    PROVIDER_AZURE: AzureEngine,
    PROVIDER_CEREBRAS: CerebrasEngine,
    PROVIDER_OPENAI_COMPATIBLE: StandardChatCompletionsEngine,
}


def _matches_host(host: str, domain: str) -> bool:
    """嚴格比對主機名稱是否為指定網域或其子網域，避免不完整 URL 子字串比對漏洞。"""
    return host == domain or host.endswith(f".{domain}")


class EngineFactory:
    @staticmethod
    def resolve_provider(client: Any) -> str:
        """未明確指定 provider 時的唯一後備：依 client 的 base_url 推論（向下相容用）。"""
        url_str = str(getattr(client, "base_url", "")).strip()
        host = (urlparse(url_str).hostname or "").lower()
        if _matches_host(host, "openai.com") or _matches_host(host, "chatgpt.com"):
            return PROVIDER_OPENAI
        if _matches_host(host, "openrouter.ai"):
            return PROVIDER_OPENROUTER
        if _matches_host(host, "generativelanguage.googleapis.com"):
            return PROVIDER_GEMINI
        if _matches_host(host, "api.groq.com"):
            return PROVIDER_GROQ
        if _matches_host(host, "api.mistral.ai"):
            return PROVIDER_MISTRAL
        if _matches_host(host, "api.sambanova.ai"):
            return PROVIDER_SAMBANOVA
        if _matches_host(host, "api.cerebras.ai") or _matches_host(host, "cerebras.ai"):
            return PROVIDER_CEREBRAS
        if _matches_host(host, "openai.azure.com") or _matches_host(host, "services.ai.azure.com"):
            return PROVIDER_AZURE
        log.warning("EngineFactory.resolve_provider 無法由 base_url (%s) 識別供應商，後備使用 ollama", url_str)
        return PROVIDER_OLLAMA

    @staticmethod
    def create(provider: str, client: Any, model: str) -> BaseEngine:
        p = provider.lower()
        engine_cls = ENGINE_MAP.get(p)
        if not engine_cls:
            raise ValueError(f"EngineFactory 不支援此 Provider: {provider}")

        log.debug("Using %s for %s", engine_cls.__name__, model)
        engine = engine_cls(client=client, model=model, provider=p)
        engine.provider_name = p
        return engine

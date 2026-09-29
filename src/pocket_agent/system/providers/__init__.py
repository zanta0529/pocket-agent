from pocket_agent.system.constants import (
    PROVIDER_OLLAMA,
    PROVIDER_OLLAMACLOUD,
    PROVIDER_OPENAI,
    PROVIDER_OPENROUTER,
)
from pocket_agent.system.providers.base import BaseProvider
from pocket_agent.system.providers.ollama import OllamaLocalProvider
from pocket_agent.system.providers.ollama_cloud import OllamaCloudProvider
from pocket_agent.system.providers.openai_provider import OpenAIProvider
from pocket_agent.system.providers.openrouter import OpenRouterProvider


def get_provider_strategy(provider_name: str) -> BaseProvider:
    strategies = {
        PROVIDER_OPENAI: OpenAIProvider,
        PROVIDER_OLLAMA: OllamaLocalProvider,
        PROVIDER_OLLAMACLOUD: OllamaCloudProvider,
        PROVIDER_OPENROUTER: OpenRouterProvider,
    }
    # 預設回傳 OpenAIProvider (Codex)
    strategy_cls = strategies.get(provider_name.lower() if provider_name else "", OpenAIProvider)
    return strategy_cls()

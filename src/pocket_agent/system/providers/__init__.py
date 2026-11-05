from pocket_agent.system.constants import (
    PROVIDER_GEMINI,
    PROVIDER_GEMINI_OAUTH,
    PROVIDER_GROQ,
    PROVIDER_MISTRAL,
    PROVIDER_OLLAMA,
    PROVIDER_OLLAMACLOUD,
    PROVIDER_OPENAI,
    PROVIDER_OPENAI_API,
    PROVIDER_OPENROUTER,
    PROVIDER_SAMBANOVA,
)
from pocket_agent.system.providers.base import BaseProvider
from pocket_agent.system.providers.gemini_provider import GeminiOAuthProvider, GeminiProvider
from pocket_agent.system.providers.groq_provider import GroqProvider
from pocket_agent.system.providers.mistral_provider import MistralProvider
from pocket_agent.system.providers.ollama import OllamaLocalProvider
from pocket_agent.system.providers.ollama_cloud import OllamaCloudProvider
from pocket_agent.system.providers.openai_provider import OpenAIApiProvider, OpenAIProvider
from pocket_agent.system.providers.openrouter import OpenRouterProvider
from pocket_agent.system.providers.sambanova_provider import SambaNovaProvider

PROVIDER_STRATEGIES = {
    PROVIDER_OPENAI: OpenAIProvider,
    PROVIDER_OPENAI_API: OpenAIApiProvider,
    PROVIDER_OLLAMA: OllamaLocalProvider,
    PROVIDER_OLLAMACLOUD: OllamaCloudProvider,
    PROVIDER_OPENROUTER: OpenRouterProvider,
    PROVIDER_GEMINI: GeminiProvider,
    PROVIDER_GEMINI_OAUTH: GeminiOAuthProvider,
    PROVIDER_GROQ: GroqProvider,
    PROVIDER_SAMBANOVA: SambaNovaProvider,
    PROVIDER_MISTRAL: MistralProvider,
}

SUPPORTED_PROVIDERS = list(PROVIDER_STRATEGIES.keys())


def get_provider_strategy(provider_name: str) -> BaseProvider:
    provider_key = provider_name.lower() if provider_name else ""
    strategy_cls = PROVIDER_STRATEGIES.get(provider_key)
    if not strategy_cls:
        raise ValueError(f"不支援的 Provider: {provider_name}")
    return strategy_cls()

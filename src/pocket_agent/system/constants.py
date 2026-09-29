from typing import Final

PROVIDER_OPENAI: Final[str] = "openai"
PROVIDER_OPENROUTER: Final[str] = "openrouter"
PROVIDER_OLLAMACLOUD: Final[str] = "ollamacloud"
PROVIDER_OLLAMA: Final[str] = "ollama"

SUPPORTED_PROVIDERS: Final[list[str]] = [
    PROVIDER_OPENAI,
    PROVIDER_OPENROUTER,
    PROVIDER_OLLAMACLOUD,
    PROVIDER_OLLAMA,
]

from typing import Final

PROVIDER_OPENAI: Final[str] = "openai"
PROVIDER_OPENROUTER: Final[str] = "openrouter"
PROVIDER_OLLAMACLOUD: Final[str] = "ollamacloud"
PROVIDER_OLLAMA: Final[str] = "ollama"
PROVIDER_GEMINI: Final[str] = "gemini"
PROVIDER_GEMINI_OAUTH: Final[str] = "gemini-oauth"
PROVIDER_GROQ: Final[str] = "groq"

SUPPORTED_PROVIDERS: Final[list[str]] = [
    PROVIDER_OPENAI,
    PROVIDER_OPENROUTER,
    PROVIDER_OLLAMACLOUD,
    PROVIDER_OLLAMA,
    PROVIDER_GEMINI,
    PROVIDER_GEMINI_OAUTH,
    PROVIDER_GROQ,
]

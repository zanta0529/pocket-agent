from typing import Final

PROVIDER_OPENAI: Final[str] = "openai"
PROVIDER_OPENAI_API: Final[str] = "openai-api"
PROVIDER_OPENROUTER: Final[str] = "openrouter"
PROVIDER_OLLAMACLOUD: Final[str] = "ollamacloud"
PROVIDER_OLLAMA: Final[str] = "ollama"
PROVIDER_GEMINI: Final[str] = "gemini"
PROVIDER_GEMINI_OAUTH: Final[str] = "gemini-oauth"
PROVIDER_GROQ: Final[str] = "groq"
PROVIDER_SAMBANOVA: Final[str] = "sambanova"
PROVIDER_MISTRAL: Final[str] = "mistral"

OAUTH_TOKEN_URLS = {
    PROVIDER_GEMINI_OAUTH: "https://oauth2.googleapis.com/token",
    # PROVIDER_OPENAI 的預設 token URL (設定檔若有覆寫則由 auth.py 自行處理)
}

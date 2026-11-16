from pocket_agent.system.providers.base import OpenAICompatibleProvider

GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class GroqProvider(OpenAICompatibleProvider):
    display_name = "Groq Cloud"
    config_key = "providers.groq.api_key"
    api_key_label = "Groq"
    base_url = GROQ_BASE_URL
    fallback_models = (
        "llama-3.1-70b-versatile",
        "llama-3.1-8b-instant",
        "mixtral-8x7b-32768",
        "gemma2-9b-it",
    )

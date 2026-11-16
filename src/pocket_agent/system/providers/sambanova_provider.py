from pocket_agent.system.providers.base import OpenAICompatibleProvider

SAMBANOVA_BASE_URL = "https://api.sambanova.ai/v1"


class SambaNovaProvider(OpenAICompatibleProvider):
    display_name = "SambaNova Cloud"
    config_key = "providers.sambanova.api_key"
    api_key_label = "SambaNova"
    base_url = SAMBANOVA_BASE_URL
    fallback_models = (
        "Meta-Llama-3.1-405B-Instruct",
        "Meta-Llama-3.1-70B-Instruct",
        "Meta-Llama-3.1-8B-Instruct",
    )

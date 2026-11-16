from pocket_agent.system.providers.base import OpenAICompatibleProvider

MISTRAL_BASE_URL = "https://api.mistral.ai/v1"


class MistralProvider(OpenAICompatibleProvider):
    display_name = "Mistral API"
    config_key = "providers.mistral.api_key"
    api_key_label = "Mistral"
    base_url = MISTRAL_BASE_URL
    fallback_models = (
        "mistral-large-latest",
        "mistral-small-latest",
        "open-mistral-nemo",
    )

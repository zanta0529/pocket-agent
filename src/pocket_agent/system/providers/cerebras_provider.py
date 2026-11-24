"""Cerebras Cloud 供應商策略實作。"""

from pocket_agent.system.constants import DEFAULT_STREAM_READ_TIMEOUT_SEC
from pocket_agent.system.providers.base import OpenAICompatibleProvider

CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"


class CerebrasProvider(OpenAICompatibleProvider):
    """Cerebras Cloud 供應商策略（支援自訂 Base URL 與 API Key）。"""

    display_name = "Cerebras Cloud"
    config_key = "providers.cerebras.api_key"
    base_url_config_key = "providers.cerebras.base_url"
    base_url_prompt_i18n_key = "provider_setup.enter_cerebras_base_url"
    api_key_label = "Cerebras"
    base_url = CEREBRAS_BASE_URL
    missing_key_i18n_key = "provider_setup.cerebras_missing_key"
    timeout_seconds = 180.0
    connect_timeout_seconds = 15.0
    read_timeout_seconds = DEFAULT_STREAM_READ_TIMEOUT_SEC
    use_shared_http_client = True
    fail_on_auth_error = True
    fallback_models = ("gpt-oss-120b", "qwen-3.8-27b")

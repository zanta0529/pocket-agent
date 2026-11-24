"""Mistral AI LLM Engine 轉接器實作。"""

from pocket_agent.engines.chat_engine import StandardChatCompletionsEngine
from pocket_agent.system.constants import PROVIDER_MISTRAL
from pocket_agent.system.providers.mistral_provider import MISTRAL_BASE_URL


class MistralEngine(StandardChatCompletionsEngine):
    """Mistral AI Chat Completions 轉接器（支援模型視窗自動探測）。"""

    provider_name = PROVIDER_MISTRAL
    supports_stream_usage = False
    supports_header_quota = True

    async def fetch_context_window(self) -> int | None:
        return await self._fetch_context_window_from_models(
            self._models_endpoint_url(MISTRAL_BASE_URL),
            field_paths=(("max_context_length",), ("context_length",)),
            use_auth=True,
        )

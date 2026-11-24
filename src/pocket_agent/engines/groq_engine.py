"""Groq Cloud LLM Engine 轉接器實作。"""

from typing import Any

from pocket_agent.engines.chat_engine import StandardChatCompletionsEngine
from pocket_agent.system.constants import PROVIDER_GROQ
from pocket_agent.system.providers.groq_provider import GROQ_BASE_URL


class GroqEngine(StandardChatCompletionsEngine):
    """Groq Cloud Chat Completions 轉接器（支援串流用量、推理參數與視窗探測）。"""

    provider_name = PROVIDER_GROQ
    supports_stream_usage = True

    def format_reasoning_param(self, reasoning_effort: str | None) -> str | None:
        """將統一的推理強度轉為 Groq 支援的等級；auto 不傳以避免非推理模型報錯。"""
        effort = (reasoning_effort or "auto").strip().lower()
        if effort in ("auto", ""):
            return None
        if effort in ("low", "medium", "high", "none"):
            return effort
        return None

    def _build_extra_body(self, reasoning_param: Any) -> dict[str, Any] | None:
        if not isinstance(reasoning_param, str) or not reasoning_param.strip():
            return None
        effort = reasoning_param.strip().lower()
        if effort == "none":
            return {"reasoning_effort": "none"}
        if effort in ("low", "medium", "high"):
            return {"reasoning_effort": effort, "reasoning_format": "parsed"}
        return None

    async def fetch_context_window(self) -> int | None:
        return await self._fetch_context_window_from_models(
            self._models_endpoint_url(GROQ_BASE_URL),
            field_paths=(("context_window",), ("max_context_length",)),
            use_auth=True,
        )

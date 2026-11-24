"""Azure OpenAI / Azure AI Foundry LLM Engine 轉接器實作。"""

from typing import Any

from pocket_agent.engines.chat_engine import StandardChatCompletionsEngine
from pocket_agent.system.constants import PROVIDER_AZURE


class AzureEngine(StandardChatCompletionsEngine):
    """Azure OpenAI Chat Completions 轉接器（支援串流用量與推理強度參數）。"""

    provider_name = PROVIDER_AZURE
    supports_stream_usage = True
    supports_header_quota = True

    def format_reasoning_param(self, reasoning_effort: str | None) -> str | None:
        """僅對明確指定的 low/medium/high 傳遞 reasoning_effort，避免舊系列或非推理部署報錯。"""
        effort = (reasoning_effort or "auto").strip().lower()
        if effort in ("low", "medium", "high"):
            return effort
        return None

    def _build_extra_body(self, reasoning_param: Any) -> dict[str, Any] | None:
        if not isinstance(reasoning_param, str) or not reasoning_param.strip():
            return None
        effort = reasoning_param.strip().lower()
        if effort in ("low", "medium", "high"):
            return {"reasoning_effort": effort}
        return None

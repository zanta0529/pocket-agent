"""Cerebras Cloud LLM Engine 轉接器實作。"""

from typing import Any
from urllib.parse import urlparse

from pocket_agent.engines.chat_engine import StandardChatCompletionsEngine
from pocket_agent.system.constants import PROVIDER_CEREBRAS

_DEFAULT_PUBLIC_MODELS_URL = "https://api.cerebras.ai/public/v1/models"


class CerebrasEngine(StandardChatCompletionsEngine):
    """Cerebras Cloud Chat Completions 轉接器（支援推理參數、視窗探測與限流提示）。"""

    provider_name = PROVIDER_CEREBRAS
    supports_stream_usage = False

    def format_reasoning_param(self, reasoning_effort: str | None) -> str | None:
        """將統一的推理強度轉為 Cerebras 支援的等級；auto 不傳以避免非推理模型報錯。"""
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

    def _public_models_url(self) -> str:
        url_str = str(getattr(self.client, "base_url", "") or "").strip()
        parsed = urlparse(url_str)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            path = parsed.path.rstrip("/")
            if path.endswith("/v1"):
                prefix = path[:-3]
                return f"{parsed.scheme}://{parsed.netloc}{prefix}/public/v1/models"
            return f"{parsed.scheme}://{parsed.netloc}/public/v1/models"
        return _DEFAULT_PUBLIC_MODELS_URL

    async def fetch_context_window(self) -> int | None:
        return await self._fetch_context_window_from_models(
            self._public_models_url(),
            field_paths=(("limits", "max_context_length"), ("context_length",), ("max_context_length",)),
            use_auth=False,
        )

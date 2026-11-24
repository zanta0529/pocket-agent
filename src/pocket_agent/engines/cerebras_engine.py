"""Cerebras Cloud LLM Engine 轉接器實作。"""

import logging
import time
from typing import Any
from urllib.parse import urlparse

from pocket_agent.engines.chat_engine import StandardChatCompletionsEngine
from pocket_agent.system.constants import PROVIDER_CEREBRAS

log = logging.getLogger("pocket-agent")

_DEFAULT_PUBLIC_MODELS_URL = "https://api.cerebras.ai/public/v1/models"
_RATE_LIMIT_RESET_HEADERS = (
    "retry-after",
    "x-ratelimit-reset-requests-minute",
    "x-ratelimit-reset-tokens-minute",
    "x-ratelimit-reset-requests-hour",
    "x-ratelimit-reset-tokens-hour",
    "x-ratelimit-reset-requests-day",
    "x-ratelimit-reset-tokens-day",
    "x-ratelimit-reset",
)


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
        try:
            resp = await self._http_get(self._public_models_url(), timeout=10.0)
            if resp.status_code != 200:
                return None
            data = resp.json().get("data", [])
            for item in data:
                if not isinstance(item, dict) or item.get("id") != self.model:
                    continue
                limits = item.get("limits")
                ctx = limits.get("max_context_length") if isinstance(limits, dict) else None
                if ctx is None:
                    ctx = item.get("context_length") or item.get("max_context_length")
                if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0:
                    key = self._cache_key()
                    self._context_window_cache[key] = (ctx, time.time() + 3600)
                    return ctx
        except Exception as e:
            log.debug("查詢 Cerebras max_context_length 失敗: %s", e)
        return None

    def parse_api_error(self, exc: Exception) -> str | None:
        """解析 HTTP 429 回應標頭中的 retry-after 與 x-ratelimit-reset-* 重置提示。"""
        if getattr(exc, "status_code", None) != 429:
            return None

        try:
            response = getattr(exc, "response", None)
            headers = getattr(response, "headers", None)
            if not headers or not hasattr(headers, "items"):
                return None
            header_map = {str(k).lower(): str(v).strip() for k, v in headers.items()}

            for header_key in _RATE_LIMIT_RESET_HEADERS:
                raw = header_map.get(header_key)
                if not raw:
                    continue
                dur = self.parse_duration_seconds(raw)
                if dur is not None:
                    return self.format_reset_time_hint(resets_in_seconds=dur)
                try:
                    val = float(raw)
                    if val > 1_000_000_000_000:
                        return self.format_reset_time_hint(resets_at=val / 1000.0)
                    if val > 1_000_000_000:
                        return self.format_reset_time_hint(resets_at=val)
                except ValueError, TypeError:
                    continue
        except Exception:
            return None
        return None

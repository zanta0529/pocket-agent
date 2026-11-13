import logging
from typing import Any

from pocket_agent.engines.base import QuotaInfo
from pocket_agent.engines.chat_engine import StandardChatCompletionsEngine

log = logging.getLogger("pocket-agent")


class OpenRouterEngine(StandardChatCompletionsEngine):
    """OpenRouter uses the standard chat.completions API.
    Inherits `<think>` tag parsing and tool-call streaming from StandardChatCompletionsEngine."""

    provider_name = "openrouter"
    supports_stream_usage = True

    def format_reasoning_param(self, reasoning_effort: str | None) -> Any:
        """將統一的推理強度轉換為 OpenRouter unified reasoning 參數。"""
        effort = (reasoning_effort or "auto").strip().lower()
        if effort in ("auto", ""):
            return None
        if effort == "none":
            return {"enabled": False}
        return {"effort": effort}

    def _build_extra_body(self, reasoning_param: Any) -> dict[str, Any] | None:
        if isinstance(reasoning_param, dict) and reasoning_param:
            return {"reasoning": reasoning_param}
        return None

    async def fetch_context_window(self) -> int | None:
        import time

        import httpx

        try:
            api_key = getattr(self.client, "api_key", None)
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            async with httpx.AsyncClient(timeout=10.0) as http_client:
                resp = await http_client.get("https://openrouter.ai/api/v1/models", headers=headers)
                if resp.status_code != 200:
                    return None
                data = resp.json().get("data", [])
                for m in data:
                    if m.get("id") == self.model:
                        ctx = m.get("context_length")
                        if isinstance(ctx, int) and ctx > 0:
                            key = self._cache_key()
                            self._context_window_cache[key] = (ctx, time.time() + 3600)
                            return ctx
        except Exception as e:
            log.debug("查詢 OpenRouter context_length 失敗: %s", e)
        return None

    async def get_quota(self) -> QuotaInfo:
        import httpx

        try:
            api_key = self.client.api_key
        except AttributeError:
            return QuotaInfo(is_supported=False, message="無法取得 API Key")

        async with httpx.AsyncClient() as http_client:
            try:
                response = await http_client.get(
                    "https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {api_key}"}, timeout=10.0
                )
                response.raise_for_status()
                data = response.json().get("data", {})

                limit = data.get("limit")
                remaining = data.get("limit_remaining")

                used = None
                if limit is not None and remaining is not None:
                    used = limit - remaining

                daily_used = data.get("usage_daily")

                free_limit = None
                free_remaining = None
                free_used = None

                free_reqs = data.get("free_model_daily_requests")
                if isinstance(free_reqs, dict):
                    free_limit = free_reqs.get("limit")
                    free_remaining = free_reqs.get("remaining")
                    free_used = free_reqs.get("used")

                return QuotaInfo(
                    is_supported=True,
                    limit=limit,
                    remaining=remaining,
                    used=used,
                    daily_used=daily_used,
                    free_requests_limit=free_limit,
                    free_requests_remaining=free_remaining,
                    free_requests_used=free_used,
                )
            except Exception as e:
                log.error(f"Failed to fetch OpenRouter quota: {e}")
                return QuotaInfo(is_supported=False, message=f"查詢 OpenRouter 額度失敗：{str(e)}")

    def parse_api_error(self, exc: Exception) -> str | None:
        """Parse OpenRouter APIStatusError for reset time or remedy hint."""
        import ast
        import re
        from datetime import datetime, timezone

        try:
            status_code = getattr(exc, "status_code", None)

            # Attempt to extract JSON from error string
            text = str(exc)
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                return None

            parsed = ast.literal_eval(match.group())
            if not isinstance(parsed, dict):
                return None

            error_data = parsed.get("error", {})
            metadata = error_data.get("metadata", {})

            if status_code == 402:
                remedy = metadata.get("remedy_hint")
                if remedy:
                    return f"\n\n💡 **建議做法**：{remedy}"
                return None

            if status_code != 429:
                return None

            headers = metadata.get("headers", {})

            reset_ms = headers.get("X-RateLimit-Reset")
            if not reset_ms:
                return None

            reset_ts = int(reset_ms) / 1000.0
            reset_dt = datetime.fromtimestamp(reset_ts, tz=timezone.utc).astimezone()

            import time

            now_ts = time.time()
            if reset_ts > now_ts:
                secs = int(reset_ts - now_ts)
                hours, remainder = divmod(secs, 3600)
                minutes, seconds = divmod(remainder, 60)
                if hours:
                    countdown = f"{hours} 小時 {minutes} 分 {seconds} 秒"
                elif minutes:
                    countdown = f"{minutes} 分 {seconds} 秒"
                else:
                    countdown = f"{seconds} 秒"

                return f"\n\n⏳ **重置時間**：{reset_dt.strftime('%Y-%m-%d %H:%M:%S')} (距今約 {countdown})"
            else:
                return f"\n\n⏳ **重置時間**：應已於 {reset_dt.strftime('%Y-%m-%d %H:%M:%S')} 重置"

        except Exception:
            return None

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Callable, Optional

from pocket_agent.engines.base import (
    BaseEngine,
    EventImageGenerationDone,
    EventText,
    EventThinking,
    EventToolCallDelta,
    EventToolCallDone,
    EventToolCallStart,
    EventUsage,
    QuotaInfo,
    usage_field,
)
from pocket_agent.tools.image import disable_model_param

log = logging.getLogger("pocket-agent")

_PLAN_DISPLAY_NAMES: dict[str, str] = {
    "free": "Free",
    "plus": "Plus",
    "pro": "Pro",
    "prolite": "Pro Lite",
    "pro_lite": "Pro Lite",
    "business": "Business",
    "team": "Team",
    "enterprise": "Enterprise",
    "edu": "Edu",
}


def format_plan_name(raw_plan: str | None) -> str | None:
    """將後端或 JWT 回傳的 plan_type 正規化為易讀的顯示名稱（例如 'business' -> 'Business'）。"""
    if raw_plan is None:
        return None
    text = str(raw_plan).strip()
    if not text:
        return None
    mapped = _PLAN_DISPLAY_NAMES.get(text.lower())
    if mapped:
        return mapped
    if text.islower():
        return text.replace("_", " ").title()
    return text


@dataclass
class OpenAIRateLimitDetails:
    """封裝從 OpenAI / Codex 錯誤回應、Header、JWT 或 Usage API 解析出的限流與額度資訊。"""

    is_usage_limit: bool = False
    plan_type: str | None = None
    resets_at: float | None = None
    resets_in_seconds: float | None = None


class OpenAIErrorQuotaStrategy(ABC):
    """
    OpenAI / Codex 錯誤解析與額度查詢之抽象策略 (Strategy Pattern)。
    封裝不同認證與後端模式（Codex OAuth vs OpenAI API Key）的差異。
    """

    def __init__(self, client: Any, model: str):
        self.client = client
        self.model = model

    @staticmethod
    def _extract_headers(exc: Exception, error_dict: dict[str, Any] | None = None) -> dict[str, str]:
        header_map: dict[str, str] = {}
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers and hasattr(headers, "items"):
            for k, v in headers.items():
                header_map[str(k).lower()] = str(v)

        if isinstance(error_dict, dict):
            meta_headers = error_dict.get("headers")
            if not isinstance(meta_headers, dict) and isinstance(error_dict.get("metadata"), dict):
                meta_headers = error_dict["metadata"].get("headers")
            if isinstance(meta_headers, dict):
                for k, v in meta_headers.items():
                    header_map.setdefault(str(k).lower(), str(v))
        return header_map

    @staticmethod
    def _parse_timestamp_or_duration(raw: object) -> tuple[float | None, float | None]:
        """將原始值解析為 (resets_at, resets_in_seconds)。"""
        if raw is None or isinstance(raw, bool):
            return None, None
        try:
            val = float(raw)
            if val > 1_000_000_000_000:
                return val / 1000.0, None
            if val > 1_000_000_000:
                return val, None
            if val >= 0:
                return None, val
        except ValueError, TypeError:
            pass

        dur = BaseEngine.parse_duration_seconds(raw)
        if dur is not None:
            return None, dur
        return None, None

    @classmethod
    def _extract_window_reset(cls, window: object) -> tuple[float | None, float | None]:
        if not isinstance(window, dict):
            return None, None
        resets_at: float | None = None
        resets_in_seconds: float | None = None

        raw_at = window.get("resets_at") if window.get("resets_at") is not None else window.get("reset_at")
        if raw_at is not None:
            at_val, in_val = cls._parse_timestamp_or_duration(raw_at)
            resets_at = at_val
            if resets_in_seconds is None:
                resets_in_seconds = in_val

        raw_in = (
            window.get("resets_in_seconds")
            if window.get("resets_in_seconds") is not None
            else window.get("reset_after_seconds")
        )
        if raw_in is not None:
            _, in_val = cls._parse_timestamp_or_duration(raw_in)
            if in_val is not None:
                resets_in_seconds = in_val

        return resets_at, resets_in_seconds

    def _parse_common_details(self, exc: Exception) -> OpenAIRateLimitDetails:
        err_text = str(exc)
        error_dict, clean_msg = BaseEngine.extract_error_payload(exc)
        header_map = self._extract_headers(exc, error_dict)

        resets_at: float | None = None
        resets_in_seconds: float | None = None
        plan_raw: str | None = (
            header_map.get("x-codex-plan-type") or header_map.get("x-codex-plan") or header_map.get("openai-plan")
        )

        # 1. 檢查 HTTP Response Headers
        retry_after_ms = header_map.get("retry-after-ms")
        if retry_after_ms:
            try:
                resets_in_seconds = float(retry_after_ms) / 1000.0
            except ValueError:
                pass

        retry_after = header_map.get("retry-after")
        if retry_after and resets_in_seconds is None:
            _, in_sec = self._parse_timestamp_or_duration(retry_after)
            if in_sec is not None:
                resets_in_seconds = in_sec

        # 檢查 Codex primary / secondary window headers
        sec_used_raw = header_map.get("x-codex-secondary-used-percent")
        sec_exhausted = False
        if sec_used_raw:
            try:
                sec_exhausted = float(sec_used_raw) >= 100.0
            except ValueError:
                pass

        if sec_exhausted:
            win_reset_raw = (
                header_map.get("x-codex-secondary-reset-at")
                or header_map.get("x-codex-secondary-resets-at")
                or header_map.get("x-codex-secondary-reset-after-seconds")
            )
        else:
            win_reset_raw = (
                header_map.get("x-codex-primary-reset-at")
                or header_map.get("x-codex-primary-resets-at")
                or header_map.get("x-codex-primary-reset-after-seconds")
                or header_map.get("x-codex-secondary-reset-at")
                or header_map.get("x-codex-secondary-resets-at")
                or header_map.get("x-codex-secondary-reset-after-seconds")
            )

        reset_ts_raw = (
            header_map.get("x-ratelimit-reset")
            or header_map.get("x-codex-resets-at")
            or header_map.get("x-codex-reset-at")
            or win_reset_raw
            or header_map.get("x-ratelimit-reset-requests")
            or header_map.get("x-ratelimit-reset-tokens")
        )
        if reset_ts_raw:
            at_val, in_val = self._parse_timestamp_or_duration(reset_ts_raw)
            if at_val is not None:
                resets_at = at_val
            if in_val is not None and resets_in_seconds is None:
                resets_in_seconds = in_val

        # 2. 檢查結構化 Error Payload（相容 OpenAI SDK v3 已解包之 exc.body）
        is_usage_limit = False
        if isinstance(error_dict, dict):
            err_type = str(error_dict.get("type", "")).lower()
            err_code = str(error_dict.get("code", "")).lower()
            msg_lower = str(error_dict.get("message", clean_msg)).lower()

            if (
                "usage_limit" in err_type
                or "usage_limit" in err_code
                or "insufficient_quota" in err_type
                or "insufficient_quota" in err_code
                or "usage limit" in msg_lower
                or error_dict.get("limit_reached") is True
            ):
                is_usage_limit = True

            body_plan = error_dict.get("plan_type") or error_dict.get("plan")
            if body_plan:
                plan_raw = str(body_plan)

            at_val, in_val = self._extract_window_reset(error_dict)
            if resets_at is None and at_val is not None:
                resets_at = at_val
            if resets_in_seconds is None and in_val is not None:
                resets_in_seconds = in_val

            # 若 payload 內含 rate_limit (如 primary_window / secondary_window)
            rate_limit = error_dict.get("rate_limit")
            if isinstance(rate_limit, dict):
                if rate_limit.get("limit_reached") is True or rate_limit.get("allowed") is False:
                    is_usage_limit = True
                pri_win = rate_limit.get("primary_window")
                sec_win = rate_limit.get("secondary_window")
                target_win = (
                    sec_win
                    if isinstance(sec_win, dict) and float(sec_win.get("used_percent") or 0) >= 100.0
                    else (pri_win or sec_win)
                )
                w_at, w_in = self._extract_window_reset(target_win)
                if resets_at is None and w_at is not None:
                    resets_at = w_at
                if resets_in_seconds is None and w_in is not None:
                    resets_in_seconds = w_in

        if not is_usage_limit and "usage limit has been reached" in err_text.lower():
            is_usage_limit = True

        return OpenAIRateLimitDetails(
            is_usage_limit=is_usage_limit,
            plan_type=format_plan_name(plan_raw),
            resets_at=resets_at,
            resets_in_seconds=resets_in_seconds,
        )

    @abstractmethod
    def parse_error_details(self, exc: Exception) -> OpenAIRateLimitDetails:
        """同步解析例外中的額度與重置時間資訊。"""

    async def enrich_error_details_async(self, details: OpenAIRateLimitDetails) -> OpenAIRateLimitDetails:
        """非同步補齊缺少的額度與重置時間資訊（預設不進行額外查詢）。"""
        return details

    def format_error_hint(self, details: OpenAIRateLimitDetails) -> str | None:
        hint = BaseEngine.format_reset_time_hint(
            resets_at=details.resets_at,
            resets_in_seconds=details.resets_in_seconds,
        )
        lines: list[str] = []
        if details.is_usage_limit:
            plan_suffix = f" (方案：{details.plan_type})" if details.plan_type else ""
            lines.append(f"\n\n💳 **API 用量已達上限**{plan_suffix}")

        if hint:
            lines.append(hint.strip())

        if lines:
            return "\n\n".join(lines)
        return None

    async def fetch_quota(self) -> QuotaInfo:
        return QuotaInfo(is_supported=False, message="此 Provider 不支援透過 API 直接查詢額度。")


class CodexOAuthQuotaStrategy(OpenAIErrorQuotaStrategy):
    """
    針對 OpenAI Codex OAuth (ChatGPT Plus / Pro / Business / Team) 的錯誤解析與額度策略。
    支援從 Error Body、x-codex-* Headers、OAuth JWT Token 與 /backend-api/wham/usage 取得方案與重置時間。
    """

    # 短期快取避免頻繁打 /wham/usage 觸發 429
    _usage_cache: tuple[dict[str, Any], float] | None = None

    def _extract_jwt_plan_type(self) -> str | None:
        from pocket_agent.system.auth import _decode_jwt_payload, get_user_info

        api_key = getattr(self.client, "api_key", None)
        if isinstance(api_key, str) and api_key.count(".") >= 2:
            payload = _decode_jwt_payload(api_key)
            auth_claim = payload.get("https://api.openai.com/auth", {})
            if isinstance(auth_claim, dict):
                plan = auth_claim.get("chatgpt_plan_type")
                if plan:
                    return format_plan_name(str(plan))

        base_url = str(getattr(self.client, "base_url", ""))
        if "chatgpt.com" in base_url:
            try:
                info = get_user_info("openai")
                plan = info.get("plan") if isinstance(info, dict) else None
                if plan:
                    return format_plan_name(str(plan))
            except Exception:
                pass
        return None

    def _resolve_wham_usage_url(self) -> str:
        base_url = str(getattr(self.client, "base_url", "")).rstrip("/")
        if "/backend-api" in base_url:
            prefix = base_url.split("/backend-api")[0]
            return f"{prefix}/backend-api/wham/usage"
        return "https://chatgpt.com/backend-api/wham/usage"

    async def _fetch_wham_usage_data(self, *, use_cache: bool = True) -> dict[str, Any] | None:
        if use_cache and self.__class__._usage_cache is not None:
            cached_data, expire_ts = self.__class__._usage_cache
            if time.time() < expire_ts:
                return cached_data

        api_key = getattr(self.client, "api_key", None)
        if not isinstance(api_key, str) or not api_key.strip():
            return None

        import httpx

        from pocket_agent.system.auth import _SSL_VERIFY, _extract_account_id

        headers = {
            "Authorization": f"Bearer {api_key}",
            "originator": "pocket-agent",
        }
        account_id = _extract_account_id(api_key)
        if account_id:
            headers["chatgpt-account-id"] = account_id

        url = self._resolve_wham_usage_url()
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=_SSL_VERIFY) as http_client:
                resp = await http_client.get(url, headers=headers)
                if resp.status_code != 200:
                    return None
                data = resp.json()
                if isinstance(data, dict):
                    self.__class__._usage_cache = (data, time.time() + 60.0)
                    return data
        except Exception as e:
            log.debug("查詢 Codex /wham/usage 失敗: %s", e)
        return None

    def parse_error_details(self, exc: Exception) -> OpenAIRateLimitDetails:
        details = self._parse_common_details(exc)
        if details.is_usage_limit and not details.plan_type:
            details.plan_type = self._extract_jwt_plan_type()
        return details

    async def enrich_error_details_async(self, details: OpenAIRateLimitDetails) -> OpenAIRateLimitDetails:
        if not details.is_usage_limit:
            return details
        if details.plan_type and (details.resets_at is not None or details.resets_in_seconds is not None):
            return details

        data = await self._fetch_wham_usage_data(use_cache=False)
        if not isinstance(data, dict):
            return details

        if not details.plan_type and data.get("plan_type"):
            details.plan_type = format_plan_name(str(data["plan_type"]))

        if details.resets_at is None and details.resets_in_seconds is None:
            rate_limit = data.get("rate_limit")
            if isinstance(rate_limit, dict):
                pri_win = rate_limit.get("primary_window")
                sec_win = rate_limit.get("secondary_window")
                target_win = (
                    sec_win
                    if isinstance(sec_win, dict) and float(sec_win.get("used_percent") or 0) >= 100.0
                    else (pri_win or sec_win)
                )
                w_at, w_in = self._extract_window_reset(target_win)
                details.resets_at = w_at
                details.resets_in_seconds = w_in

        return details

    async def fetch_quota(self) -> QuotaInfo:
        data = await self._fetch_wham_usage_data(use_cache=True)
        plan_display = self._extract_jwt_plan_type()
        if isinstance(data, dict) and data.get("plan_type"):
            plan_display = format_plan_name(str(data["plan_type"])) or plan_display

        if not isinstance(data, dict):
            if plan_display:
                return QuotaInfo(
                    is_supported=True,
                    plan_type=plan_display,
                    extra_lines=["Codex 即時視窗額度：目前無法連線至 /wham/usage 端點取得百分比。"],
                )
            return QuotaInfo(is_supported=False, message="無法取得 Codex OAuth 額度資訊。")

        extra_lines: list[str] = []
        rate_limit = data.get("rate_limit")
        if isinstance(rate_limit, dict):
            for key, label in (("primary_window", "5 小時視窗"), ("secondary_window", "7 天週額度視窗")):
                win = rate_limit.get(key)
                if not isinstance(win, dict):
                    continue
                used_pct = win.get("used_percent")
                w_at, w_in = self._extract_window_reset(win)
                reset_hint = BaseEngine.format_reset_time_hint(resets_at=w_at, resets_in_seconds=w_in)
                clean_reset = reset_hint.replace("\n\n⏳ **重置時間**：", "") if reset_hint else "未知"
                if used_pct is not None:
                    extra_lines.append(f"Codex {label}: 已用 {used_pct}% (重置時間：{clean_reset})")

        return QuotaInfo(
            is_supported=True,
            plan_type=plan_display,
            extra_lines=extra_lines or None,
        )


class OpenAIApiQuotaStrategy(OpenAIErrorQuotaStrategy):
    """針對標準 OpenAI API (API Key 模式) 的錯誤解析與額度策略。"""

    def parse_error_details(self, exc: Exception) -> OpenAIRateLimitDetails:
        return self._parse_common_details(exc)

    async def fetch_quota(self) -> QuotaInfo:
        return QuotaInfo(
            is_supported=False,
            message="OpenAI API Key 模式請至 platform.openai.com/usage 查看帳單與用量。",
        )


class OpenAIResponsesEngine(BaseEngine):
    supports_image_generation = True

    def __init__(self, client: Any, model: str, provider: str | None = None):
        super().__init__(client=client, model=model, provider=provider)
        self._quota_strategy: OpenAIErrorQuotaStrategy | None = None

    @property
    def quota_strategy(self) -> OpenAIErrorQuotaStrategy:
        """依當前 provider 或 client 特徵動態選擇對應之 Strategy。"""
        if self._quota_strategy is None:
            provider = getattr(self, "provider_name", "") or ""
            base_url = str(getattr(self.client, "base_url", ""))
            api_key = getattr(self.client, "api_key", None)
            is_jwt = isinstance(api_key, str) and api_key.count(".") >= 2

            if provider == "openai-api" or ("api.openai.com" in base_url and not is_jwt):
                self._quota_strategy = OpenAIApiQuotaStrategy(self.client, self.model)
            else:
                self._quota_strategy = CodexOAuthQuotaStrategy(self.client, self.model)
        return self._quota_strategy

    async def stream_chat(
        self,
        input_items: list,
        instructions: str,
        tools: list,
        reasoning_param: Any,
        cancel_check: Optional[Callable[[], bool]] = None,
        build_tools_callback: Optional[Callable[[], list]] = None,
    ) -> AsyncGenerator[Any, None]:

        # 供應商輸入防禦：確保傳入 Responses API 的 function_call 與 output 嚴格成對
        input_items = self.sanitize_input_items(input_items)

        try:
            stream = await self.client.responses.create(
                model=self.model,
                input=input_items,
                instructions=instructions,
                tools=tools,
                store=False,
                stream=True,
                reasoning=reasoning_param,
            )
        except Exception as e:
            if "model" in str(e).lower() or "unknown" in str(e).lower():
                if build_tools_callback:
                    disable_model_param()
                    tools = build_tools_callback()
                stream = await self.client.responses.create(
                    model=self.model,
                    input=input_items,
                    instructions=instructions,
                    tools=tools,
                    store=False,
                    stream=True,
                    reasoning=reasoning_param,
                )
            else:
                raise

        func_calls = {}

        try:
            async for event in stream:
                if cancel_check and cancel_check():
                    break

                etype = getattr(event, "type", "")

                if etype == "response.reasoning_summary_text.delta":
                    yield EventThinking(getattr(event, "delta", ""))

                elif etype == "response.output_text.delta":
                    yield EventText(getattr(event, "delta", ""))

                elif etype == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", "") == "function_call":
                        cid = item.call_id
                        func_calls[cid] = {"name": item.name, "args": getattr(item, "arguments", "") or ""}
                        yield EventToolCallStart(cid, item.name)

                elif etype == "response.function_call_arguments.delta":
                    cid = getattr(event, "call_id", None)
                    delta = getattr(event, "delta", "")
                    if cid and cid in func_calls:
                        func_calls[cid]["args"] += delta
                        yield EventToolCallDelta(cid, delta)

                elif etype == "response.output_item.done":
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", "") == "function_call":
                        cid = item.call_id
                        full_args = getattr(item, "arguments", "") or ""
                        if cid in func_calls:
                            func_calls[cid]["args"] = full_args
                            yield EventToolCallDone(cid, item.name, full_args)

                    elif item and getattr(item, "type", "") == "image_generation_call":
                        b64 = getattr(item, "result", None)
                        if b64:
                            yield EventImageGenerationDone(b64)

                elif etype in ("response.failed", "error"):
                    err = getattr(event, "error", None) or getattr(event, "response", None)
                    err_msg = getattr(err, "message", None) or str(err or etype)
                    raise RuntimeError(f"OpenAI 串流回應失敗 ({etype}): {err_msg}")

                elif etype == "response.incomplete":
                    resp_obj = getattr(event, "response", None)
                    reason = getattr(resp_obj, "incomplete_details", None) or "incomplete"
                    raise RuntimeError(f"OpenAI 串流回應未完成 (incomplete): {reason}")

                elif etype == "response.done":
                    usage = getattr(getattr(event, "response", None), "usage", None)
                    if usage:
                        total_tokens = (
                            usage.get("total_tokens", 0)
                            if isinstance(usage, dict)
                            else getattr(usage, "total_tokens", 0)
                        )
                        yield EventUsage(total_tokens, usage_field(usage, "input_tokens"))
        finally:
            if stream:
                try:
                    await stream.close()
                except Exception:
                    pass

    def parse_api_error(self, exc: Exception) -> str | None:
        """透過對應的 OpenAI / Codex Strategy 解析限流與額度耗盡錯誤（含方案類型與重置時間）。"""
        try:
            details = self.quota_strategy.parse_error_details(exc)
            return self.quota_strategy.format_error_hint(details)
        except Exception:
            return None

    async def format_api_error_async(self, exc: Exception) -> str:
        """非同步格式化 API 錯誤：若例外本身缺少重置時間，允許透過 Strategy 進行非同步額度端點補齊。"""
        try:
            details = self.quota_strategy.parse_error_details(exc)
            details = await self.quota_strategy.enrich_error_details_async(details)
            hint = self.quota_strategy.format_error_hint(details)
            return self._build_api_error_message(exc, hint)
        except Exception:
            return self.format_api_error(exc)

    async def get_quota(self) -> QuotaInfo:
        """委派給當前策略（Codex OAuth 或 OpenAI API）查詢額度與重置時間。"""
        return await self.quota_strategy.fetch_quota()

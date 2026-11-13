import time
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Callable, Optional


@dataclass
class EventThinking:
    delta: str


@dataclass
class EventText:
    delta: str


@dataclass
class EventToolCallStart:
    call_id: str
    name: str


@dataclass
class EventToolCallDelta:
    call_id: str
    delta: str


@dataclass
class EventToolCallDone:
    call_id: str
    name: str
    full_args: str


@dataclass
class EventImageGenerationDone:
    b64: str


@dataclass
class EventUsage:
    total_tokens: int
    input_tokens: int = 0  # 本輪送出的 input tokens；供應商未回報時為 0


def usage_field(usage: Any, *names: str) -> int:
    """從 dict 或物件型態的 usage 取第一個存在且為正整數的欄位，否則回傳 0。"""
    for name in names:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if isinstance(value, int) and value > 0:
            return value
    return 0


@dataclass
class QuotaInfo:
    is_supported: bool
    limit: Optional[float] = None
    remaining: Optional[float] = None
    used: Optional[float] = None
    message: Optional[str] = None

    # 擴充欄位（選填）
    daily_used: Optional[float] = None
    free_requests_limit: Optional[int] = None
    free_requests_remaining: Optional[int] = None
    free_requests_used: Optional[int] = None
    plan_type: Optional[str] = None
    extra_lines: Optional[list[str]] = None

    def get_display_lines(self) -> list[str]:
        """
        將額度資訊格式化為多行字串，供前端一致性呼叫與顯示。
        設計模式：開放給主程式統一呼叫的介面，將格式化邏輯封裝在此。
        """
        if not self.is_supported:
            return [self.message or "此 Provider 不支援透過 API 查詢額度。"]

        lines = []
        if self.plan_type:
            lines.append(f"目前訂閱方案: {self.plan_type}")

        if self.limit is not None and self.remaining is not None and self.used is not None:
            lines.append(f"API 帳戶額度: 已用 ${self.used:.4f} / 上限 ${self.limit:.4f} (剩餘 ${self.remaining:.4f})")

        if self.daily_used is not None:
            lines.append(f"今日花費: ${self.daily_used:.4f}")

        if self.free_requests_limit is not None and self.free_requests_remaining is not None:
            used = (
                self.free_requests_used
                if self.free_requests_used is not None
                else (self.free_requests_limit - self.free_requests_remaining)
            )
            lines.append(
                f"免費模型今日請求次數: 已用 {used} 次 / "
                f"上限 {self.free_requests_limit} 次 (剩餘 {self.free_requests_remaining} 次)"
            )

        if self.extra_lines:
            lines.extend(self.extra_lines)

        return lines


class BaseEngine:
    # 能力旗標：是否支援內建的圖片生成工具（由各 Adapter 覆寫）
    supports_image_generation: bool = False

    # (provider, base_url, model) -> (window_tokens, expire_ts)
    _context_window_cache: dict[tuple[str, str, str], tuple[int, float]] = {}

    def __init__(self, client: Any, model: str, provider: str | None = None):
        self.client = client
        self.model = model
        if provider:
            self.provider_name = provider

    def _cache_key(self) -> tuple[str, str, str]:
        provider = getattr(self, "provider_name", type(self).__name__)
        base_url = str(getattr(self.client, "base_url", ""))
        return (provider, base_url, self.model)

    def context_window(self) -> int | None:
        """同步讀取快取之 context 視窗 tokens；過期或無快取時回傳 None（由預算模組使用保底值）。"""
        key = self._cache_key()
        if key in self._context_window_cache:
            tokens, expire_ts = self._context_window_cache[key]
            if time.time() < expire_ts:
                return tokens
        return None

    async def fetch_context_window(self) -> int | None:
        """非同步查詢供應商端點取得視窗大小並寫入快取。預設不支援。"""
        return None

    def is_context_overflow(self, exc: Exception) -> bool:
        """
        判斷例外是否為「上下文長度超載」。預設以 HTTP 400 搭配常見關鍵字判斷，
        子類別可覆寫以支援各供應商特有的錯誤格式。
        """
        if getattr(exc, "status_code", None) != 400:
            return False
        err_msg = str(exc).lower()
        return any(
            key in err_msg for key in ("context_length_exceeded", "context length", "too large", "maximum context")
        )

    def is_invalid_tool_call_error(self, exc: Exception) -> bool:
        """判斷例外是否為供應商回報的工具呼叫不成對或格式錯誤 (如 HTTP 400)。"""
        if getattr(exc, "status_code", None) != 400:
            return False
        err_msg = str(exc).lower()
        return "no tool output found" in err_msg or "tool_call_id" in err_msg or "function_call" in err_msg

    def sanitize_input_items(self, input_items: list) -> list:
        """供應商輸入檢查與清理。

        1. 保證 function_call 與 function_call_output 成對閉合。
        2. 清理各元素的多餘內部中繼欄位（例如 is_compaction, compacted_range 等），
           確保嚴格相容各大供應商 API 契約，避免 Unknown parameter 400 錯誤。
        """
        from pocket_agent.core.compaction import ensure_paired_tool_calls

        paired = ensure_paired_tool_calls(input_items)
        sanitized = []
        for item in paired:
            if not isinstance(item, dict):
                sanitized.append(item)
                continue
            item_type = item.get("type")
            if item_type == "function_call":
                sanitized.append(
                    {
                        "type": "function_call",
                        "call_id": item.get("call_id", ""),
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    }
                )
            elif item_type == "function_call_output":
                sanitized.append(
                    {
                        "type": "function_call_output",
                        "call_id": item.get("call_id", ""),
                        "output": item.get("output", ""),
                    }
                )
            else:
                msg: dict[str, Any] = {
                    "role": item.get("role", "user"),
                    "content": item.get("content", ""),
                }
                if "type" in item:
                    msg["type"] = item["type"]
                sanitized.append(msg)
        return sanitized

    def connection_error_hint(self, exc: Exception) -> str | None:
        """
        若例外為連線中斷／逾時類錯誤，回傳友善的補充說明；否則回傳 None。
        """
        if "Connection error" not in str(exc):
            return None
        return (
            "\n\n💡 **系統提示**：此錯誤通常發生在網路不穩定、或是模型產生的內容與"
            "工具呼叫（例如產生整份 PDF）過於龐大，導致 API 伺服器中斷連線或逾時。"
        )

    async def get_quota(self) -> QuotaInfo:
        """
        查詢當前 Provider 的額度狀態。預設為不支援。
        """
        return QuotaInfo(is_supported=False, message="此 Provider 不支援透過 API 直接查詢額度。")

    def format_reasoning_param(self, reasoning_effort: str | None) -> Any:
        """將統一的 reasoning_effort ('auto'|'high'|'medium'|'low'|'none') 轉為供應商專屬參數。"""
        return None

    async def stream_chat(
        self,
        input_items: list,
        instructions: str,
        tools: list,
        reasoning_param: Any,
        cancel_check: Optional[Callable[[], bool]] = None,
        build_tools_callback: Optional[Callable[[], list]] = None,
    ) -> AsyncGenerator[Any, None]:
        """
        Subclasses must yield the Event* dataclasses.
        """
        raise NotImplementedError

    @staticmethod
    def parse_duration_seconds(raw: object) -> float | None:
        """
        將秒數或時間長度字串（如 '90', '1h30m10s', '6m0s', '500ms'）解析為秒數。
        若傳入為 > 1_000_000_000 的數值（Unix epoch timestamp），回傳 None 交由 timestamp 邏輯處理。
        """
        import re

        if raw is None or isinstance(raw, bool):
            return None
        if isinstance(raw, (int, float)):
            val = float(raw)
            return val if 0 <= val <= 1_000_000_000 else None

        text = str(raw).strip().lower()
        if not text:
            return None

        try:
            val = float(text)
            return val if 0 <= val <= 1_000_000_000 else None
        except ValueError:
            pass

        # 解析複合時間字串，例如 "1d2h", "1h30m15s", "6m0s", "500ms"
        matches = list(re.finditer(r"(\d+(?:\.\d+)?)\s*(ms|d|h|m|s)", text))
        if not matches:
            return None

        unit_multipliers = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
        total = 0.0
        for m in matches:
            amount = float(m.group(1))
            unit = m.group(2)
            total += amount * unit_multipliers[unit]
        return total

    @staticmethod
    def extract_error_payload(exc: Exception) -> tuple[dict[str, Any] | None, str]:
        """
        從 API 例外中提取結構化錯誤字典與乾淨的錯誤訊息。
        相容：
        1. OpenAI SDK v3 已預先解包之 `exc.body`（直接為內層 error dict）
        2. 傳統未解包之 `{"error": {...}}` 或 `{"detail": {...}}`
        3. `exc.response` 的 `.json()` / `.text` 原始 JSON
        4. 例外字串內嵌之 JSON 或 Python dict repr（優先 json.loads 再 fallback ast.literal_eval）
        """
        import ast
        import json
        import re

        err_msg = str(exc)
        merged: dict[str, Any] = {}

        def _absorb_dict(candidate: object) -> None:
            if not isinstance(candidate, dict):
                return
            for k, v in candidate.items():
                if k not in ("error", "detail") and v is not None and k not in merged:
                    merged[k] = v
            inner_err = candidate.get("error")
            if isinstance(inner_err, dict):
                for k, v in inner_err.items():
                    if v is not None:
                        merged[k] = v
            elif isinstance(inner_err, str) and inner_err.strip() and "message" not in merged:
                merged["message"] = inner_err.strip()

            inner_detail = candidate.get("detail")
            if isinstance(inner_detail, dict):
                _absorb_dict(inner_detail)
            elif isinstance(inner_detail, str) and inner_detail.strip() and "message" not in merged:
                merged["message"] = inner_detail.strip()

        try:
            body = getattr(exc, "body", None)
            if isinstance(body, dict):
                _absorb_dict(body)

            response = getattr(exc, "response", None)
            if response is not None:
                resp_json = None
                if hasattr(response, "json") and callable(response.json):
                    try:
                        resp_json = response.json()
                    except Exception:
                        resp_json = None
                if not isinstance(resp_json, dict):
                    resp_text = getattr(response, "text", None)
                    if isinstance(resp_text, str) and "{" in resp_text:
                        try:
                            resp_json = json.loads(resp_text)
                        except Exception:
                            resp_json = None
                if isinstance(resp_json, dict):
                    _absorb_dict(resp_json)

            if not merged:
                match = re.search(r"\{.*\}", err_msg, re.DOTALL)
                if match:
                    raw_block = match.group()
                    parsed = None
                    try:
                        parsed = json.loads(raw_block)
                    except Exception:
                        try:
                            parsed = ast.literal_eval(raw_block)
                        except Exception:
                            parsed = None
                    if isinstance(parsed, dict):
                        _absorb_dict(parsed)

            if merged:
                msg_candidate = merged.get("message")
                if isinstance(msg_candidate, str) and msg_candidate.strip():
                    err_msg = msg_candidate.strip()
                return merged, err_msg
        except Exception:
            pass

        return None, err_msg

    @staticmethod
    def format_reset_time_hint(
        resets_at: float | int | None = None,
        resets_in_seconds: float | int | None = None,
    ) -> str | None:
        """將重置時間戳記或倒數秒數格式化為 human-readable 的提示字串。供各 Adapter 共用。"""
        import time
        from datetime import datetime, timezone

        now_ts = time.time()
        if resets_at is None and resets_in_seconds is not None:
            resets_at = now_ts + resets_in_seconds
        elif resets_in_seconds is None and resets_at is not None:
            resets_in_seconds = max(0.0, resets_at - now_ts)

        if resets_at is None:
            return None

        reset_dt = datetime.fromtimestamp(resets_at, tz=timezone.utc).astimezone()
        reset_time_str = reset_dt.strftime("%Y-%m-%d %H:%M:%S")

        secs = int(max(0.0, resets_in_seconds if resets_in_seconds is not None else (resets_at - now_ts)))
        hours, remainder = divmod(secs, 3600)
        minutes, seconds = divmod(remainder, 60)

        if hours:
            countdown_str = f"{hours} 小時 {minutes} 分 {seconds} 秒"
        elif minutes:
            countdown_str = f"{minutes} 分 {seconds} 秒"
        else:
            countdown_str = f"{seconds} 秒"

        if resets_at > now_ts:
            return f"\n\n⏳ **重置時間**：{reset_time_str} (距今約 {countdown_str})"
        return f"\n\n⏳ **重置時間**：應已於 {reset_dt.strftime('%Y-%m-%d %H:%M:%S')} 重置"

    def parse_api_error(self, exc: Exception) -> str | None:
        """
        Parse API exception and return a human-readable hint string (e.g. reset time, remedy).
        """
        return None

    def is_non_retryable_error(self, exc: Exception) -> bool:
        """
        判斷例外是否為不可重試的額度／計費類錯誤（預設為 HTTP 402／429）。
        若為 True，Agent 主流程會立即中止並透過 format_api_error 呈現友善訊息。
        子類別可覆寫以支援各供應商特有的錯誤。
        """
        return getattr(exc, "status_code", None) in (402, 429)

    def _build_api_error_message(self, exc: Exception, hint: str | None) -> str:
        status_code = getattr(exc, "status_code", "Unknown")
        _, err_msg = self.extract_error_payload(exc)

        if status_code == 429:
            msg = f"API 請求頻率限制 / 額度耗盡 (HTTP 429 Rate Limit Exceeded)\n\n伺服器回傳：{err_msg}"
        elif status_code == 402:
            msg = f"API 帳戶餘額不足 (HTTP 402 Payment Required)\n\n伺服器回傳：{err_msg}"
        else:
            msg = f"API 錯誤 (HTTP {status_code})\n\n伺服器回傳：{err_msg}"

        if hint:
            msg += hint if hint.startswith("\n") else f"\n\n{hint}"

        return msg

    def format_api_error(self, exc: Exception) -> str:
        """
        Strategy method to format the API error into a human-readable message.
        Implementations include the status code (e.g., 429, 402) and any specific hints.
        """
        hint = self.parse_api_error(exc)
        return self._build_api_error_message(exc, hint)

    async def format_api_error_async(self, exc: Exception) -> str:
        """
        非同步版本的 API 錯誤格式化方法。
        預設委派給同步的 format_api_error(exc)；子類別可覆寫以在例外缺乏重置時間時進行非同步補齊。
        """
        return self.format_api_error(exc)

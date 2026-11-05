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

    def get_display_lines(self) -> list[str]:
        """
        將額度資訊格式化為多行字串，供前端一致性呼叫與顯示。
        設計模式：開放給主程式統一呼叫的介面，將格式化邏輯封裝在此。
        """
        if not self.is_supported:
            return [self.message or "此 Provider 不支援透過 API 查詢額度。"]

        lines = []
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

        return lines


class BaseEngine:
    # 能力旗標：是否支援內建的圖片生成工具（由各 Adapter 覆寫）
    supports_image_generation: bool = False

    # (provider, base_url, model) -> (window_tokens, expire_ts)
    _context_window_cache: dict[tuple[str, str, str], tuple[int, float]] = {}

    def __init__(self, client: Any, model: str):
        self.client = client
        self.model = model

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

    def format_api_error(self, exc: Exception) -> str:
        """
        Strategy method to format the API error into a human-readable message.
        Implementations include the status code (e.g., 429, 402) and any specific hints.
        """
        import ast
        import re

        status_code = getattr(exc, "status_code", "Unknown")
        err_msg = str(exc)

        try:
            error_dict = None
            body = getattr(exc, "body", None)
            if isinstance(body, dict):
                error_dict = body.get("error")

            if not isinstance(error_dict, dict):
                match = re.search(r"\{.*\}", err_msg, re.DOTALL)
                if match:
                    parsed = ast.literal_eval(match.group())
                    if isinstance(parsed, dict):
                        error_dict = parsed.get("error")

            if isinstance(error_dict, dict):
                err_msg = error_dict.get("message", err_msg)
        except Exception:
            pass

        hint = self.parse_api_error(exc)

        if status_code == 429:
            msg = f"API 請求頻率限制 / 額度耗盡 (HTTP 429 Rate Limit Exceeded)\n\n伺服器回傳：{err_msg}"
        elif status_code == 402:
            msg = f"API 帳戶餘額不足 (HTTP 402 Payment Required)\n\n伺服器回傳：{err_msg}"
        else:
            msg = f"API 錯誤 (HTTP {status_code})\n\n伺服器回傳：{err_msg}"

        if hint:
            msg += hint

        return msg

    @staticmethod
    def format_chat_tools(tools: list) -> Any:
        """
        Helper method for Adapters that need to convert the flattened tool schema
        to the standard nested OpenAI chat.completions schema.
        """
        from openai import NOT_GIVEN

        if not tools:
            return NOT_GIVEN

        chat_tools = []
        for t in tools:
            if t.get("type") == "function" and "name" in t:
                chat_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": t.get("name"),
                            "description": t.get("description", ""),
                            "parameters": t.get("parameters", {}),
                        },
                    }
                )
            else:
                chat_tools.append(t)
        return chat_tools

    @staticmethod
    def format_chat_messages(input_items: list) -> list:
        """
        Helper method to convert Pocket Agent's internal history format (based on Responses API)
        to standard OpenAI chat.completions messages format.
        """
        messages = []
        for item in input_items:
            msg = dict(item)

            # Format text/image content lists
            if isinstance(msg.get("content"), list):
                new_content = []
                for part in msg["content"]:
                    if part.get("type") == "input_text":
                        new_content.append({"type": "text", "text": part.get("text", "")})
                    elif part.get("type") == "input_image":
                        new_content.append({"type": "image_url", "image_url": {"url": part.get("image_url", "")}})
                    else:
                        new_content.append(part)
                msg["content"] = new_content

            # Convert Responses API tool format to Chat Completions format
            item_type = msg.get("type")
            if item_type == "function_call":
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": msg.get("call_id"),
                                "type": "function",
                                "function": {"name": msg.get("name"), "arguments": msg.get("arguments", "{}")},
                            }
                        ],
                    }
                )
            elif item_type == "function_call_output":
                messages.append({"role": "tool", "tool_call_id": msg.get("call_id"), "content": msg.get("output", "")})
            else:
                messages.append(
                    {
                        "role": msg.get("role", "user"),
                        "content": msg.get("content", ""),
                    }
                )

        return messages

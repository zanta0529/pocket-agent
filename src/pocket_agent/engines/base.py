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
    def __init__(self, client: Any, model: str):
        self.client = client
        self.model = model

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

    def parse_api_error(self, exc: Exception) -> str | None:
        """
        Parse API exception and return a human-readable hint string (e.g. reset time, remedy).
        """
        return None

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
                if "type" in msg:
                    del msg["type"]  # Remove Responses API specific type field if it exists
                if "role" not in msg:
                    msg["role"] = "user"  # Fallback if missing
                messages.append(msg)

        return messages

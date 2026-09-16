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


class BaseEngine:
    def __init__(self, client: Any, model: str):
        self.client = client
        self.model = model

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

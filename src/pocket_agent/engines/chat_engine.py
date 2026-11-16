import logging
from typing import Any, AsyncGenerator, Callable, Optional

from pocket_agent.engines.base import (
    BaseEngine,
    EventText,
    EventThinking,
    EventToolCallDelta,
    EventToolCallDone,
    EventToolCallStart,
    EventUsage,
    usage_field,
)

log = logging.getLogger("pocket-agent")


class ThinkTagStreamParser:
    """串流 `<think>` / `</think>` 標籤狀態機（State Pattern），支援跨 chunk 部分匹配。"""

    def __init__(self) -> None:
        self.in_think_block = False
        self.buffer = ""

    def feed(self, delta: str) -> list[EventText | EventThinking]:
        events: list[EventText | EventThinking] = []
        self.buffer += delta
        while self.buffer:
            tag = "</think>" if self.in_think_block else "<think>"
            event_cls = EventThinking if self.in_think_block else EventText
            pos = self.buffer.find(tag)
            if pos != -1:
                if pos > 0:
                    events.append(event_cls(self.buffer[:pos]))
                self.in_think_block = not self.in_think_block
                self.buffer = self.buffer[pos + len(tag) :]
                continue

            partial_len = 0
            for i in range(len(tag) - 1, 0, -1):
                if self.buffer.endswith(tag[:i]):
                    partial_len = i
                    break
            if partial_len:
                if len(self.buffer) > partial_len:
                    events.append(event_cls(self.buffer[:-partial_len]))
                self.buffer = self.buffer[-partial_len:]
            else:
                events.append(event_cls(self.buffer))
                self.buffer = ""
            break
        return events

    def flush(self) -> list[EventText | EventThinking]:
        if not self.buffer:
            return []
        event_cls = EventThinking if self.in_think_block else EventText
        evt = event_cls(self.buffer)
        self.buffer = ""
        return [evt]


class StandardChatCompletionsEngine(BaseEngine):
    """
    Standard Engine that uses the standard OpenAI /v1/chat/completions API.
    Compatible with Gemini, Groq, Mistral, SambaNova, etc.
    It implements `<think>` tag parsing to support reasoning models via standard output.
    """

    supports_stream_usage: bool = False

    @staticmethod
    def format_chat_tools(tools: list) -> Any:
        """將扁平化的工具 Schema 轉換為 OpenAI chat.completions 巢狀格式。"""
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
        """將內部 Responses API 歷史格式轉換為標準 chat.completions messages 格式。"""
        messages = []
        for item in input_items:
            msg = dict(item)

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

    def _build_extra_body(self, reasoning_param: Any) -> dict[str, Any] | None:
        """供子類別覆寫以注入供應商專屬之 extra_body 參數。"""
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

        messages = self.format_chat_messages(input_items)
        if instructions:
            messages.insert(0, {"role": "system", "content": instructions})

        chat_tools = self.format_chat_tools(tools)

        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": chat_tools,
            "stream": True,
        }
        if self.supports_stream_usage:
            create_kwargs["stream_options"] = {"include_usage": True}
        extra_body = self._build_extra_body(reasoning_param)
        if extra_body:
            create_kwargs["extra_body"] = extra_body

        try:
            stream = await self.client.chat.completions.create(**create_kwargs)
        except Exception as e:
            log.info(f"Standard chat completion failed: {e}")
            raise

        tool_calls_buffer = {}
        think_parser = ThinkTagStreamParser()

        try:
            async for chunk in stream:
                if cancel_check and cancel_check():
                    break

                delta = chunk.choices[0].delta if chunk.choices else None
                finish_reason = chunk.choices[0].finish_reason if chunk.choices else None
                usage = getattr(chunk, "usage", None)

                if not delta and not finish_reason and not usage:
                    continue

                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_buffer:
                            cid = tc.id or f"call_{idx}"
                            name = tc.function.name or ""
                            tool_calls_buffer[idx] = {"id": cid, "name": name, "args": ""}
                            yield EventToolCallStart(cid, name)

                        if tc.function and tc.function.arguments:
                            tool_calls_buffer[idx]["args"] += tc.function.arguments
                            yield EventToolCallDelta(tool_calls_buffer[idx]["id"], tc.function.arguments)

                if finish_reason:
                    if finish_reason == "tool_calls":
                        for idx, tc_data in tool_calls_buffer.items():
                            yield EventToolCallDone(tc_data["id"], tc_data["name"], tc_data["args"])
                    elif tool_calls_buffer:
                        log.warning(
                            "Chat completion tool call stream terminated with finish_reason=%r; "
                            "discarding %d incomplete tool call(s).",
                            finish_reason,
                            len(tool_calls_buffer),
                        )
                    tool_calls_buffer.clear()

                if delta and getattr(delta, "reasoning_content", None):
                    yield EventThinking(delta.reasoning_content)

                if delta and delta.content:
                    for evt in think_parser.feed(delta.content):
                        yield evt

                if usage:
                    total_tokens = (
                        usage.get("total_tokens", 0) if isinstance(usage, dict) else getattr(usage, "total_tokens", 0)
                    )
                    if total_tokens > 0:
                        yield EventUsage(total_tokens, usage_field(usage, "prompt_tokens"))

            for evt in think_parser.flush():
                yield evt

        finally:
            if stream:
                try:
                    await stream.close()
                except Exception:
                    pass

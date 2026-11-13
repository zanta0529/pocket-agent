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

        in_think_block = False
        tool_calls_buffer = {}
        content_buffer = ""

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
                    content_buffer += delta.content
                    while content_buffer:
                        if not in_think_block:
                            start_pos = content_buffer.find("<think>")
                            if start_pos != -1:
                                if start_pos > 0:
                                    yield EventText(content_buffer[:start_pos])
                                in_think_block = True
                                content_buffer = content_buffer[start_pos + 7 :]
                                continue
                            else:
                                partial_match = False
                                for i in range(len("<think>") - 1, 0, -1):
                                    if content_buffer.endswith("<think>"[:i]):
                                        if len(content_buffer) > i:
                                            yield EventText(content_buffer[:-i])
                                        content_buffer = content_buffer[-i:]
                                        partial_match = True
                                        break
                                if not partial_match:
                                    yield EventText(content_buffer)
                                    content_buffer = ""
                                break
                        else:
                            end_pos = content_buffer.find("</think>")
                            if end_pos != -1:
                                if end_pos > 0:
                                    yield EventThinking(content_buffer[:end_pos])
                                in_think_block = False
                                content_buffer = content_buffer[end_pos + 8 :]
                                continue
                            else:
                                partial_match = False
                                for i in range(len("</think>") - 1, 0, -1):
                                    if content_buffer.endswith("</think>"[:i]):
                                        if len(content_buffer) > i:
                                            yield EventThinking(content_buffer[:-i])
                                        content_buffer = content_buffer[-i:]
                                        partial_match = True
                                        break
                                if not partial_match:
                                    yield EventThinking(content_buffer)
                                    content_buffer = ""
                                break

                if usage:
                    total_tokens = (
                        usage.get("total_tokens", 0) if isinstance(usage, dict) else getattr(usage, "total_tokens", 0)
                    )
                    if total_tokens > 0:
                        yield EventUsage(total_tokens, usage_field(usage, "prompt_tokens"))

            if content_buffer:
                if not in_think_block:
                    yield EventText(content_buffer)
                else:
                    yield EventThinking(content_buffer)

        finally:
            if stream:
                try:
                    await stream.close()
                except Exception:
                    pass

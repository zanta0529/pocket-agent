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
)

log = logging.getLogger("pocket-agent")


class StandardChatCompletionsEngine(BaseEngine):
    """
    Standard Engine that uses the standard OpenAI /v1/chat/completions API.
    Compatible with Gemini, Groq, Mistral, SambaNova, etc.
    It implements `<think>` tag parsing to support reasoning models via standard output.
    """

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

        try:
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=chat_tools,
                stream=True,
            )
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
                    for idx, tc_data in tool_calls_buffer.items():
                        yield EventToolCallDone(tc_data["id"], tc_data["name"], tc_data["args"])
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
                        yield EventUsage(total_tokens)

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

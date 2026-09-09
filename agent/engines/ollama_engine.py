import logging
from typing import Any, AsyncGenerator, Callable, Optional

from openai import NOT_GIVEN

from .base import (
    BaseEngine,
    EventText,
    EventThinking,
    EventToolCallDelta,
    EventToolCallDone,
    EventToolCallStart,
    EventUsage,
)

log = logging.getLogger("pocket-agent")


class OllamaChatEngine(BaseEngine):
    async def stream_chat(
        self,
        input_items: list,
        instructions: str,
        tools: list,
        reasoning_param: Any,
        cancel_check: Optional[Callable[[], bool]] = None,
        build_tools_callback: Optional[Callable[[], list]] = None,
    ) -> AsyncGenerator[Any, None]:

        messages = []
        if instructions:
            messages.append({"role": "system", "content": instructions})

        # input_items usually contains the history (which are standard messages)
        # However, for tool results, if we have tool call history, it needs to be formatted for chat.completions.
        # Pocket Agent's input_items is a direct copy of self.history.
        # Wait, how does Responses API handle tool results? It passes them in `input`.
        # If the history contains tool messages, they must be formatted correctly.
        for item in input_items:
            # We copy to avoid mutating the original history
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
            messages.append(msg)

        # Optional: formatting tools for chat completions
        # Tools in Pocket Agent are standard function JSON schema.
        chat_tools = tools if tools else NOT_GIVEN

        try:
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=chat_tools,
                stream=True,
            )
        except Exception:
            log.exception("Ollama chat completion failed")
            raise

        in_think_block = False

        # Buffer for tool calls to yield EventToolCallDone
        tool_calls_buffer = {}

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
                        # OpenAI API chunks tool_calls by index
                        idx = tc.index
                        if idx not in tool_calls_buffer:
                            cid = tc.id or f"call_{idx}"
                            name = tc.function.name or ""
                            tool_calls_buffer[idx] = {"id": cid, "name": name, "args": ""}
                            yield EventToolCallStart(cid, name)

                        if tc.function and tc.function.arguments:
                            tool_calls_buffer[idx]["args"] += tc.function.arguments
                            yield EventToolCallDelta(tool_calls_buffer[idx]["id"], tc.function.arguments)

                # Wait, if finish_reason == "tool_calls" or stream ends, we should emit Done
                # Sometimes finish_reason is empty until the last chunk
                if finish_reason:
                    for idx, tc_data in tool_calls_buffer.items():
                        yield EventToolCallDone(tc_data["id"], tc_data["name"], tc_data["args"])
                    tool_calls_buffer.clear()

                if delta and delta.content:
                    text = delta.content

                    # State machine for <think> tags
                    idx = 0
                    while idx < len(text):
                        if not in_think_block:
                            # Look for <think>
                            start_pos = text.find("<think>", idx)
                            if start_pos != -1:
                                # Yield text before <think>
                                if start_pos > idx:
                                    yield EventText(text[idx:start_pos])
                                in_think_block = True
                                idx = start_pos + 7
                            else:
                                # Look for partial `<think>` at the end of the chunk
                                # This is a simplified buffer approach.
                                # A perfect approach would buffer partial `<` matches,
                                # but for simplicity we assume `<think>` usually arrives in one chunk
                                # or we just output the `<` if it's not a think block.
                                # To be safe, we just yield the rest.
                                yield EventText(text[idx:])
                                break
                        else:
                            # Look for </think>
                            end_pos = text.find("</think>", idx)
                            if end_pos != -1:
                                if end_pos > idx:
                                    yield EventThinking(text[idx:end_pos])
                                in_think_block = False
                                idx = end_pos + 8
                            else:
                                yield EventThinking(text[idx:])
                                break

                if usage:
                    total_tokens = (
                        usage.get("total_tokens", 0) if isinstance(usage, dict) else getattr(usage, "total_tokens", 0)
                    )
                    if total_tokens > 0:
                        yield EventUsage(total_tokens)

        finally:
            if stream:
                try:
                    await stream.close()
                except Exception:
                    pass

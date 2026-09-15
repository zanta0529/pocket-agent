import logging
from typing import Any, AsyncGenerator, Callable, Optional

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


class OpenRouterEngine(BaseEngine):
    """OpenRouter uses the standard chat.completions API.
    We implement `<think>` tag parsing to support reasoning models (e.g. DeepSeek-R1)."""

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
            # Note: For openrouter, reasoning_param is currently not natively passed via API
            # payload in the standard openai client (unless via extra_body),
            # so we just rely on the model generating <think> tags natively, which works for DeepSeek models.
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=chat_tools,
                stream=True,
            )
        except Exception as e:
            log.info(f"OpenRouter chat completion failed: {e}")
            raise

        in_think_block = False
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
                    # Some newer OpenAI compatible wrappers might return reasoning_content natively
                    yield EventThinking(delta.reasoning_content)

                if delta and delta.content:
                    text = delta.content
                    idx = 0
                    while idx < len(text):
                        if not in_think_block:
                            start_pos = text.find("<think>", idx)
                            if start_pos != -1:
                                if start_pos > idx:
                                    yield EventText(text[idx:start_pos])
                                in_think_block = True
                                idx = start_pos + 7
                            else:
                                yield EventText(text[idx:])
                                break
                        else:
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

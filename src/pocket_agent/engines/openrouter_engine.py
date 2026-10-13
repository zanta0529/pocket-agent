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
    QuotaInfo,
    usage_field,
)

log = logging.getLogger("pocket-agent")


class OpenRouterEngine(BaseEngine):
    """OpenRouter uses the standard chat.completions API.
    We implement `<think>` tag parsing to support reasoning models (e.g. DeepSeek-R1)."""

    async def get_quota(self) -> QuotaInfo:
        import httpx

        try:
            api_key = self.client.api_key
        except AttributeError:
            return QuotaInfo(is_supported=False, message="無法取得 API Key")

        async with httpx.AsyncClient() as http_client:
            try:
                response = await http_client.get(
                    "https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {api_key}"}, timeout=10.0
                )
                response.raise_for_status()
                data = response.json().get("data", {})

                limit = data.get("limit")
                remaining = data.get("limit_remaining")

                used = None
                if limit is not None and remaining is not None:
                    used = limit - remaining

                daily_used = data.get("usage_daily")

                free_limit = None
                free_remaining = None
                free_used = None

                free_reqs = data.get("free_model_daily_requests")
                if isinstance(free_reqs, dict):
                    free_limit = free_reqs.get("limit")
                    free_remaining = free_reqs.get("remaining")
                    free_used = free_reqs.get("used")

                return QuotaInfo(
                    is_supported=True,
                    limit=limit,
                    remaining=remaining,
                    used=used,
                    daily_used=daily_used,
                    free_requests_limit=free_limit,
                    free_requests_remaining=free_remaining,
                    free_requests_used=free_used,
                )
            except Exception as e:
                log.error(f"Failed to fetch OpenRouter quota: {e}")
                return QuotaInfo(is_supported=False, message=f"查詢 OpenRouter 額度失敗：{str(e)}")

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
                    # Some newer OpenAI compatible wrappers might return reasoning_content natively
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

            # Flush any remaining buffer when stream ends
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

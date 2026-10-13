import logging
from typing import Any, AsyncGenerator, Callable, Optional

from pocket_agent.engines.base import (
    BaseEngine,
    EventImageGenerationDone,
    EventText,
    EventThinking,
    EventToolCallDelta,
    EventToolCallDone,
    EventToolCallStart,
    EventUsage,
    usage_field,
)
from pocket_agent.tools.image import disable_model_param

log = logging.getLogger("pocket-agent")


class OpenAIResponsesEngine(BaseEngine):
    supports_image_generation = True

    async def stream_chat(
        self,
        input_items: list,
        instructions: str,
        tools: list,
        reasoning_param: Any,
        cancel_check: Optional[Callable[[], bool]] = None,
        build_tools_callback: Optional[Callable[[], list]] = None,
    ) -> AsyncGenerator[Any, None]:

        try:
            stream = await self.client.responses.create(
                model=self.model,
                input=input_items,
                instructions=instructions,
                tools=tools,
                store=False,
                stream=True,
                reasoning=reasoning_param,
            )
        except Exception as e:
            if "model" in str(e).lower() or "unknown" in str(e).lower():
                if build_tools_callback:
                    disable_model_param()
                    tools = build_tools_callback()
                stream = await self.client.responses.create(
                    model=self.model,
                    input=input_items,
                    instructions=instructions,
                    tools=tools,
                    store=False,
                    stream=True,
                    reasoning=reasoning_param,
                )
            else:
                raise

        func_calls = {}

        try:
            async for event in stream:
                if cancel_check and cancel_check():
                    break

                etype = getattr(event, "type", "")

                if etype == "response.reasoning_summary_text.delta":
                    yield EventThinking(getattr(event, "delta", ""))

                elif etype == "response.output_text.delta":
                    yield EventText(getattr(event, "delta", ""))

                elif etype == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", "") == "function_call":
                        cid = item.call_id
                        func_calls[cid] = {"name": item.name, "args": getattr(item, "arguments", "") or ""}
                        yield EventToolCallStart(cid, item.name)

                elif etype == "response.function_call_arguments.delta":
                    cid = getattr(event, "call_id", None)
                    delta = getattr(event, "delta", "")
                    if cid and cid in func_calls:
                        func_calls[cid]["args"] += delta
                        yield EventToolCallDelta(cid, delta)

                elif etype == "response.output_item.done":
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", "") == "function_call":
                        cid = item.call_id
                        full_args = getattr(item, "arguments", "") or ""
                        if cid in func_calls:
                            func_calls[cid]["args"] = full_args
                            yield EventToolCallDone(cid, item.name, full_args)

                    elif item and getattr(item, "type", "") == "image_generation_call":
                        b64 = getattr(item, "result", None)
                        if b64:
                            yield EventImageGenerationDone(b64)

                elif etype == "response.done":
                    usage = getattr(getattr(event, "response", None), "usage", None)
                    if usage:
                        total_tokens = (
                            usage.get("total_tokens", 0)
                            if isinstance(usage, dict)
                            else getattr(usage, "total_tokens", 0)
                        )
                        yield EventUsage(total_tokens, usage_field(usage, "input_tokens"))
        finally:
            if stream:
                try:
                    await stream.close()
                except Exception:
                    pass

    def parse_api_error(self, exc: Exception) -> str | None:
        """Parse OpenAI APIStatusError (or usage_limit_reached) for reset time."""
        import ast
        import re
        from datetime import datetime, timezone

        try:
            error = None

            # Path 1: Check standard body attribute
            body = getattr(exc, "body", None)
            if isinstance(body, dict):
                error = body.get("error")

            # Path 2: Fallback to parsing string if body is missing
            if not isinstance(error, dict):
                text = str(exc)
                match = re.search(r"\{.*\}", text, re.DOTALL)
                if match:
                    try:
                        parsed = ast.literal_eval(match.group())
                        if isinstance(parsed, dict):
                            error = parsed.get("error")
                    except ValueError, SyntaxError:
                        pass

            if not isinstance(error, dict):
                return None

            if error.get("type") != "usage_limit_reached":
                return None

            plan_type: str = error.get("plan_type") or "未知"
            resets_at: object = error.get("resets_at")
            resets_in_seconds: object = error.get("resets_in_seconds")

            lines: list[str] = [f"\n\n💳 **API 用量已達上限** (方案：{plan_type})"]

            reset_time_str = ""
            countdown_str = ""

            if isinstance(resets_at, (int, float)) and resets_at > 0:
                reset_dt = datetime.fromtimestamp(resets_at, tz=timezone.utc).astimezone()
                reset_time_str = reset_dt.strftime("%Y-%m-%d %H:%M:%S")

            if isinstance(resets_in_seconds, (int, float)) and resets_in_seconds > 0:
                secs = int(resets_in_seconds)
                hours, remainder = divmod(secs, 3600)
                minutes, seconds = divmod(remainder, 60)
                if hours:
                    countdown_str = f"{hours} 小時 {minutes} 分 {seconds} 秒"
                elif minutes:
                    countdown_str = f"{minutes} 分 {seconds} 秒"
                else:
                    countdown_str = f"{seconds} 秒"

            if reset_time_str and countdown_str:
                lines.append(f"⏳ **重置時間**：{reset_time_str} (距今約 {countdown_str})")
            elif reset_time_str:
                lines.append(f"⏳ **重置時間**：{reset_time_str}")
            elif countdown_str:
                lines.append(f"⏳ **距今約**：{countdown_str}")

            if len(lines) > 1:
                return "\n\n".join(lines)
            return None
        except Exception:
            return None

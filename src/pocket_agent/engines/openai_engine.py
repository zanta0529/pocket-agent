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

        # 供應商輸入防禦：確保傳入 Responses API 的 function_call 與 output 嚴格成對
        input_items = self.sanitize_input_items(input_items)

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

                elif etype in ("response.failed", "error"):
                    err = getattr(event, "error", None) or getattr(event, "response", None)
                    err_msg = getattr(err, "message", None) or str(err or etype)
                    raise RuntimeError(f"OpenAI 串流回應失敗 ({etype}): {err_msg}")

                elif etype == "response.incomplete":
                    resp_obj = getattr(event, "response", None)
                    reason = getattr(resp_obj, "incomplete_details", None) or "incomplete"
                    raise RuntimeError(f"OpenAI 串流回應未完成 (incomplete): {reason}")

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
        """Parse OpenAI APIStatusError (including OpenAI Codex usage limit and headers) for reset time."""
        import ast
        import re

        try:
            err_text = str(exc)
            resets_at: float | int | None = None
            resets_in_seconds: float | int | None = None
            plan_type: str | None = None

            # 1. 優先檢查 HTTP Response Headers (OpenAI SDK 的 APIStatusError.response.headers)
            response = getattr(exc, "response", None)
            headers = getattr(response, "headers", None) or {}
            header_map = {str(k).lower(): str(v) for k, v in headers.items()} if hasattr(headers, "items") else {}

            retry_after = header_map.get("retry-after")
            reset_ts_raw = (
                header_map.get("x-ratelimit-reset")
                or header_map.get("x-codex-resets-at")
                or header_map.get("x-ratelimit-reset-requests")
                or header_map.get("x-ratelimit-reset-tokens")
            )

            if retry_after:
                try:
                    resets_in_seconds = float(retry_after)
                except ValueError:
                    pass

            if reset_ts_raw:
                try:
                    val = float(reset_ts_raw)
                    if val > 1_000_000_000:
                        resets_at = val
                    else:
                        resets_in_seconds = val
                except ValueError:
                    pass

            # 2. 檢查 JSON Body 或錯誤字串中的 payload
            error = None
            body = getattr(exc, "body", None)
            if isinstance(body, dict):
                error = body.get("error")

            if not isinstance(error, dict):
                match = re.search(r"\{.*\}", err_text, re.DOTALL)
                if match:
                    try:
                        parsed = ast.literal_eval(match.group())
                        if isinstance(parsed, dict):
                            error = parsed.get("error")
                    except ValueError, SyntaxError:
                        pass

            is_usage_limit = False
            if isinstance(error, dict):
                err_type = str(error.get("type", "")).lower()
                err_code = str(error.get("code", "")).lower()
                msg = str(error.get("message", "")).lower()

                if "usage_limit" in err_type or "usage_limit" in err_code or "usage limit" in msg:
                    is_usage_limit = True

                plan_type = error.get("plan_type")
                if resets_at is None and isinstance(error.get("resets_at"), (int, float)):
                    resets_at = float(error["resets_at"])
                if resets_in_seconds is None and isinstance(error.get("resets_in_seconds"), (int, float)):
                    resets_in_seconds = float(error["resets_in_seconds"])
            else:
                if "usage limit has been reached" in err_text.lower():
                    is_usage_limit = True

            # 3. 呼叫 BaseEngine 的共用時間格式化工具
            hint = self.format_reset_time_hint(resets_at=resets_at, resets_in_seconds=resets_in_seconds)

            lines: list[str] = []
            if is_usage_limit:
                plan_suffix = f" (方案：{plan_type})" if plan_type else ""
                lines.append(f"\n\n💳 **API 用量已達上限**{plan_suffix}")

            if hint:
                lines.append(hint.strip())

            if lines:
                return "\n\n".join(lines)
            return None
        except Exception:
            return None

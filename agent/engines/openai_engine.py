import logging
from typing import Any, AsyncGenerator, Callable, Optional

from agent.imagegen import disable_model_param

from .base import (
    BaseEngine,
    EventImageGenerationDone,
    EventText,
    EventThinking,
    EventToolCallDelta,
    EventToolCallDone,
    EventToolCallStart,
    EventUsage,
)

log = logging.getLogger("pocket-agent")


class OpenAIResponsesEngine(BaseEngine):
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
                        yield EventUsage(total_tokens)
        finally:
            if stream:
                try:
                    await stream.close()
                except Exception:
                    pass

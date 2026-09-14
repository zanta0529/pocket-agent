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

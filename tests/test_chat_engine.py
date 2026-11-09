import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pocket_agent.engines.base import EventToolCallDelta, EventToolCallDone, EventToolCallStart
from pocket_agent.engines.chat_engine import StandardChatCompletionsEngine


def _make_chunk(tool_calls=None, finish_reason=None, content=None):
    delta = SimpleNamespace(
        tool_calls=tool_calls,
        content=content,
        reasoning_content=None,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=None)


def _make_tc_delta(index: int, call_id: str | None = None, name: str | None = None, arguments: str | None = None):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class _AsyncChunkStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("bad_finish_reason", ["length", "content_filter", "stop", "unknown_reason", None])
def test_chat_engine_does_not_emit_tool_call_done_on_non_tool_calls_finish_reason(bad_finish_reason):
    """驗證非 tool_calls 結束原因時，即使 JSON 合法也不發出 EventToolCallDone。"""

    async def _run():
        chunks = [
            _make_chunk(
                tool_calls=[_make_tc_delta(0, call_id="call_1", name="read_file", arguments='{"path": "secret.txt"}')]
            ),
        ]
        if bad_finish_reason is not None:
            chunks.append(_make_chunk(finish_reason=bad_finish_reason))

        fake_stream = _AsyncChunkStream(chunks)
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_stream)

        engine = StandardChatCompletionsEngine(client=mock_client, model="gemini-pro")
        events = []
        async for ev in engine.stream_chat(input_items=[], instructions="", tools=[], reasoning_param=None):
            events.append(ev)

        assert any(isinstance(e, EventToolCallStart) for e in events)
        assert any(isinstance(e, EventToolCallDelta) for e in events)
        assert not any(isinstance(e, EventToolCallDone) for e in events)
        assert fake_stream.closed is True

    asyncio.run(_run())


def test_chat_engine_emits_tool_call_done_for_multiple_tools_only_on_tool_calls_finish():
    """驗證 finish_reason == 'tool_calls' 時，能正常對多個工具呼叫發出 EventToolCallDone。"""

    async def _run():
        chunks = [
            _make_chunk(
                tool_calls=[
                    _make_tc_delta(0, call_id="call_a", name="read_file", arguments='{"path": '),
                    _make_tc_delta(1, call_id="call_b", name="list_dir", arguments='{"path": "."}'),
                ]
            ),
            _make_chunk(
                tool_calls=[
                    _make_tc_delta(0, arguments='"a.txt"}'),
                ]
            ),
            _make_chunk(finish_reason="tool_calls"),
        ]
        fake_stream = _AsyncChunkStream(chunks)
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_stream)

        engine = StandardChatCompletionsEngine(client=mock_client, model="gemini-pro")
        done_events = [
            ev
            async for ev in engine.stream_chat(input_items=[], instructions="", tools=[], reasoning_param=None)
            if isinstance(ev, EventToolCallDone)
        ]

        assert len(done_events) == 2
        assert done_events[0].call_id == "call_a"
        assert done_events[0].full_args == '{"path": "a.txt"}'
        assert done_events[1].call_id == "call_b"
        assert done_events[1].full_args == '{"path": "."}'
        assert fake_stream.closed is True

    asyncio.run(_run())

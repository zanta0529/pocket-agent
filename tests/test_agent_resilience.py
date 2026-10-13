"""Agent 韌性與 Adapter 成對防護單元測試。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from pocket_agent.core.agent import Agent
from pocket_agent.engines.base import BaseEngine
from pocket_agent.engines.openai_engine import OpenAIResponsesEngine


def test_base_engine_sanitize_and_error_detection():
    engine = BaseEngine(client=None, model="test-model")

    items = [
        {"role": "user", "content": "測試任務"},
        {"type": "function_call", "call_id": "call_123", "name": "read_file", "arguments": "{}"},
        # 故意省略 output
        {"role": "assistant", "content": "回覆"},
    ]

    sanitized = engine.sanitize_input_items(items)
    # 孤立的 call_123 應被過濾剔除
    assert len(sanitized) == 2
    assert sanitized[0]["role"] == "user"
    assert sanitized[1]["role"] == "assistant"

    class Fake400Error(Exception):
        status_code = 400

        def __str__(self):
            return "No tool output found for function call call_123"

    assert engine.is_invalid_tool_call_error(Fake400Error()) is True

    class FakeOtherError(Exception):
        status_code = 400

        def __str__(self):
            return "bad_request"

    assert engine.is_invalid_tool_call_error(FakeOtherError()) is False


def test_openai_responses_engine_sanitizes_before_stream():
    async def _test():
        mock_client = MagicMock()
        mock_stream = AsyncMock()

        async def _fake_aiter():
            if False:
                yield None

        mock_stream.__aiter__.side_effect = _fake_aiter
        mock_client.responses.create = AsyncMock(return_value=mock_stream)

        engine = OpenAIResponsesEngine(client=mock_client, model="gpt-4o")

        raw_items = [
            {"role": "user", "content": "任務"},
            {"type": "function_call", "call_id": "c_orphan", "name": "read_file", "arguments": "{}"},
        ]

        async for _ in engine.stream_chat(raw_items, instructions="test", tools=[], reasoning_param=None):
            pass

        # 驗證 create 被呼叫時傳入的 input 已被過濾，無孤立 call
        called_args = mock_client.responses.create.call_args[1]
        sent_input = called_args["input"]
        assert len(sent_input) == 1
        assert sent_input[0]["role"] == "user"

    asyncio.run(_test())


def test_subagent_graceful_degradation_at_max_rounds():
    async def _test():
        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []

        subagent = Agent(
            client=mock_client,
            mcp=mock_mcp,
            model="test-model",
            is_subagent=True,
            max_tool_rounds=2,
        )

        rounds_called = 0
        received_tools_history = []

        class MockSubEngine(BaseEngine):
            async def stream_chat(
                self,
                input_items,
                instructions,
                tools,
                reasoning_param=None,
                cancel_check=None,
                build_tools_callback=None,
            ):
                nonlocal rounds_called
                rounds_called += 1
                received_tools_history.append(list(tools))

                from pocket_agent.engines.base import EventText, EventToolCallDone, EventToolCallStart

                if rounds_called == 1:
                    # 第一輪呼叫工具
                    yield EventToolCallStart("call_1", "read_file")
                    yield EventToolCallDone("call_1", "read_file", '{"path": "test.txt"}')
                else:
                    # 第二輪（最後一輪）：此時 tools 應已被清空，輸出總結文字
                    yield EventText("【階段性研究報告】已確認專案架構與關鍵檔案。")

        subagent._engine_cache = (("mock", id(mock_client), "test-model"), MockSubEngine(mock_client, "test-model"))

        # 模擬檔案工具回傳
        from pocket_agent.tools import fs

        orig_call = fs.call_fs_tool
        fs.call_fs_tool = MagicMock(return_value="檔案內容")
        try:
            _thinking, reply, _usage = await subagent.chat("請探勘專案")
            assert "階段性研究報告" in reply
            # 驗證最後一輪 tools 被強制清空
            assert len(received_tools_history[0]) > 0  # 第 1 輪有 tools
            assert len(received_tools_history[1]) == 0  # 第 2 輪 (最後一輪) tools 被清空
        finally:
            fs.call_fs_tool = orig_call

    asyncio.run(_test())

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

    items.append(
        {
            "role": "user",
            "content": "摘要內容",
            "is_compaction": True,
            "compacted_range": [0, 5],
            "original_tokens": 1200,
            "compacted_tokens": 400,
        }
    )

    sanitized = engine.sanitize_input_items(items)
    # 孤立的 call_123 應被過濾剔除，且自訂中繼欄位（如 is_compaction）應被完全淨化
    assert len(sanitized) == 3
    assert sanitized[0]["role"] == "user"
    assert sanitized[1]["role"] == "assistant"
    assert sanitized[2]["role"] == "user"
    assert sanitized[2]["content"] == "摘要內容"
    assert "is_compaction" not in sanitized[2]
    assert "compacted_range" not in sanitized[2]
    assert "original_tokens" not in sanitized[2]
    assert "compacted_tokens" not in sanitized[2]

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


def test_subagent_graceful_degradation_at_max_rounds(monkeypatch):
    async def _test():
        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []

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

        mock_engine = MockSubEngine(mock_client, "test-model")
        from pocket_agent.engines.factory import EngineFactory

        monkeypatch.setattr(EngineFactory, "create", lambda *args, **kwargs: mock_engine)
        monkeypatch.setattr("pocket_agent.core.agent.call_fs_tool", MagicMock(return_value="檔案內容"))

        subagent = Agent(
            client=mock_client,
            mcp=mock_mcp,
            model="test-model",
            is_subagent=True,
            max_tool_rounds=2,
            provider="openai",
        )

        _thinking, reply, _usage = await subagent.chat("請探勘專案")
        assert "階段性研究報告" in reply
        # 驗證最後一輪 tools 被強制清空
        assert len(received_tools_history[0]) > 0  # 第 1 輪有 tools
        assert len(received_tools_history[1]) == 0  # 第 2 輪 (最後一輪) tools 被清空

    asyncio.run(_test())


def test_console_formatter_prepends_newline_and_icons():
    import logging

    from pocket_agent.core.agent import _ConsoleFormatter

    formatter = _ConsoleFormatter("%(message)s")

    record_compaction = logging.LogRecord(
        name="pocket-agent",
        level=logging.WARNING,
        pathname="",
        lineno=0,
        msg="Context compaction: cleared 3 old tool outputs",
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record_compaction)
    assert formatted.startswith("\n")
    assert "🧹" in formatted

    record_truncate = logging.LogRecord(
        name="pocket-agent",
        level=logging.WARNING,
        pathname="",
        lineno=0,
        msg="Tool output truncated on append",
        args=(),
        exc_info=None,
    )
    formatted_trunc = formatter.format(record_truncate)
    assert formatted_trunc.startswith("\n")
    assert "✂️" in formatted_trunc


def test_agent_web_research_convergence_limits(monkeypatch):
    """測試網路調研工具的軟性收斂警告（5次）與硬性攔截上限（10次）。"""

    async def _test():
        from pocket_agent.engines.base import EventText, EventToolCallDone, EventToolCallStart
        from pocket_agent.engines.factory import EngineFactory

        mock_client = MagicMock()
        mock_mcp = MagicMock()

        round_counter = [0]

        class MockWebEngine(BaseEngine):
            async def stream_chat(
                self, input_items, instructions, tools, reasoning_param=None, cancel_check=None, **kwargs
            ):
                round_counter[0] += 1
                r = round_counter[0]
                if r <= 11:
                    yield EventToolCallStart(f"call_{r}", "search_web")
                    yield EventToolCallDone(f"call_{r}", "search_web", f'{{"query": "q_{r}"}}')
                else:
                    yield EventText("完成任務輸出")

        mock_engine = MockWebEngine(mock_client, "test-model")
        monkeypatch.setattr(EngineFactory, "create", lambda *args, **kwargs: mock_engine)
        monkeypatch.setattr("pocket_agent.core.agent.call_web_tool", lambda name, args: "搜尋結果摘要")

        agent = Agent(
            client=mock_client,
            mcp=mock_mcp,
            model="test-model",
            max_tool_rounds=15,
            provider="openai",
        )
        agent.web_research_soft_limit = 5
        agent.web_research_hard_limit = 10

        emitted_tool_results = []

        def stream_listener(kind, data):
            if kind == "tool_done" and isinstance(data, dict):
                emitted_tool_results.append(data.get("result", ""))

        agent.on_stream = stream_listener

        _thinking, reply, _usage = await agent.chat("進行調研任務")
        assert "完成任務輸出" in reply

        # 第 5 次呼叫應附加軟性收斂提醒
        assert any("系統提醒：調研收斂" in out for out in emitted_tool_results)
        # 第 11 次呼叫 (超出硬上限 10 次) 應被硬性攔截
        assert any("調研上限攔截" in out for out in emitted_tool_results)

    asyncio.run(_test())


def test_agent_blocks_truncated_chat_completion_tool_call_with_valid_json(monkeypatch):
    """驗證 ChatCompletions 在 finish_reason='length' 且累積參數為合法 JSON 時，Agent 絕不執行該工具。"""

    async def _test():
        from types import SimpleNamespace

        from pocket_agent.engines.chat_engine import StandardChatCompletionsEngine
        from pocket_agent.engines.factory import EngineFactory

        chunk_tc = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call_trunc",
                                function=SimpleNamespace(name="read_file", arguments='{"path": "valid.txt"}'),
                            )
                        ],
                        content=None,
                        reasoning_content=None,
                    ),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        chunk_len = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(tool_calls=None, content=None, reasoning_content=None),
                    finish_reason="length",
                )
            ],
            usage=None,
        )

        class _FakeStream:
            def __aiter__(self):
                async def _g():
                    yield chunk_tc
                    yield chunk_len

                return _g()

            async def close(self):
                pass

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=_FakeStream())
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []

        engine = StandardChatCompletionsEngine(client=mock_client, model="gemini-pro")
        monkeypatch.setattr(EngineFactory, "create", lambda *args, **kwargs: engine)

        fs_spy = MagicMock(return_value="should not run")
        monkeypatch.setattr("pocket_agent.core.agent.call_fs_tool", fs_spy)

        agent = Agent(client=mock_client, mcp=mock_mcp, model="gemini-pro", provider="gemini")
        await agent.chat("測試截斷工具呼叫")

        fs_spy.assert_not_called()

    asyncio.run(_test())

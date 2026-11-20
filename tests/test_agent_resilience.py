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


def test_divergence_guard_warns_and_stops():
    """驗證 DivergenceGuard 於連續 2 次重複時警告、連續 3 次重複時中止。"""
    from pocket_agent.core.agent import DivergenceGuard

    guard = DivergenceGuard(warn_threshold=2, stop_threshold=3)
    call_a = [{"name": "read_file", "arguments": '{"path": "a.py", "start_line": 1}'}]
    call_a_reordered = [{"name": "read_file", "arguments": '{"start_line": 1, "path": "a.py"}'}]
    call_b = [{"name": "read_file", "arguments": '{"path": "b.py"}'}]

    assert guard.observe_round(call_a) == "ok"
    assert guard.observe_round(call_a_reordered) == "warn"
    assert guard.observe_round(call_a) == "stop"
    assert guard.observe_round(call_b) == "ok"


def test_instructions_static_prefix_invariant_across_queries():
    """驗證不同 query 與權限狀態下，_instructions() 的靜態核心前綴保持位元組一致。"""
    mock_client = MagicMock()
    mock_mcp = MagicMock()
    agent = Agent(client=mock_client, mcp=mock_mcp, model="gpt-5", system_prompt="CORE_SYSTEM_RULE")

    inst_1 = agent._instructions("query one", repo_map_str="MAP_A", skills_prompt="SKILLS_A")
    agent.accept_edits = True
    inst_2 = agent._instructions("query two", repo_map_str="MAP_B", skills_prompt="SKILLS_B")

    anchor = "CORE_SYSTEM_RULE"
    assert anchor in inst_1 and anchor in inst_2
    prefix_1 = inst_1.split(anchor, 1)[0] + anchor
    prefix_2 = inst_2.split(anchor, 1)[0] + anchor
    assert prefix_1 == prefix_2


def test_agent_not_found_probe_emits_not_found_status_and_avoids_error_bailout(monkeypatch):
    """連續兩輪 [NOT_FOUND] 探測應發出 status='not_found' 且不觸發連續錯誤熔斷。"""

    async def _test():
        from pocket_agent.engines.base import EventText, EventToolCallDone, EventToolCallStart
        from pocket_agent.engines.factory import EngineFactory

        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []

        rounds = [0]
        seen_inputs: list[list] = []
        seen_tools: list[list] = []

        class MockProbeEngine(BaseEngine):
            async def stream_chat(
                self,
                input_items,
                instructions,
                tools,
                reasoning_param=None,
                cancel_check=None,
                build_tools_callback=None,
            ):
                rounds[0] += 1
                seen_inputs.append(list(input_items))
                seen_tools.append(list(tools))

                if rounds[0] == 1:
                    yield EventToolCallStart("c1", "list_dir")
                    yield EventToolCallDone("c1", "list_dir", '{"path": ".generate-system-docs"}')
                elif rounds[0] == 2:
                    yield EventToolCallStart("c2", "read_file")
                    yield EventToolCallDone("c2", "read_file", '{"path": "optional.yml"}')
                else:
                    yield EventText("已確認選用設定不存在，採用預設值。")

        mock_engine = MockProbeEngine(mock_client, "test-model")
        monkeypatch.setattr(EngineFactory, "create", lambda *args, **kwargs: mock_engine)

        def fake_fs(name, args, on_confirm, workspace_dirs=None):
            if name == "list_dir":
                return "[NOT_FOUND] 目錄不存在：.generate-system-docs"
            return "[NOT_FOUND] 檔案不存在：optional.yml"

        monkeypatch.setattr("pocket_agent.core.agent.call_fs_tool", fake_fs)

        events: list[tuple[str, object]] = []
        agent = Agent(client=mock_client, mcp=mock_mcp, model="test-model", provider="openai")
        agent.on_stream = lambda kind, data: events.append((kind, data))

        _thinking, reply, _usage = await agent.chat("分析專案")

        done_events = [data for kind, data in events if kind == "tool_done" and isinstance(data, dict)]
        assert len(done_events) == 2
        assert all(ev.get("ok") is True and ev.get("status") == "not_found" for ev in done_events)
        assert len(seen_tools[2]) > 0
        expected_out = "[NOT_FOUND] 目錄不存在：.generate-system-docs"
        assert any(isinstance(item, dict) and expected_out in str(item.get("output", "")) for item in seen_inputs[1])
        assert "已確認選用設定不存在，採用預設值。" in reply

    asyncio.run(_test())


def test_agent_accept_edits_overwrite_required_avoids_bailout_and_recovers(tmp_path, monkeypatch):
    """Accept-edits 模式下連續漏帶 overwrite=true 應發出 overwrite_required 且不觸發錯誤熔斷。"""
    import json

    monkeypatch.chdir(tmp_path)
    (tmp_path / "file_a.md").write_text("old a", encoding="utf-8")
    (tmp_path / "file_b.md").write_text("old b", encoding="utf-8")

    async def _test():
        from pocket_agent.engines.base import EventText, EventToolCallDone, EventToolCallStart
        from pocket_agent.engines.factory import EngineFactory

        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []

        rounds = [0]
        seen_instructions: list[str] = []
        seen_tools: list[list] = []

        class MockOverwriteEngine(BaseEngine):
            async def stream_chat(
                self,
                input_items,
                instructions,
                tools,
                reasoning_param=None,
                cancel_check=None,
                build_tools_callback=None,
            ):
                rounds[0] += 1
                seen_instructions.append(instructions)
                seen_tools.append(list(tools))

                if rounds[0] == 1:
                    yield EventToolCallStart("c1", "write_to_file")
                    yield EventToolCallDone(
                        "c1",
                        "write_to_file",
                        json.dumps({"path": "file_a.md", "content": "new a"}),
                    )
                elif rounds[0] == 2:
                    yield EventToolCallStart("c2", "write_to_file")
                    yield EventToolCallDone(
                        "c2",
                        "write_to_file",
                        json.dumps({"path": "file_b.md", "content": "new b"}),
                    )
                elif rounds[0] == 3:
                    yield EventToolCallStart("c3", "write_to_file")
                    yield EventToolCallDone(
                        "c3",
                        "write_to_file",
                        json.dumps({"path": "file_a.md", "content": "new a", "overwrite": True}),
                    )
                else:
                    yield EventText("已完成覆寫。")

        mock_engine = MockOverwriteEngine(mock_client, "test-model")
        monkeypatch.setattr(EngineFactory, "create", lambda *args, **kwargs: mock_engine)

        confirm_mock = MagicMock(return_value=False)
        events: list[tuple[str, object]] = []
        agent = Agent(client=mock_client, mcp=mock_mcp, model="test-model", provider="openai")
        agent.accept_edits = True
        agent.on_confirm = confirm_mock
        agent.on_stream = lambda kind, data: events.append((kind, data))

        _thinking, reply, _usage = await agent.chat("覆寫檔案")

        assert "已完成覆寫。" in reply
        assert "overwrite=true" in seen_instructions[0]
        assert len(seen_tools[2]) > 0
        done_events = [data for kind, data in events if kind == "tool_done" and isinstance(data, dict)]
        assert len(done_events) == 3
        assert done_events[0]["ok"] is True and done_events[0]["status"] == "overwrite_required"
        assert done_events[1]["ok"] is True and done_events[1]["status"] == "overwrite_required"
        assert done_events[2]["ok"] is True and done_events[2]["status"] == "ok"
        assert (tmp_path / "file_a.md").read_text(encoding="utf-8") == "new a"
        assert (tmp_path / "file_b.md").read_text(encoding="utf-8") == "old b"
        assert agent.activity.get_file_changes()["modified"] == ["file_a.md"]
        confirm_mock.assert_not_called()

    asyncio.run(_test())


def test_agent_manual_confirm_overwrites_existing_file_in_single_round(tmp_path, monkeypatch):
    """手動確認模式下（accept_edits=False）未帶 overwrite=true 寫入既有檔案應單輪確認並覆寫成功。"""
    import json

    monkeypatch.chdir(tmp_path)
    doc = tmp_path / "docs" / "architecture" / "architecture-generated.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("old doc", encoding="utf-8")

    async def _test():
        from pocket_agent.engines.base import EventText, EventToolCallDone, EventToolCallStart
        from pocket_agent.engines.factory import EngineFactory

        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []

        rounds = [0]

        class MockManualOverwriteEngine(BaseEngine):
            async def stream_chat(
                self,
                input_items,
                instructions,
                tools,
                reasoning_param=None,
                cancel_check=None,
                build_tools_callback=None,
            ):
                rounds[0] += 1
                if rounds[0] == 1:
                    yield EventToolCallStart("c1", "write_to_file")
                    yield EventToolCallDone(
                        "c1",
                        "write_to_file",
                        json.dumps({"path": "docs/architecture/architecture-generated.md", "content": "new doc"}),
                    )
                else:
                    yield EventText("已更新架構文件。")

        mock_engine = MockManualOverwriteEngine(mock_client, "test-model")
        monkeypatch.setattr(EngineFactory, "create", lambda *args, **kwargs: mock_engine)

        confirm_prompts: list[str] = []
        events: list[tuple[str, object]] = []
        agent = Agent(client=mock_client, mcp=mock_mcp, model="test-model", provider="openai")
        agent.accept_edits = False
        agent.on_confirm = lambda msg: (confirm_prompts.append(msg), True)[1]
        agent.on_stream = lambda kind, data: events.append((kind, data))

        _thinking, reply, _usage = await agent.chat("更新架構文件")

        assert "已更新架構文件。" in reply
        assert confirm_prompts == ["覆寫既有檔案：docs/architecture/architecture-generated.md"]
        assert doc.read_text(encoding="utf-8") == "new doc"
        done_events = [data for kind, data in events if kind == "tool_done" and isinstance(data, dict)]
        assert len(done_events) == 1
        assert done_events[0]["ok"] is True
        assert done_events[0]["status"] == "ok"
        assert "docs/architecture/architecture-generated.md" in agent.activity.get_file_changes()["modified"]

    asyncio.run(_test())


def test_agent_preserves_intermediate_assistant_text_across_tool_rounds(tmp_path, monkeypatch):
    """多輪工具迴圈中模型於工具呼叫前輸出的文字應保留在下一輪輸入與對話歷史中。"""
    import json

    monkeypatch.chdir(tmp_path)
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")

    async def _test():
        from pocket_agent.engines.base import EventText, EventToolCallDone, EventToolCallStart
        from pocket_agent.engines.factory import EngineFactory

        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []

        seen_inputs: list[list[dict]] = []
        rounds = [0]

        class MockMultiRoundEngine(BaseEngine):
            async def stream_chat(
                self,
                input_items,
                instructions,
                tools,
                reasoning_param=None,
                cancel_check=None,
                build_tools_callback=None,
            ):
                seen_inputs.append([dict(item) for item in input_items if isinstance(item, dict)])
                rounds[0] += 1
                if rounds[0] == 1:
                    yield EventText("已載入規則，先讀取筆記檔案。")
                    yield EventToolCallStart("c1", "read_file")
                    yield EventToolCallDone("c1", "read_file", json.dumps({"path": "note.txt"}))
                else:
                    yield EventText("已完成檢查。")

        mock_engine = MockMultiRoundEngine(mock_client, "test-model")
        monkeypatch.setattr(EngineFactory, "create", lambda *args, **kwargs: mock_engine)

        agent = Agent(client=mock_client, mcp=mock_mcp, model="test-model", provider="openai")
        _thinking, reply, _usage = await agent.chat("檢視筆記")

        assert "已完成檢查。" in reply
        assert len(seen_inputs) == 2
        round2_roles = [m.get("role") or m.get("type") for m in seen_inputs[1]]
        assert round2_roles == ["user", "assistant", "function_call", "function_call_output"]
        assert seen_inputs[1][1]["content"] == "已載入規則，先讀取筆記檔案。"
        history_contents = [m.get("content") for m in agent.history if m.get("role") == "assistant"]
        assert "已載入規則，先讀取筆記檔案。" in history_contents
        assert any("已完成檢查。" in str(c) for c in history_contents)

    asyncio.run(_test())


def test_agent_replace_target_not_found_soft_status_avoids_error_bailout(tmp_path, monkeypatch):
    """replace_file_content 連續兩輪回傳 [TARGET_NOT_FOUND] 時屬可復原診斷，不應觸發雙錯誤迫停。"""
    import json

    monkeypatch.chdir(tmp_path)
    sample = tmp_path / "sample.py"
    sample.write_text("line\n" * 44 + "gamma = 1\n", encoding="utf-8")

    async def _test():
        from pocket_agent.engines.base import EventText, EventToolCallDone, EventToolCallStart
        from pocket_agent.engines.factory import EngineFactory

        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []

        rounds = [0]

        class MockRecoverReplaceEngine(BaseEngine):
            async def stream_chat(
                self,
                input_items,
                instructions,
                tools,
                reasoning_param=None,
                cancel_check=None,
                build_tools_callback=None,
            ):
                rounds[0] += 1
                if rounds[0] <= 2:
                    yield EventToolCallStart(f"c{rounds[0]}", "replace_file_content")
                    yield EventToolCallDone(
                        f"c{rounds[0]}",
                        "replace_file_content",
                        json.dumps(
                            {
                                "path": "sample.py",
                                "start_line": 1,
                                "end_line": 2,
                                "target": "gamma = 1",
                                "replacement": "gamma = 2",
                            }
                        ),
                    )
                elif rounds[0] == 3:
                    yield EventToolCallStart("c3", "replace_file_content")
                    yield EventToolCallDone(
                        "c3",
                        "replace_file_content",
                        json.dumps(
                            {
                                "path": "sample.py",
                                "start_line": 40,
                                "end_line": 46,
                                "target": "gamma = 1",
                                "replacement": "gamma = 2",
                            }
                        ),
                    )
                else:
                    yield EventText("已完成替換。")

        mock_engine = MockRecoverReplaceEngine(mock_client, "test-model")
        monkeypatch.setattr(EngineFactory, "create", lambda *args, **kwargs: mock_engine)

        events: list[tuple[str, object]] = []
        agent = Agent(client=mock_client, mcp=mock_mcp, model="test-model", provider="openai")
        agent.accept_edits = True
        agent.on_stream = lambda kind, data: events.append((kind, data))

        _thinking, reply, _usage = await agent.chat("更新 gamma")

        assert "已完成替換。" in reply
        assert rounds[0] == 4
        assert "gamma = 2" in sample.read_text(encoding="utf-8")
        done_events = [data for kind, data in events if kind == "tool_done" and isinstance(data, dict)]
        assert len(done_events) == 3
        assert done_events[0]["ok"] is True and done_events[0]["status"] == "target_not_found"
        assert done_events[1]["ok"] is True and done_events[1]["status"] == "target_not_found"
        assert done_events[2]["ok"] is True and done_events[2]["status"] == "ok"

    asyncio.run(_test())


def test_agent_auto_allows_current_workspace_scratch_but_gates_sensitive_session(tmp_path, monkeypatch):
    """非敏感會話下寫入目前工作區 scratch_dir 應免確認放行；專案檔與敏感會話仍須確認。"""
    import json

    from pocket_agent.system.paths import scratch_dir

    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()

    async def _test():
        from pocket_agent.engines.base import EventText, EventToolCallDone, EventToolCallStart
        from pocket_agent.engines.factory import EngineFactory

        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []

        scratch_target = scratch_dir() / "assess" / "assess.md"
        repo_target = tmp_path / "docs" / "out.md"
        rounds = [0]

        class MockScratchAndRepoEngine(BaseEngine):
            async def stream_chat(
                self,
                input_items,
                instructions,
                tools,
                reasoning_param=None,
                cancel_check=None,
                build_tools_callback=None,
            ):
                rounds[0] += 1
                if rounds[0] == 1:
                    yield EventToolCallStart("c1", "write_to_file")
                    yield EventToolCallDone(
                        "c1",
                        "write_to_file",
                        json.dumps({"path": str(scratch_target), "content": "scratch report"}),
                    )
                    yield EventToolCallStart("c2", "write_to_file")
                    yield EventToolCallDone(
                        "c2",
                        "write_to_file",
                        json.dumps({"path": str(repo_target), "content": "repo doc"}),
                    )
                elif rounds[0] == 2:
                    yield EventText("第一輪完成。")
                elif rounds[0] == 3:
                    yield EventToolCallStart("c3", "write_to_file")
                    yield EventToolCallDone(
                        "c3",
                        "write_to_file",
                        json.dumps({"path": str(scratch_target), "content": "sensitive update", "overwrite": True}),
                    )
                else:
                    yield EventText("敏感會話完成。")

        mock_engine = MockScratchAndRepoEngine(mock_client, "test-model")
        monkeypatch.setattr(EngineFactory, "create", lambda *args, **kwargs: mock_engine)

        confirm_prompts: list[str] = []
        agent = Agent(client=mock_client, mcp=mock_mcp, model="test-model", provider="openai")
        agent.accept_edits = False
        agent.on_confirm = lambda msg: (confirm_prompts.append(msg), True)[1]

        _, reply1, _ = await agent.chat("撰寫評估與專案文件")
        assert "第一輪完成。" in reply1
        assert scratch_target.read_text(encoding="utf-8") == "scratch report"
        assert repo_target.read_text(encoding="utf-8") == "repo doc"
        assert len(confirm_prompts) == 1
        assert "out.md" in confirm_prompts[0]

        confirm_prompts.clear()
        agent.is_sensitive_session = True
        _, reply2, _ = await agent.chat("敏感模式寫入暫存檔")
        assert "敏感會話完成。" in reply2
        assert scratch_target.read_text(encoding="utf-8") == "sensitive update"
        assert len(confirm_prompts) == 1

    asyncio.run(_test())

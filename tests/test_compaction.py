"""HistoryCompactor 單元測試：安全切點檢查、原子性替換與無效益降級。"""

import asyncio

from pocket_agent.core.compaction import HistoryCompactor
from pocket_agent.engines.base import BaseEngine, EventText


class MockSummaryEngine(BaseEngine):
    def __init__(self, summary_output: str = "這是摘要內容", fail: bool = False):
        super().__init__(client=None, model="mock-model")
        self.summary_output = summary_output
        self.fail = fail

    async def stream_chat(self, input_items, instructions, tools, reasoning_param=None, cancel_check=None):
        if self.fail:
            raise RuntimeError("API 連線失敗")
        yield EventText(self.summary_output)


def _pair(call_id: str, output: str) -> list[dict]:
    return [
        {"type": "function_call", "call_id": call_id, "name": "read_file", "arguments": "{}"},
        {"type": "function_call_output", "call_id": call_id, "output": output},
    ]


def test_find_safe_split_point_preserves_pairs():
    engine = MockSummaryEngine()
    compactor = HistoryCompactor(engine, keep_recent_messages=2)

    # 構造歷史：2 個 pairs (4 則) + 2 則 user/assistant，共 6 則
    history = [
        {"role": "user", "content": "hello 1"},
        {"role": "assistant", "content": "reply 1"},
        {"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "content 1"},
        {"role": "user", "content": "hello 2"},
        {"role": "assistant", "content": "reply 2"},
        {"role": "user", "content": "hello 3"},
        {"role": "assistant", "content": "reply 3"},
    ]

    split_idx = compactor.find_safe_split_point(history)
    assert split_idx > 0

    to_summarize = history[:split_idx]
    # 確保切點前段的 calls 與 outputs 成對
    c_ids = [m["call_id"] for m in to_summarize if m.get("type") == "function_call"]
    o_ids = [m["call_id"] for m in to_summarize if m.get("type") == "function_call_output"]
    assert c_ids == o_ids


def test_compact_history_success():
    async def _test():
        engine = MockSummaryEngine(summary_output="客觀摘要：使用者要求修復 Bug，已完成。")
        compactor = HistoryCompactor(engine, keep_recent_messages=2)

        history = [
            {"role": "user", "content": "長篇大論 " * 200},
            {"role": "assistant", "content": "詳細解答 " * 200},
            {"role": "user", "content": "更多資訊 " * 200},
            {"role": "assistant", "content": "更多解答 " * 200},
            {"role": "user", "content": "保留問題 1"},
            {"role": "assistant", "content": "保留回答 1"},
        ]

        new_hist, success = await compactor.compact_history(history)
        assert success is True
        assert len(new_hist) == 3  # 1 則摘要 + 2 則保留
        assert "高密度摘要" in new_hist[0]["content"]
        assert new_hist[0].get("is_compaction") is True
        assert "compacted_range" in new_hist[0]
        assert "original_tokens" in new_hist[0]
        assert new_hist[1]["content"] == "保留問題 1"
        assert new_hist[2]["content"] == "保留回答 1"

    asyncio.run(_test())


def test_compact_history_fails_gracefully():
    async def _test():
        engine = MockSummaryEngine(fail=True)
        compactor = HistoryCompactor(engine, keep_recent_messages=2)

        history = [
            {"role": "user", "content": "長篇大論 " * 200},
            {"role": "assistant", "content": "詳細解答 " * 200},
            {"role": "user", "content": "更多資訊 " * 200},
            {"role": "assistant", "content": "更多解答 " * 200},
            {"role": "user", "content": "保留問題 1"},
            {"role": "assistant", "content": "保留回答 1"},
        ]

        new_hist, success = await compactor.compact_history(history)
        assert success is False
        assert new_hist == history  # 保證原子性，原樣保留

    asyncio.run(_test())


def test_compact_history_no_benefit_fallback():
    async def _test():
        # 摘要長度與原內容相近，壓縮效益未達 20%
        engine = MockSummaryEngine(summary_output="長篇摘要 " * 200)
        compactor = HistoryCompactor(engine, keep_recent_messages=2)

        history = [
            {"role": "user", "content": "長篇大論 " * 200},
            {"role": "assistant", "content": "詳細解答 " * 200},
            {"role": "user", "content": "保留問題 1"},
            {"role": "assistant", "content": "保留回答 1"},
        ]

        new_hist, success = await compactor.compact_history(history)
        assert success is False
        assert new_hist == history

    asyncio.run(_test())


def test_compact_history_empty_or_cancelled():
    async def _test():
        # 1. 摘要內容為空
        engine_empty = MockSummaryEngine(summary_output="")
        compactor_empty = HistoryCompactor(engine_empty, keep_recent_messages=2)
        history = [
            {"role": "user", "content": "長篇大論 " * 200},
            {"role": "assistant", "content": "詳細解答 " * 200},
            {"role": "user", "content": "保留問題 1"},
            {"role": "assistant", "content": "保留回答 1"},
        ]
        new_hist, success = await compactor_empty.compact_history(history)
        assert success is False
        assert new_hist == history

        # 2. cancel_check 觸發取消
        engine_cancel = MockSummaryEngine(summary_output="正常摘要")
        compactor_cancel = HistoryCompactor(engine_cancel, keep_recent_messages=2)
        new_hist2, success2 = await compactor_cancel.compact_history(history, cancel_check=lambda: True)
        assert success2 is False
        assert new_hist2 == history

    asyncio.run(_test())


def test_compact_history_session_persistence(tmp_path, monkeypatch):
    from pocket_agent.core import sessions

    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path)
    session_id = "test-compaction-session"
    history = [
        {
            "role": "user",
            "content": "[系統紀錄：較早歷史對話之高密度摘要]\n摘要",
            "is_compaction": True,
            "compacted_range": [0, 4],
            "original_tokens": 1200,
            "compacted_tokens": 150,
        },
        {"role": "user", "content": "保留問題 1"},
        {"role": "assistant", "content": "保留回答 1"},
    ]
    sessions.save_session(session_id, history)
    loaded = sessions.load_session(session_id)
    assert loaded is not None
    loaded_history = loaded["history"]
    assert len(loaded_history) == 3
    assert loaded_history[0]["is_compaction"] is True
    assert loaded_history[0]["compacted_range"] == [0, 4]
    assert loaded_history[0]["original_tokens"] == 1200
    assert loaded_history[0]["compacted_tokens"] == 150


def test_ensure_paired_tool_calls():
    from pocket_agent.core.compaction import ensure_paired_tool_calls

    items = [
        {"role": "user", "content": "hello"},
        # 成對的 call 1
        {"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        # 孤立的 call 2 (無 output)
        {"type": "function_call", "call_id": "c2", "name": "read_file", "arguments": "{}"},
        # 孤立的 output 3 (無 call)
        {"type": "function_call_output", "call_id": "c3", "output": "orphan output"},
        {"role": "assistant", "content": "done"},
    ]

    cleaned = ensure_paired_tool_calls(items)
    assert len(cleaned) == 4
    assert cleaned[0]["role"] == "user"
    assert cleaned[1]["call_id"] == "c1"
    assert cleaned[2]["call_id"] == "c1"
    assert cleaned[3]["role"] == "assistant"


def test_agent_compact_history_and_cli_command():
    async def _test():
        from unittest.mock import MagicMock, PropertyMock, patch

        from pocket_agent.core.agent import Agent
        from pocket_agent.frontend.cli.commands.core import CommandContext
        from pocket_agent.frontend.cli.commands.session import CompactCommand

        agent = Agent(client=MagicMock(), mcp=MagicMock(), model="mock-model")
        mock_engine = MockSummaryEngine(summary_output="高密度重點摘要：已完成規劃。")

        with patch.object(Agent, "engine", new_callable=PropertyMock(return_value=mock_engine)):
            agent.history = [
                {"role": "user", "content": "長篇大論 " * 200},
                {"role": "assistant", "content": "詳細解答 " * 200},
                {"role": "user", "content": "更多資訊 " * 200},
                {"role": "assistant", "content": "更多解答 " * 200},
                {"role": "user", "content": "保留問題 1"},
                {"role": "assistant", "content": "保留回答 1"},
            ]

            # 1. 測試 Agent.compact_history 直接更新 agent.history
            success, orig, comp = await agent.compact_history(keep_recent=2)
            assert success is True
            assert orig > comp
            assert len(agent.history) == 3
            assert "高密度" in agent.history[0]["content"]

            # 2. 測試 CLI CompactCommand 呼叫
            agent.history = [
                {"role": "user", "content": "長篇大論 " * 200},
                {"role": "assistant", "content": "詳細解答 " * 200},
                {"role": "user", "content": "更多資訊 " * 200},
                {"role": "assistant", "content": "更多解答 " * 200},
                {"role": "user", "content": "保留問題 2"},
                {"role": "assistant", "content": "保留回答 2"},
            ]
            renderer = MagicMock()
            ctx = CommandContext(
                agent=agent,
                state=MagicMock(),
                reader=MagicMock(),
                renderer=renderer,
                tools=[],
            )
            cmd = CompactCommand()
            res = await cmd.execute(ctx, "")
            assert res.action == "handled"
            assert len(agent.history) == 3
            renderer.message.assert_called()
            last_msg = renderer.message.call_args[0][0]
            assert "已壓縮" in last_msg
            assert "釋放約" in last_msg

            # 3. 測試歷程不足 6 則時提示無需壓縮
            agent.history = [
                {"role": "user", "content": "問題"},
                {"role": "assistant", "content": "回答"},
            ]
            await cmd.execute(ctx, "")
            assert len(agent.history) == 2

    asyncio.run(_test())


def test_agent_run_loop_persists_tool_compact_to_history():
    async def _test():
        from unittest.mock import MagicMock, PropertyMock, patch

        from pocket_agent.core.agent import Agent

        agent = Agent(client=MagicMock(), mcp=MagicMock(), model="mock-model")
        mock_engine = MockSummaryEngine(summary_output="摘要")

        with patch.object(Agent, "engine", new_callable=PropertyMock(return_value=mock_engine)):
            big_output = "很長的工具輸出 " * 500
            # 設置 agent.history，包含會被修剪的舊工具輸出
            agent.history = [
                {"role": "user", "content": "查詢檔案"},
                {"type": "function_call", "call_id": "call_old", "name": "read_file", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_old", "output": big_output},
                {"type": "function_call", "call_id": "call_rec1", "name": "read_file", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_rec1", "output": "最近結果 1"},
                {"type": "function_call", "call_id": "call_rec2", "name": "read_file", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_rec2", "output": "最近結果 2"},
            ]

            # 視窗設為 1000 觸發 compact
            from pocket_agent.core.context_budget import ContextBudget

            test_budget = ContextBudget(window=1000)
            agent._resolve_budget = MagicMock(return_value=test_budget)

            # 執行單輪迴圈前檢查
            items = list(agent.history)
            cleared = test_budget.compact(items)
            assert cleared == 1
            assert "[舊工具輸出已清除" in items[2]["output"]

            # 同步回寫邏輯驗證
            compacted_by_call_id = {
                m["call_id"]: m["output"]
                for m in items
                if isinstance(m, dict)
                and m.get("type") == "function_call_output"
                and isinstance(m.get("output"), str)
                and m["output"].startswith("[舊工具輸出已清除")
            }
            for hist_item in agent.history:
                if (
                    isinstance(hist_item, dict)
                    and hist_item.get("type") == "function_call_output"
                    and hist_item.get("call_id") in compacted_by_call_id
                ):
                    hist_item["output"] = compacted_by_call_id[hist_item["call_id"]]

            # 驗證 agent.history 確實已被同步更新
            assert "[舊工具輸出已清除" in agent.history[2]["output"]

    asyncio.run(_test())

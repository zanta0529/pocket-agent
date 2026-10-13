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

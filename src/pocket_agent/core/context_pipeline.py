"""送出前上下文預算縮減管線（Chain of Responsibility）。"""

import logging
from dataclasses import dataclass
from typing import Callable

from pocket_agent.core.context_budget import ContextBudget
from pocket_agent.engines.base import BaseEngine

log = logging.getLogger("pocket-agent")


@dataclass
class ContextPrepareResult:
    """上下文管線處理結果。"""

    input_items: list
    estimated_tokens: int
    overflow_error: str | None = None


class ContextPipeline:
    """依序執行：清除舊工具輸出 → LLM 歷史摘要 → 安全滑動視窗 → 預算超限防護。"""

    def __init__(self, budget: ContextBudget, engine: BaseEngine) -> None:
        self.budget = budget
        self.engine = engine

    async def prepare(
        self,
        input_items: list,
        history: list[dict],
        instructions: str,
        tools: list,
        *,
        cancel_check: Callable[[], bool],
        activity: object | None = None,
    ) -> ContextPrepareResult:
        # 1. 優先清除較舊的工具輸出 (Tool Compact)，並直接複用該次估算結果
        cleared_outputs, estimated_tokens = self.budget.compact_and_estimate(input_items, instructions, tools)
        if cleared_outputs > 0:
            self._sync_cleared_outputs(input_items, history)

        # 2. 若清除工具輸出後仍超限，嘗試以專用 Compactor 進行 LLM 歷史摘要
        if estimated_tokens > self.budget.compact_threshold and len(input_items) > 8:
            from pocket_agent.core.compaction import HistoryCompactor

            compactor = HistoryCompactor(self.engine)
            new_items, compacted = await compactor.compact_history(
                input_items,
                cancel_check=cancel_check,
                activity=activity,
            )
            if compacted:
                input_items = new_items
                history[:] = list(new_items)
                estimated_tokens = self.budget.estimate_items(input_items, instructions, tools)

        # 3. 若摘要後仍超限且歷史輪次過長，才安全滑動視窗裁減中間對話
        if estimated_tokens > self.budget.compact_threshold and len(input_items) > 14:
            log.warning(
                "Context overflow predicted (est. %d tokens). Truncating history safely.",
                estimated_tokens,
            )
            from pocket_agent.core.compaction import ensure_paired_tool_calls

            head_msgs = [m for m in input_items[:2] if m.get("role") == "user"]
            notice = {
                "role": "system",
                "content": "[System] 中間的對話歷史已被系統自動截斷以避免 Context Overflow。",
            }
            tail_msgs = input_items[-10:]
            input_items = head_msgs + [notice] + ensure_paired_tool_calls(tail_msgs)
            history[:] = list(input_items)
            estimated_tokens = self.budget.estimate_items(input_items, instructions, tools)

        # 4. 若經過上述所有縮減策略後，Payload 總量依然超過可用輸入預算，明確終止送出
        if estimated_tokens > self.budget.effective_input_budget:
            log.error(
                "Payload 估算 tokens (%d) 仍超出可用輸入預算 (%d)，終止當輪送出。",
                estimated_tokens,
                self.budget.effective_input_budget,
            )
            return ContextPrepareResult(
                input_items=input_items,
                estimated_tokens=estimated_tokens,
                overflow_error=(
                    f"【上下文預算防護】當前對話與工具定義總量 (約 {estimated_tokens:,} tokens) "
                    f"已超過可用輸入預算 ({self.budget.effective_input_budget:,} tokens)。\n\n"
                    "系統已自動攔截此請求以防止 API 崩潰。建議作法：請縮減 System Prompt、使用 `/clear` 清除對話，"
                    "或利用 `run_subagents_parallel` 將任務分拆給子代理處理。"
                ),
            )

        return ContextPrepareResult(input_items=input_items, estimated_tokens=estimated_tokens)

    @staticmethod
    def _sync_cleared_outputs(input_items: list, history: list[dict]) -> None:
        compacted_by_call_id = {
            m["call_id"]: m["output"]
            for m in input_items
            if isinstance(m, dict)
            and m.get("type") == "function_call_output"
            and isinstance(m.get("output"), str)
            and m["output"].startswith("[舊工具輸出已清除")
        }
        for hist_item in history:
            if (
                isinstance(hist_item, dict)
                and hist_item.get("type") == "function_call_output"
                and hist_item.get("call_id") in compacted_by_call_id
            ):
                hist_item["output"] = compacted_by_call_id[hist_item["call_id"]]

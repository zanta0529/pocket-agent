"""上下文預算：解析模型視窗、估算用量、在寫入時截斷工具輸出、超限時清除舊工具結果。

與供應商無關：視窗大小依「使用者設定 → 引擎查詢 → 保底值」解析，預算邏輯只依賴解析出的數字。
"""

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("pocket-agent")

# 查不到視窗時的保守保底值（tokens）
DEFAULT_CONTEXT_WINDOW = 32_000
# 超過視窗此比例時開始清除舊工具結果
COMPACT_RATIO = 0.7
# 清除舊結果時的目標水位比例（低於觸發門檻，拉出緩衝空間）
COMPACT_TARGET_RATIO = 0.6
# 單筆工具輸出佔視窗的上限比例
TOOL_OUTPUT_RATIO = 0.08
# 無 tokenizer 時的保守估算：約 3 字元 1 token
CHARS_PER_TOKEN = 3
# 清除舊結果時保留的最近工具輸出數量
KEEP_RECENT_OUTPUTS = 2
# 使用率顏色分界
WARN_RATIO = 0.7
CRITICAL_RATIO = 0.85

_PLACEHOLDER = "[舊工具輸出已為節省上下文而清除；如仍需要，請重新以精準範圍讀取。]"

SOURCE_CONFIG = "設定值"
SOURCE_ENGINE = "供應商查詢"
SOURCE_DEFAULT = "保底值（未設定，供應商未提供）"

_encoding: Any = None
_encoding_loaded = False


def estimate_tokens(obj: Any) -> int:
    """估算 token 數：有 tiktoken 時使用，否則以字元數保守推估。"""
    global _encoding, _encoding_loaded
    text = obj if isinstance(obj, str) else str(obj)
    if not _encoding_loaded:
        _encoding_loaded = True
        try:
            import tiktoken

            _encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _encoding = None
    if _encoding is not None:
        try:
            return len(_encoding.encode(text, disallowed_special=()))
        except Exception:
            pass
    return len(text) // CHARS_PER_TOKEN + 1


def resolve_context_window(configured: Any, engine_value: int | None) -> tuple[int, str]:
    """回傳 (視窗 tokens, 來源說明)。無效或非正數的值一律略過。"""
    for value, source in ((configured, SOURCE_CONFIG), (engine_value, SOURCE_ENGINE)):
        try:
            number = int(value) if value is not None else 0
        except TypeError, ValueError:
            number = 0
        if number > 0:
            return number, source
    return DEFAULT_CONTEXT_WINDOW, SOURCE_DEFAULT


@dataclass(frozen=True)
class ContextUsage:
    """某一時間點的上下文使用快照。"""

    used_tokens: int
    window: int
    source: str
    is_estimate: bool
    effective_budget: int = 0

    @property
    def ratio(self) -> float:
        return self.used_tokens / self.window if self.window > 0 else 0.0

    @property
    def effective_ratio(self) -> float:
        return self.used_tokens / self.effective_budget if self.effective_budget > 0 else 0.0

    @property
    def level(self) -> str:
        """回傳 ok／warn／critical，供前端決定顏色。"""
        if self.ratio >= CRITICAL_RATIO:
            return "critical"
        if self.ratio >= WARN_RATIO:
            return "warn"
        return "ok"

    def display_lines(self) -> list[str]:
        kind = "估算" if self.is_estimate else "實際"
        prefix = "約 " if self.is_estimate else ""
        lines = [
            f"上下文：{prefix}{self.used_tokens:,} / {self.window:,} tokens（{self.ratio:.0%}，{kind}）",
        ]
        if self.effective_budget > 0:
            lines.append(
                f"有效輸入預算：{prefix}{self.used_tokens:,} / {self.effective_budget:,} tokens"
                f"（{self.effective_ratio:.0%}）"
            )
        lines.append(f"視窗來源：{self.source}")
        return lines


# 預設預留輸出/推理空間與安全餘裕（tokens）
DEFAULT_RESERVE_OUTPUT = 4096
DEFAULT_SAFETY_MARGIN = 1024


class ContextBudget:
    """依視窗大小計算預算，並提供截斷與清除工具。"""

    def __init__(
        self,
        window: int,
        source: str = SOURCE_DEFAULT,
        reserve_output: int = DEFAULT_RESERVE_OUTPUT,
        safety_margin: int = DEFAULT_SAFETY_MARGIN,
        compact_target_ratio: float = COMPACT_TARGET_RATIO,
    ):
        self.window = max(1, int(window))
        self.source = source
        self.reserve_output = max(0, int(reserve_output))
        self.safety_margin = max(0, int(safety_margin))
        self.compact_target_ratio = float(compact_target_ratio)

    @property
    def effective_input_budget(self) -> int:
        """可用輸入預算 ＝ 有效視窗 － 預留輸出空間 － 安全餘裕。最低不小於 1000 tokens。"""
        budget = self.window - self.reserve_output - self.safety_margin
        return max(1000, budget)

    @property
    def compact_threshold(self) -> int:
        """超過可用輸入預算的 COMPACT_RATIO 時觸發清除工具輸出。"""
        return int(self.effective_input_budget * COMPACT_RATIO)

    @property
    def compact_target_tokens(self) -> int:
        """清除舊工具輸出時的目標水位，低於觸發門檻以避免頻繁觸發。"""
        return int(self.effective_input_budget * self.compact_target_ratio)

    @property
    def max_tool_output_chars(self) -> int:
        return max(500, int(self.effective_input_budget * TOOL_OUTPUT_RATIO * CHARS_PER_TOKEN))

    def truncate_tool_output(self, text: Any) -> Any:
        """工具輸出寫入 history 前只截斷一次；非字串或未超限時原樣回傳。"""
        if not isinstance(text, str):
            return text
        limit = self.max_tool_output_chars
        if len(text) <= limit:
            return text
        log.warning("Tool output truncated on append (from %d to %d chars).", len(text), limit)
        return (
            text[:limit] + f"\n\n... (輸出過長，已截斷，省略 {len(text) - limit} 字元) ...\n"
            "[系統建議] 若遺失關鍵資訊，請限縮搜尋範圍重試（例如指定 start_line/end_line）；"
            "若任務龐大，建議將進度寫入 `.agents/scratch/notes.md` 草稿區。"
        )

    def estimate_items(self, items: list, instructions: str = "", tools: list | None = None) -> int:
        """估算送出內容（含 instructions 與 tools schema）的 token 數。"""
        total = sum(estimate_tokens(item) for item in items)
        total += estimate_tokens(instructions) if instructions else 0
        total += estimate_tokens(tools) if tools else 0
        return total

    def snapshot(
        self,
        items: list,
        instructions: str = "",
        tools: list | None = None,
        actual_input_tokens: int = 0,
    ) -> ContextUsage:
        """有供應商回報的實際 input tokens 時採用實際值，否則估算。"""
        if actual_input_tokens > 0:
            return ContextUsage(
                actual_input_tokens,
                self.window,
                self.source,
                False,
                effective_budget=self.effective_input_budget,
            )
        return ContextUsage(
            self.estimate_items(items, instructions, tools),
            self.window,
            self.source,
            True,
            effective_budget=self.effective_input_budget,
        )

    def compact(self, items: list, instructions: str = "", tools: list | None = None) -> int:
        """超過門檻時，由舊到新把工具輸出換成佔位文字（保留 call／output 成對）。回傳清除筆數。

        最近 KEEP_RECENT_OUTPUTS 筆輸出不清除。items 會被原地修改。
        """
        total = self.estimate_items(items, instructions, tools)
        if total <= self.compact_threshold:
            return 0

        output_idx = [
            i for i, item in enumerate(items) if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]
        candidates = output_idx[:-KEEP_RECENT_OUTPUTS] if KEEP_RECENT_OUTPUTS else output_idx
        cleared = 0
        for i in candidates:
            if total <= self.compact_target_tokens:
                break
            old = items[i].get("output")
            if not isinstance(old, str) or old == _PLACEHOLDER:
                continue
            total -= max(0, estimate_tokens(old) - estimate_tokens(_PLACEHOLDER))
            items[i]["output"] = _PLACEHOLDER
            cleared += 1
        if cleared:
            log.warning("Context compaction: cleared %d old tool outputs (est. %d tokens now).", cleared, total)
        return cleared

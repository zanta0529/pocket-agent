"""上下文預算：解析模型視窗、估算用量、在寫入時截斷工具輸出、超限時清除舊工具結果。

與供應商無關：視窗大小依「使用者設定 → 引擎查詢 → 保底值」解析，預算邏輯只依賴解析出的數字。
"""

import logging
from dataclasses import dataclass
from typing import Any

from pocket_agent.system.constants import (
    CHARS_PER_TOKEN_ESTIMATE,
    DEFAULT_COMPACT_RATIO,
    DEFAULT_COMPACT_TARGET_RATIO,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_KEEP_RECENT_OUTPUTS,
    DEFAULT_RESERVE_OUTPUT_TOKENS,
    DEFAULT_SAFETY_MARGIN_TOKENS,
    DEFAULT_TOOL_OUTPUT_RATIO,
)

log = logging.getLogger("pocket-agent")

# 向後相容別名 (對齊 constants.py)
COMPACT_RATIO = DEFAULT_COMPACT_RATIO
COMPACT_TARGET_RATIO = DEFAULT_COMPACT_TARGET_RATIO
TOOL_OUTPUT_RATIO = DEFAULT_TOOL_OUTPUT_RATIO
CHARS_PER_TOKEN = CHARS_PER_TOKEN_ESTIMATE
KEEP_RECENT_OUTPUTS = DEFAULT_KEEP_RECENT_OUTPUTS
DEFAULT_RESERVE_OUTPUT = DEFAULT_RESERVE_OUTPUT_TOKENS
DEFAULT_SAFETY_MARGIN = DEFAULT_SAFETY_MARGIN_TOKENS

# 使用率顏色分界
WARN_RATIO = 0.7
CRITICAL_RATIO = 0.85

_PLACEHOLDER = "[舊工具輸出已清除以節省上下文。請依已取得之既有結論繼續完成任務，避免重複檢索相同資料。]"


def format_compact_placeholder(tool_name: str = "", tool_args: str = "") -> str:
    """產生富含語意但不誘導模型重新發起相同查詢的佔位文字。"""
    if not tool_name:
        return _PLACEHOLDER
    args_snippet = " ".join(str(tool_args).split())
    if len(args_snippet) > 60:
        args_snippet = args_snippet[:57] + "..."
    if args_snippet and args_snippet != "{}":
        return (
            f"[舊工具輸出已清除以節省上下文（工具：{tool_name}，參數：{args_snippet}）。"
            "請依既有結論與推論繼續產出，避免重複發起相同檢索。]"
        )
    return f"[舊工具輸出已清除以節省上下文（工具：{tool_name}）。請依既有結論與推論繼續產出，避免重複發起相同檢索。]"


SOURCE_CONFIG = "設定值"
SOURCE_ENGINE = "供應商查詢"
SOURCE_DEFAULT = "保底值（未設定，供應商未提供）"

_str_token_cache: dict[str, int] = {}
_MAX_TOKEN_CACHE_SIZE = 512


def estimate_tokens(obj: Any) -> int:
    """估算 token 數：以字元數保守推估，並快取常見字串結果。"""
    text = obj if isinstance(obj, str) else str(obj)
    cached = _str_token_cache.get(text)
    if cached is not None:
        return cached
    result = len(text) // CHARS_PER_TOKEN + 1
    if len(text) <= 200_000:
        if len(_str_token_cache) >= _MAX_TOKEN_CACHE_SIZE:
            _str_token_cache.clear()
        _str_token_cache[text] = result
    return result


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

    def truncate_tool_output(
        self,
        text: Any,
        tool_name: str = "",
        call_id: str = "",
        is_sensitive: bool = False,
    ) -> Any:
        """工具輸出寫入 history 前進行動態檢查；超限時啟動落盤虛擬化與雙端切片。"""
        if not isinstance(text, str):
            return text
        limit = self.max_tool_output_chars
        if len(text) <= limit:
            return text
        log.warning("Tool output truncated on append (from %d to %d chars).", len(text), limit)
        from pocket_agent.core.spillover import default_spillover

        return default_spillover.spill_or_pass(
            text,
            tool_name=tool_name,
            call_id=call_id,
            max_chars=limit,
            is_sensitive=is_sensitive,
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
        estimated_tokens: int | None = None,
    ) -> ContextUsage:
        """有供應商回報的實際 input tokens 時採用實際值，否則沿用既有的估算值或重新估算。"""
        if actual_input_tokens > 0:
            return ContextUsage(
                actual_input_tokens,
                self.window,
                self.source,
                False,
                effective_budget=self.effective_input_budget,
            )
        resolved_tokens = (
            estimated_tokens if estimated_tokens is not None else self.estimate_items(items, instructions, tools)
        )
        return ContextUsage(
            resolved_tokens,
            self.window,
            self.source,
            True,
            effective_budget=self.effective_input_budget,
        )

    def compact_and_estimate(
        self,
        items: list,
        instructions: str = "",
        tools: list | None = None,
    ) -> tuple[int, int]:
        """超過門檻時，由舊到新把工具輸出換成佔位文字，並回傳 (清除筆數, 當前估算 tokens)。

        最近 KEEP_RECENT_OUTPUTS 筆輸出不清除。關鍵指引與技能輸出受白名單保護不清除。items 會被原地修改。
        """
        total = self.estimate_items(items, instructions, tools)
        if total <= self.compact_threshold:
            return 0, total

        # 建立 call_id -> function_call 映射，識別產生輸出的工具名稱與參數
        call_map: dict[str, dict] = {
            m.get("call_id"): m
            for m in items
            if isinstance(m, dict) and m.get("type") == "function_call" and m.get("call_id")
        }

        output_idx = [
            i for i, item in enumerate(items) if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]
        candidates = output_idx[:-KEEP_RECENT_OUTPUTS] if KEEP_RECENT_OUTPUTS else output_idx
        cleared = 0
        for i in candidates:
            if total <= self.compact_target_tokens:
                break
            old = items[i].get("output")
            if not isinstance(old, str) or old.startswith("[舊工具輸出已清除"):
                continue

            # 保護白名單：避免 use_skill 或核心規範 (AGENTS.md/PROJECT_INSTRUCTIONS.md) 被清除導致失憶死循環
            call_id = items[i].get("call_id")
            fc = call_map.get(call_id, {})
            tool_name = fc.get("name", "")
            tool_args = str(fc.get("arguments", ""))
            if tool_name == "use_skill" or any(p in tool_args for p in ("AGENTS.md", "PROJECT_INSTRUCTIONS.md")):
                continue

            placeholder_text = format_compact_placeholder(tool_name, tool_args)
            total -= max(0, estimate_tokens(old) - estimate_tokens(placeholder_text))
            items[i]["output"] = placeholder_text
            cleared += 1
        if cleared:
            log.info("Context compaction: cleared %d old tool outputs (est. %d tokens now).", cleared, total)
        return cleared, total

    def compact(self, items: list, instructions: str = "", tools: list | None = None) -> int:
        """超過門檻時，由舊到新把工具輸出換成佔位文字（保留 call／output 成對）。回傳清除筆數。"""
        cleared, _ = self.compact_and_estimate(items, instructions, tools)
        return cleared

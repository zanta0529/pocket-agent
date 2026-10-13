"""ContextBudget 單元測試：視窗解析、截斷、舊結果清除與使用量快照（不連網）。"""

from pocket_agent.core.context_budget import (
    _PLACEHOLDER,
    DEFAULT_CONTEXT_WINDOW,
    SOURCE_CONFIG,
    SOURCE_DEFAULT,
    SOURCE_ENGINE,
    ContextBudget,
    ContextUsage,
    resolve_context_window,
)
from pocket_agent.engines.base import BaseEngine, EventUsage, usage_field


def _pair(call_id: str, output: str) -> list[dict]:
    return [
        {"type": "function_call", "call_id": call_id, "name": "read_file", "arguments": "{}"},
        {"type": "function_call_output", "call_id": call_id, "output": output},
    ]


def test_resolve_priority_config_then_engine_then_default():
    assert resolve_context_window(64000, 128000) == (64000, SOURCE_CONFIG)
    assert resolve_context_window(None, 128000) == (128000, SOURCE_ENGINE)
    assert resolve_context_window(None, None) == (DEFAULT_CONTEXT_WINDOW, SOURCE_DEFAULT)


def test_resolve_ignores_invalid_values():
    assert resolve_context_window("abc", 0) == (DEFAULT_CONTEXT_WINDOW, SOURCE_DEFAULT)
    assert resolve_context_window(-1, 4096) == (4096, SOURCE_ENGINE)


def test_truncate_tool_output_only_when_over_limit():
    budget = ContextBudget(32000)
    limit = budget.max_tool_output_chars
    assert budget.truncate_tool_output("x" * limit) == "x" * limit
    truncated = budget.truncate_tool_output("x" * (limit + 1000))
    assert truncated.startswith("x" * limit)
    assert "已截斷" in truncated
    assert budget.truncate_tool_output(None) is None


def test_compact_keeps_pairs_and_recent_outputs():
    budget = ContextBudget(1000)  # 門檻 700 tokens
    big = "資料" * 800
    items: list = []
    for i in range(5):
        items += _pair(f"c{i}", big)

    cleared = budget.compact(items)

    assert cleared > 0
    outputs = [m for m in items if m["type"] == "function_call_output"]
    assert len([m for m in items if m["type"] == "function_call"]) == len(outputs) == 5
    assert outputs[-1]["output"] == big
    assert outputs[-2]["output"] == big
    assert outputs[0]["output"] == _PLACEHOLDER


def test_compact_noop_under_threshold():
    budget = ContextBudget(32000)
    items = _pair("c0", "small")
    assert budget.compact(items) == 0
    assert items[1]["output"] == "small"


def test_snapshot_prefers_actual_tokens():
    budget = ContextBudget(32000, SOURCE_CONFIG)
    estimated = budget.snapshot(_pair("c0", "hello"))
    assert estimated.is_estimate is True
    actual = budget.snapshot([], actual_input_tokens=1234)
    assert actual.is_estimate is False
    assert actual.used_tokens == 1234


def test_usage_level_and_display_lines():
    assert ContextUsage(100, 1000, SOURCE_CONFIG, False).level == "ok"
    assert ContextUsage(750, 1000, SOURCE_CONFIG, False).level == "warn"
    assert ContextUsage(900, 1000, SOURCE_CONFIG, True).level == "critical"
    lines = ContextUsage(900, 1000, SOURCE_DEFAULT, True).display_lines()
    assert "估算" in lines[0] and "90%" in lines[0]
    assert SOURCE_DEFAULT in lines[1]


def test_engine_defaults_and_usage_field():
    assert BaseEngine(client=None, model="m").context_window() is None
    assert EventUsage(10).input_tokens == 0
    assert usage_field({"prompt_tokens": 7}, "input_tokens", "prompt_tokens") == 7
    assert usage_field(object(), "input_tokens") == 0


def test_effective_input_budget_and_reserves():
    budget = ContextBudget(window=32000, reserve_output=4000, safety_margin=1000)
    assert budget.effective_input_budget == 27000
    assert budget.compact_threshold == int(27000 * 0.7)


def test_effective_input_budget_minimum_floor():
    # 當視窗極小且預留空間大於視窗時，應保有最低 1000 tokens 地板
    budget = ContextBudget(window=4000, reserve_output=4000, safety_margin=1000)
    assert budget.effective_input_budget == 1000


def test_read_file_char_limit(tmp_path):
    from pocket_agent.tools.fs import call_fs_tool

    # 建立一個包含超長單行（超過 12000 字元）的檔案
    long_file = tmp_path / "minified.js"
    long_content = "const data = '" + ("A" * 15000) + "';\nconsole.log(data);\n"
    long_file.write_text(long_content, encoding="utf-8")

    result = call_fs_tool("read_file", {"path": str(long_file)}, on_confirm=None, workspace_dirs=[str(tmp_path)])
    assert "單次讀取上限 12000 字元" in result
    assert "start_line=" in result

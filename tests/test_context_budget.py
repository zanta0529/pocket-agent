"""ContextBudget 單元測試：視窗解析、截斷、舊結果清除與使用量快照（不連網）。"""

from pocket_agent.core.context_budget import (
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
    assert "[系統日誌虛擬化]" in truncated
    assert "完整日誌路徑" in truncated
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
    assert "[舊工具輸出已清除" in outputs[0]["output"]
    assert "read_file" in outputs[0]["output"]


def test_compact_placeholder_includes_tool_context_and_prevents_retrigger():
    budget = ContextBudget(1000)
    items = [
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "search_web",
            "arguments": '{"query": "RHEL 9.8 podman"}',
        },
        {"type": "function_call_output", "call_id": "c1", "output": "大量搜尋結果 " * 200},
        {"type": "function_call", "call_id": "c_recent1", "name": "read_file", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c_recent1", "output": "最近結果 1 " * 200},
        {"type": "function_call", "call_id": "c_recent2", "name": "read_file", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c_recent2", "output": "最近結果 2 " * 200},
        {"role": "user", "content": "next prompt"},
    ]
    cleared = budget.compact(items)
    assert cleared == 1
    output = items[1]["output"]
    assert "search_web" in output
    assert "RHEL 9.8 podman" in output
    assert "避免重複發起相同檢索" in output
    assert "如仍需要，請重新以精準範圍讀取" not in output


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


def test_compact_clears_down_to_target_tokens():
    # 預算 1000 tokens，門檻 700 tokens，目標水位 600 tokens
    budget = ContextBudget(1000, reserve_output=0, safety_margin=0, compact_target_ratio=0.6)
    assert budget.compact_threshold == 700
    assert budget.compact_target_tokens == 600

    big = "資料" * 200
    items: list = []
    for i in range(5):
        items += _pair(f"c{i}", big)

    cleared = budget.compact(items)
    assert cleared > 0
    est = budget.estimate_items(items)
    assert est <= budget.compact_threshold


def test_usage_display_lines_with_effective_budget():
    usage = ContextUsage(500, 1000, SOURCE_CONFIG, False, effective_budget=800)
    lines = usage.display_lines()
    assert any("有效輸入預算" in line for line in lines)
    assert "62%" in lines[1]


def test_engine_context_window_cache():
    import time

    engine = BaseEngine(client=None, model="test-model")
    key = engine._cache_key()
    BaseEngine._context_window_cache[key] = (16000, time.time() + 100)
    assert engine.context_window() == 16000

    # 模擬過期
    BaseEngine._context_window_cache[key] = (16000, time.time() - 1)
    assert engine.context_window() is None


def test_is_context_overflow_detection():
    engine = BaseEngine(client=None, model="test-model")

    class FakeOverflowError(Exception):
        status_code = 400

        def __str__(self):
            return "context_length_exceeded: maximum context length is 8192"

    assert engine.is_context_overflow(FakeOverflowError()) is True

    class FakeOtherError(Exception):
        status_code = 400

        def __str__(self):
            return "invalid_parameter"

    assert engine.is_context_overflow(FakeOtherError()) is False


def test_read_file_single_long_line_notice(tmp_path):
    from pocket_agent.tools.fs import call_fs_tool

    long_file = tmp_path / "single_long.txt"
    long_file.write_text("A" * 15000, encoding="utf-8")

    result = call_fs_tool("read_file", {"path": str(long_file)}, on_confirm=None, workspace_dirs=[str(tmp_path)])
    assert "超長單行，已被字元截斷" in result
    assert "search_files" in result


def test_console_formatter_warning_indent_and_color():
    import logging

    from pocket_agent.core.agent import _LOG_FORMAT, _ConsoleFormatter

    formatter = _ConsoleFormatter(_LOG_FORMAT)
    record = logging.LogRecord(
        name="pocket-agent",
        level=logging.WARNING,
        pathname=__file__,
        lineno=10,
        msg="Context compaction: cleared 3 old tool outputs.",
        args=(),
        exc_info=None,
    )
    output = formatter.format(record)
    assert "│ 🧹 " in output
    assert "\033[38;5;245m" in output
    assert "Context compaction: cleared 3 old tool outputs." in output

    record_trunc = logging.LogRecord(
        name="pocket-agent",
        level=logging.WARNING,
        pathname=__file__,
        lineno=10,
        msg="Tool output truncated on append (from 100 to 50 chars).",
        args=(),
        exc_info=None,
    )
    output_trunc = formatter.format(record_trunc)
    assert "│ ✂️ " in output_trunc
    assert "\033[38;5;245m" in output_trunc


def test_read_file_char_offset_continuation(tmp_path):
    from pocket_agent.tools.fs import call_fs_tool

    long_file = tmp_path / "stream_code.js"
    content = ("A" * 5000) + ("B" * 5000)
    long_file.write_text(content, encoding="utf-8")

    result = call_fs_tool(
        "read_file",
        {"path": str(long_file), "start_line": 1, "char_offset": 5000},
        on_confirm=None,
        workspace_dirs=[str(tmp_path)],
    )
    assert "[+5000]" in result
    assert "B" * 100 in result
    assert "A" not in result


def test_compact_preserves_whitelisted_skills_and_guidelines():
    budget = ContextBudget(1000)  # 門檻 700 tokens
    big = "資料" * 800
    items = [
        {"type": "function_call", "call_id": "c_skill", "name": "use_skill", "arguments": '{"name": "review"}'},
        {"type": "function_call_output", "call_id": "c_skill", "output": big},
        {
            "type": "function_call",
            "call_id": "c_doc",
            "name": "read_file",
            "arguments": '{"path": "docs/PROJECT_INSTRUCTIONS.md"}',
        },
        {"type": "function_call_output", "call_id": "c_doc", "output": big},
        {"type": "function_call", "call_id": "c_normal", "name": "read_file", "arguments": '{"path": "src/app.py"}'},
        {"type": "function_call_output", "call_id": "c_normal", "output": big},
        {"type": "function_call", "call_id": "c_recent1", "name": "read_file", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c_recent1", "output": big},
        {"type": "function_call", "call_id": "c_recent2", "name": "read_file", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c_recent2", "output": big},
    ]

    cleared = budget.compact(items)
    assert cleared == 1  # 只有 c_normal 被清除
    outputs = {m["call_id"]: m["output"] for m in items if m["type"] == "function_call_output"}
    assert outputs["c_skill"] == big  # 白名單保護未被清除
    assert "[舊工具輸出已清除" in outputs["c_normal"]
    assert "src/app.py" in outputs["c_normal"]

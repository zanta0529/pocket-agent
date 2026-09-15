from io import StringIO

import pytest
from prompt_toolkit.formatted_text import fragment_list_to_text
from rich.cells import cell_len
from rich.console import Console

from pocket_agent.frontend.cli_render import CliRenderer, toolbar
from pocket_agent.frontend.cli_state import CliState


@pytest.mark.parametrize("width", [0, 1, 5, 40, 60, 80, 120])
def test_toolbar_width_and_literal_markup(width):
    state = CliState("openai", "<中文模型>[red]" * 20, ["中文 path.txt"])
    text = fragment_list_to_text(toolbar(state, width))
    assert cell_len(text) <= width
    if width >= 40:
        assert "待送 1" in text


def test_plain_output_preserves_literal_markup_and_markdown():
    stream = StringIO()
    renderer = CliRenderer(Console(file=stream, force_terminal=False, no_color=True, width=80))
    renderer.message("<model>[red]")
    renderer.user("first\nsecond")
    renderer.reply("**answer**\n\n```python\nprint(1)\n```", 1.0, 10)
    output = stream.getvalue()
    assert "<model>[red]" in output
    assert "first" in output and "second" in output
    assert "answer" in output and "print(1)" in output
    assert "\x1b" not in output


def test_toolbar_strips_control_characters():
    text = fragment_list_to_text(toolbar(CliState("openai", "x\x1b[2J\ny"), 80))
    assert "\x1b" not in text and "\n" not in text


def test_banner_narrow_and_wide():
    # Narrow width
    stream_narrow = StringIO()
    renderer_narrow = CliRenderer(Console(file=stream_narrow, force_terminal=False, no_color=True, width=39))
    renderer_narrow.banner()
    assert "╭──────╮" not in stream_narrow.getvalue()
    assert "Pocket Agent" in stream_narrow.getvalue()

    # Wide width
    stream_wide = StringIO()
    renderer_wide = CliRenderer(Console(file=stream_wide, force_terminal=False, no_color=True, width=80))
    renderer_wide.banner()
    assert "╭──────╮" in stream_wide.getvalue()
    assert "Pocket Agent" in stream_wide.getvalue()
    assert "你的終端 AI 工作夥伴" in stream_wide.getvalue()


def test_event_tool_classification():
    stream = StringIO()
    renderer = CliRenderer(Console(file=stream, force_terminal=False, no_color=True, width=80))
    renderer.event("tool_start", {"name": "read_file"})
    renderer.event("tool_start", {"name": "write_to_file"})
    renderer.event("tool_start", {"name": "run_command"})
    renderer.event("read", "path.txt")

    output = stream.getvalue()
    assert "● Read (read_file)" in output
    assert "● Write (write_to_file)" in output
    assert "● Action (run_command)" in output
    assert "● Read (path.txt)" in output


# ── F1: 串流輸出事件 ───────────────────────────────────


def test_stream_text_prints_header_once():
    """多次 text 事件只印一次 ● Pocket Agent 標頭。"""
    stream = StringIO()
    renderer = CliRenderer(Console(file=stream, force_terminal=False, no_color=True, width=80))
    renderer.event("text", "Hello")
    renderer.event("text", " world")
    output = stream.getvalue()
    assert output.count("● Pocket Agent") == 1
    assert "Hello" in output
    assert "world" in output
    assert renderer._stream_header_printed is True
    assert renderer._stream_has_content is True


def test_stream_round_start_resets_state():
    """round_start 後再次 text 重新印標頭。"""
    stream = StringIO()
    renderer = CliRenderer(Console(file=stream, force_terminal=False, no_color=True, width=80))
    renderer.event("text", "first round")
    renderer.event("round_start", "")
    assert renderer._stream_header_printed is False
    assert renderer._stream_has_content is False
    renderer.event("text", "second round")
    output = stream.getvalue()
    assert output.count("● Pocket Agent") == 2


def test_stream_round_start_without_content_no_extra_newline():
    """round_start 在沒有串流內容時，不輸出多餘換行。"""
    stream = StringIO()
    renderer = CliRenderer(Console(file=stream, force_terminal=False, no_color=True, width=80))
    # 沒有先輸出 text 就發 round_start
    renderer.event("round_start", "")
    # 狀態應保持重置
    assert renderer._stream_header_printed is False
    assert renderer._stream_has_content is False


# ── F2: tool_done 結果標記 ────────────────────────────


def test_tool_done_ok_prints_nothing():
    """tool_done ok=True 成功時不印出冗餘訊息。"""
    stream = StringIO()
    renderer = CliRenderer(Console(file=stream, force_terminal=False, no_color=True, width=80))
    renderer.event("tool_done", {"name": "read_file", "ok": True})
    output = stream.getvalue()
    assert output == ""


def test_tool_done_fail_prints_cross():
    """tool_done ok=False 印 ✗ 工具名。"""
    stream = StringIO()
    renderer = CliRenderer(Console(file=stream, force_terminal=False, no_color=True, width=80))
    renderer.event("tool_done", {"name": "write_to_file", "ok": False})
    output = stream.getvalue()
    assert "✗" in output
    assert "write_to_file" in output


# ── F3: thinking 縮排顯示 ─────────────────────────────


def test_thinking_event_shows_first_line():
    """thinking 只顯示首行，最多 80 字元。"""
    stream = StringIO()
    renderer = CliRenderer(Console(file=stream, force_terminal=False, no_color=True, width=120))
    renderer.event("thinking", "Line one\nLine two\nLine three")
    output = stream.getvalue()
    assert "∴" in output
    assert "Line one" in output
    assert "Line two" not in output


def test_thinking_event_truncates_long_line():
    """thinking 首行超過 80 字元時截斷。"""
    stream = StringIO()
    renderer = CliRenderer(Console(file=stream, force_terminal=False, no_color=True, width=120))
    long_line = "A" * 100
    renderer.event("thinking", long_line)
    output = stream.getvalue()
    # 只應顯示 80 個 A
    assert "A" * 80 in output
    assert "A" * 81 not in output


def test_thinking_empty_does_not_print():
    """空字串 thinking 事件不輸出任何內容。"""
    stream = StringIO()
    renderer = CliRenderer(Console(file=stream, force_terminal=False, no_color=True, width=80))
    renderer.event("thinking", "")
    renderer.event("thinking", "   \n   ")
    assert stream.getvalue().strip() == ""


# ── reply() streamed 參數 ─────────────────────────────


def test_reply_streamed_skips_content():
    """reply(streamed=True) 不重複印 Markdown 內容，只印 footer。"""
    stream = StringIO()
    renderer = CliRenderer(Console(file=stream, force_terminal=False, no_color=True, width=80))
    # 模擬已串流文字
    renderer._stream_has_content = True
    renderer._stream_header_printed = True
    renderer.reply("**answer**", 1.0, 10, streamed=True)
    output = stream.getvalue()
    # 不應有 Markdown 渲染（no_color 下 **answer** 渲染為 answer）
    # 但重點是「● Pocket Agent」標頭不應重複
    assert "● Pocket Agent" not in output
    # footer 應存在
    assert "本輪已返回" in output
    # 串流旗標應重置
    assert renderer._stream_header_printed is False
    assert renderer._stream_has_content is False


def test_reply_not_streamed_prints_content():
    """reply(streamed=False) 仍完整印出標頭與 Markdown。"""
    stream = StringIO()
    renderer = CliRenderer(Console(file=stream, force_terminal=False, no_color=True, width=80))
    renderer.reply("**answer**", 1.0, 10, streamed=False)
    output = stream.getvalue()
    assert "● Pocket Agent" in output
    assert "answer" in output
    assert "本輪已返回" in output

from io import StringIO

import pytest
from prompt_toolkit.formatted_text import fragment_list_to_text
from rich.cells import cell_len
from rich.console import Console

from agent.frontend.cli_render import CliRenderer, toolbar
from agent.frontend.cli_state import CliState


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

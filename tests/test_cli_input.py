import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent.frontend.cli_input import SlashCommandCompleter, file_argument, key_bindings, references
from agent.frontend.cli_state import CliState


def test_windows_quoted_references():
    path = r"C:\中文 folder\test.txt"
    quoted = chr(34) + path + chr(34)
    assert file_argument(quoted) == path
    assert references("read @" + quoted + " now") == [path]
    assert references(r"@C:\temp\a.txt") == [r"C:\temp\a.txt"]
    with pytest.raises(ValueError):
        references("@" + chr(34) + path)


def test_completion_tracks_provider():
    state = CliState("openai", "model")
    completer = SlashCommandCompleter(state)
    with patch("agent.commands.list_commands", return_value=[]):
        assert list(completer.get_completions(Document("/im"), CompleteEvent()))
        state.provider = "ollama"
        assert not list(completer.get_completions(Document("/im"), CompleteEvent()))


def test_path_completion_and_parser_agree(tmp_path):
    path = tmp_path / "中文 file.txt"
    path.write_text("data")
    completer = SlashCommandCompleter(CliState("openai", "model"))
    prefix = str(tmp_path) + "/"
    completions = list(completer.paths(prefix))
    assert len(completions) == 1
    assert Path(references("@" + completions[0].text)[0]) == path


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ("hello\r", "hello"),
        ("a\x1b\rb\r", "a\nb"),
        ("\x1b[200~a\nb\x1b[201~\r", "a\nb"),
        ("discard\x03kept\r", "kept"),
    ],
)
def test_keyboard_input(keys, expected):
    async def scenario():
        with create_pipe_input() as pipe:
            session = PromptSession(
                input=pipe,
                output=DummyOutput(),
                multiline=True,
                key_bindings=key_bindings(lambda: False),
            )
            pipe.send_text(keys)
            return await asyncio.wait_for(session.prompt_async(), timeout=3)

    assert asyncio.run(scenario()) == expected


@pytest.mark.parametrize(("keys", "exception"), [("\x03", KeyboardInterrupt), ("\x04", EOFError)])
def test_empty_input_interrupts(keys, exception):
    async def scenario():
        with create_pipe_input() as pipe:
            session = PromptSession(
                input=pipe,
                output=DummyOutput(),
                multiline=True,
                key_bindings=key_bindings(lambda: False),
            )
            pipe.send_text(keys)
            try:
                await asyncio.wait_for(session.prompt_async(), timeout=3)
            except exception:
                return True
            return False

    assert asyncio.run(scenario())


def test_selection_restores_chat_prompt_options():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from agent.frontend.cli_input import CliInput

    async def scenario():
        reader = CliInput.__new__(CliInput)
        reader.selecting = False
        reader.completer = SlashCommandCompleter(CliState("openai", "model"))
        reader.session = SimpleNamespace(prompt_async=AsyncMock(side_effect=["model", "chat"]))
        assert await reader.choose("Model", ["model"]) == "model"
        assert not reader.selecting
        assert await reader.read() == "chat"
        assert reader.session.prompt_async.call_args.kwargs["multiline"] is True
        assert reader.session.prompt_async.call_args.kwargs["completer"] is reader.completer

    asyncio.run(scenario())

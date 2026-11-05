import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.buffer import Buffer, CompletionState
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput

from pocket_agent.frontend.cli.input import SlashCommandCompleter, file_argument, key_bindings, references
from pocket_agent.frontend.cli.state import CliState


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
    with patch("pocket_agent.system.commands.list_commands", return_value=[]):
        assert list(completer.get_completions(Document("/im"), CompleteEvent()))
        state.provider = "ollama"
        assert not list(completer.get_completions(Document("/im"), CompleteEvent()))


@pytest.fixture
def isolated_slash_completer() -> Iterator[SlashCommandCompleter]:
    with (
        patch("pocket_agent.system.commands.list_commands", return_value=[]),
        patch("pocket_agent.extensions.skills.list_skills", return_value=[]),
        patch("pocket_agent.core.subagents.list_subagents", return_value=[]),
        patch("pocket_agent.system.paths.is_portable", return_value=False),
    ):
        yield SlashCommandCompleter(CliState("ollama", "model"))


def test_slash_command_completion_returns_all_candidates(isolated_slash_completer: SlashCommandCompleter) -> None:
    from pocket_agent.frontend.cli.input import BUILTINS

    completions = list(isolated_slash_completer.get_completions(Document("/"), CompleteEvent()))
    assert len(completions) > 20
    assert [completion.text for completion in completions] == list(BUILTINS)
    assert all(completion.style != "class:completion-menu.more" for completion in completions)


@pytest.mark.parametrize("prefix", ["/", "/model", "/no-such-command", "/m"])
def test_slash_command_completion_refilters_without_pages(
    isolated_slash_completer: SlashCommandCompleter, prefix: str
) -> None:
    from pocket_agent.frontend.cli.input import BUILTINS

    # 先建立完整候選，再切換篩選條件，不應遺留分頁狀態。
    list(isolated_slash_completer.get_completions(Document("/"), CompleteEvent()))
    completions = list(isolated_slash_completer.get_completions(Document(prefix), CompleteEvent()))
    assert [completion.text for completion in completions] == [name for name in BUILTINS if name.startswith(prefix[1:])]


@pytest.mark.parametrize(("up_count", "target_index"), [(0, 23), (14, 9)])
@pytest.mark.parametrize(("confirm", "suffix"), [("\r", ""), ("\t", " ")])
def test_slash_navigation_crosses_old_page_boundaries(
    isolated_slash_completer: SlashCommandCompleter, up_count: int, target_index: int, confirm: str, suffix: str
) -> None:
    completions = list(isolated_slash_completer.get_completions(Document("/"), CompleteEvent()))
    confirmed = []

    async def scenario() -> str:
        with create_pipe_input() as pipe:
            bindings = key_bindings(lambda: False, state=None, agent=None, completer=isolated_slash_completer)

            @bindings.add("c-g")
            def finish(event: KeyPressEvent) -> None:
                confirmed.append(event.current_buffer.complete_state is None)
                event.app.exit(result=event.current_buffer.text)

            session = PromptSession(
                input=pipe,
                output=DummyOutput(),
                multiline=True,
                completer=isolated_slash_completer,
                complete_while_typing=False,
                key_bindings=bindings,
            )

            def pre_run() -> None:
                buffer = session.app.current_buffer
                buffer.document = Document("/")
                buffer.complete_state = CompletionState(buffer.document, completions=completions)
                buffer.go_to_completion(0)
                pipe.send_text("\x1b[B" * 23 + "\x1b[A" * up_count + confirm + "\x07")

            return await asyncio.wait_for(session.prompt_async(pre_run=pre_run), timeout=3)

    assert asyncio.run(scenario()) == "/" + completions[target_index].text + suffix
    # Enter／Tab 只確認補齊；必須由後續 Ctrl+G 才結束 prompt。
    assert confirmed == [True]


@pytest.mark.parametrize(("confirm", "expected"), [("\r", "/more-before"), ("\t", "/more-before ")])
def test_completion_names_containing_old_paging_words(
    isolated_slash_completer: SlashCommandCompleter, confirm: str, expected: str
) -> None:
    commands = [{"name": "more-before", "description": "正常指令"}]
    with patch("pocket_agent.system.commands.list_commands", return_value=commands):
        document = Document("/more-")
        completions = list(isolated_slash_completer.get_completions(document, CompleteEvent()))
    buffer = Buffer(document=document)
    buffer.complete_state = CompletionState(document, completions=completions)
    bindings = key_bindings(lambda: False, state=None, agent=None, completer=isolated_slash_completer)
    event = SimpleNamespace(current_buffer=buffer)
    key = Keys.ControlM if confirm == "\r" else Keys.ControlI
    bindings.get_bindings_for_keys((key,))[-1].handler(event)
    assert buffer.text == expected
    assert buffer.complete_state is None


def test_up_without_completion_preserves_history_navigation() -> None:
    async def scenario() -> str:
        with create_pipe_input() as pipe:
            session = PromptSession(
                input=pipe,
                output=DummyOutput(),
                history=InMemoryHistory(["previous input"]),
                multiline=True,
                key_bindings=key_bindings(lambda: False, state=None, agent=None),
            )

            async def drive() -> str:
                prompt_task = asyncio.create_task(session.prompt_async())
                try:
                    while session.default_buffer._load_history_task is None:
                        await asyncio.sleep(0)
                    await session.default_buffer._load_history_task
                    pipe.send_text("\x1b[A\r")
                    return await prompt_task
                finally:
                    if not prompt_task.done():
                        prompt_task.cancel()
                        await asyncio.gather(prompt_task, return_exceptions=True)

            return await asyncio.wait_for(drive(), timeout=3)

    assert asyncio.run(scenario()) == "previous input"


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
    ],
)
def test_keyboard_input(keys, expected):
    async def scenario():
        with create_pipe_input() as pipe:
            session = PromptSession(
                input=pipe,
                output=DummyOutput(),
                multiline=True,
                key_bindings=key_bindings(lambda: False, state=None, agent=None),
            )
            pipe.send_text(keys)
            return await asyncio.wait_for(session.prompt_async(), timeout=3)

    assert asyncio.run(scenario()) == expected


# @pytest.mark.xfail(
#     strict=False,
#     reason=(
#         "在 CI 無 TTY 環境下，prompt_toolkit 可能在 key binding 攔截前就將 \\x03 "
#         "轉為 KeyboardInterrupt，導致 asyncio.run() 中斷整個進程。"
#         "本機有 TTY 時行為正確（Ctrl+C 清空 buffer 而非退出），為平台差異非程式碼錯誤。"
#     ),
# )
# def test_ctrl_c_clears_buffer_and_continues():
#     """Ctrl+C 在 buffer 有文字時應清空 buffer，使用者可繼續輸入並送出。"""

#     async def scenario():
#         with create_pipe_input() as pipe:
#             session = PromptSession(
#                 input=pipe,
#                 output=DummyOutput(),
#                 multiline=True,
#                 key_bindings=key_bindings(lambda: False),
#             )
#             pipe.send_text("discard\x03kept\r")
#             return await asyncio.wait_for(session.prompt_async(), timeout=3)

#     try:
#         result = asyncio.run(scenario())
#     except KeyboardInterrupt:
#         # CI 無 TTY 環境：prompt_toolkit Application 在 binding 前攔截 \x03
#         # 並以 KeyboardInterrupt 傳出；此為平台差異，直接 skip 而非 fail
#         pytest.skip("prompt_toolkit 在無 TTY 環境將 \\x03 轉為 KeyboardInterrupt（平台差異）")
#     assert result == "kept"


@pytest.mark.parametrize(("keys", "exception"), [("\x03", KeyboardInterrupt), ("\x04", EOFError)])
def test_empty_input_interrupts(keys, exception):
    async def scenario():
        with create_pipe_input() as pipe:
            session = PromptSession(
                input=pipe,
                output=DummyOutput(),
                multiline=True,
                key_bindings=key_bindings(lambda: False, state=None, agent=None),
            )
            pipe.send_text(keys)
            await asyncio.wait_for(session.prompt_async(), timeout=3)

    with pytest.raises(exception):
        asyncio.run(scenario())


def test_selection_restores_chat_prompt_options():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pocket_agent.frontend.cli.input import CliInput

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


def test_slash_command_alias_display_names():
    completer = SlashCommandCompleter(CliState("openai", "model"))
    with (
        patch("pocket_agent.system.commands.list_commands", return_value=[]),
        patch("pocket_agent.extensions.skills.list_skills", return_value=[]),
        patch("pocket_agent.core.subagents.list_subagents", return_value=[]),
    ):
        completions = {c.text: c.display_text for c in completer.get_completions(Document("/mcp"), CompleteEvent())}
        assert completions.get("mcp") == "mcp (tools)"

        tools_completions = {
            c.text: c.display_text for c in completer.get_completions(Document("/tools"), CompleteEvent())
        }
        assert tools_completions.get("mcp") == "mcp (tools)"

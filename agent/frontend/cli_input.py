import os
import re
import shutil
import sys
from collections.abc import Callable, Iterator

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.output import ColorDepth

from .cli_render import plain, toolbar
from .cli_state import CliState

BUILTINS = {
    "file": "附加一般檔案",
    "attachments": "查看／移除待送附件",
    "model": "切換當次提供者與模型",
    "session": "查看／載入歷史對話",
    "compact": "摘要並壓縮中間對話歷史，回收 context 空間",
    "clear": "清除對話，保留附件",
    "help": "指令說明",
    "tools": "工具清單",
    "add-dir": "尚未接入，不會讀取目錄",
    "logout": "登出",
    "quit": "離開",
}
QUOTE = chr(34)
REFERENCE = re.compile(r"(?:^|\s)@(?:" + QUOTE + "([^" + QUOTE + "]+)" + QUOTE + r"(?=\s|$)|([^\s" + QUOTE + "]+))")


def file_argument(value: str) -> str:
    value = value.strip()
    if value.startswith(QUOTE):
        if not value.endswith(QUOTE) or len(value) < 3:
            raise ValueError("路徑雙引號未成對或路徑為空")
        value = value[1:-1]
    if not value or QUOTE in value:
        raise ValueError("用法：/file <路徑>（含空白可加雙引號）")
    return value


def references(text: str) -> list[str]:
    matches = list(REFERENCE.finditer(text))
    for start in re.finditer(r"(?:^|\s)@" + QUOTE, text):
        if not any(m.start() == start.start() for m in matches):
            raise ValueError("@ 路徑雙引號未成對，或結尾缺少空白")
    return [m.group(1) or m.group(2) for m in matches]


class SlashCommandCompleter(Completer):
    def __init__(self, state: CliState) -> None:
        self.state = state

    def paths(self, prefix: str) -> Iterator[Completion]:
        quoted = prefix.startswith(QUOTE)
        value = prefix[1:] if quoted else prefix
        if QUOTE in value:
            return
        dirname, basename = os.path.split(value)
        try:
            with os.scandir(os.path.expanduser(dirname or ".")) as entries:
                for entry in entries:
                    if not entry.name.lower().startswith(basename.lower()):
                        continue
                    directory = entry.is_dir()
                    candidate = os.path.join(dirname, entry.name) + (os.sep if directory else "")
                    if quoted or any(c.isspace() for c in candidate):
                        candidate = QUOTE + candidate + ("" if directory else QUOTE)
                    yield Completion(
                        candidate,
                        start_position=-len(prefix),
                        display=plain(entry.name),
                        display_meta="目錄" if directory else "檔案",
                    )
        except OSError:
            return

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterator[Completion]:
        text = document.text_before_cursor
        if text.startswith("/file "):
            yield from self.paths(text[6:])
            return
        pattern = r"(?:^|\s)@(" + QUOTE + "[^" + QUOTE + r"]*|[^\s" + QUOTE + "]*)$"
        match = re.search(pattern, text)
        if match:
            yield from self.paths(match.group(1))
            return
        if not text.startswith("/") or any(c.isspace() for c in text):
            return
        from agent.commands import list_commands

        commands = dict(BUILTINS)
        if self.state.provider == "openai":
            commands["image"] = "產生圖片（待送附件作參考圖）"
        for command in list_commands():
            commands.setdefault(command["name"], command["description"])
        for name, description in commands.items():
            if name.startswith(text[1:]):
                yield Completion(name, start_position=-(len(text) - 1), display_meta=plain(description))


def key_bindings(selecting: Callable[[], bool], on_clear_screen: Callable[[], None] | None = None) -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("enter")
    def submit(event: KeyPressEvent) -> None:
        event.current_buffer.validate_and_handle()

    @bindings.add("escape", "enter", filter=Condition(lambda: not selecting()))
    def newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add("escape", filter=Condition(selecting), eager=True)
    def cancel(event: KeyPressEvent) -> None:
        event.app.exit(exception=KeyboardInterrupt())

    @bindings.add("c-c")
    def interrupt(event: KeyPressEvent) -> None:
        if event.current_buffer.text and not selecting():
            event.current_buffer.reset()
        else:
            event.app.exit(exception=KeyboardInterrupt())

    @bindings.add("tab")
    def complete(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if buffer.complete_state:
            buffer.complete_next()
        else:
            buffer.start_completion(select_first=True)

    @bindings.add("c-l")
    def clear_screen(event: KeyPressEvent) -> None:
        """清除終端畫面（不清對話歷史），符合 terminal 習慣。"""
        event.app.renderer.clear()
        if on_clear_screen is not None:
            on_clear_screen()

    return bindings


class CliInput:
    def __init__(self, state: CliState, on_clear_screen: Callable[[], None] | None = None) -> None:
        self.selecting = False
        self.completer = SlashCommandCompleter(state)
        self.session = PromptSession(
            multiline=True,
            completer=self.completer,
            complete_while_typing=True,
            bottom_toolbar=lambda: toolbar(state, shutil.get_terminal_size().columns),
            key_bindings=key_bindings(lambda: self.selecting, on_clear_screen=on_clear_screen),
            erase_when_done=True,
            color_depth=ColorDepth.DEPTH_1_BIT if "NO_COLOR" in os.environ or not sys.stdout.isatty() else None,
        )

    async def read(self, default: str = "") -> str:
        return await self.session.prompt_async(
            "› ",
            default=default,
            completer=self.completer,
            multiline=True,
        )

    async def choose(self, label: str, choices: list[str]) -> str | None:
        from prompt_toolkit.completion import WordCompleter

        self.selecting = True
        try:
            return (
                await self.session.prompt_async(
                    label + "（Esc 返回）：",
                    completer=WordCompleter(choices, sentence=True),
                    default="",
                    multiline=False,
                )
            ).strip()
        except (KeyboardInterrupt, EOFError):
            return None
        finally:
            self.selecting = False

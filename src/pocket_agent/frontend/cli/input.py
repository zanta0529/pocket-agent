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

from pocket_agent.frontend.cli.render import plain, toolbar
from pocket_agent.frontend.cli.state import CliState

BUILTINS = {
    "add-dir": "單次會話內，將外部資料夾加入工作範圍",
    "attachments": "查看／移除待送附件",
    "clear": "清除對話，保留附件",
    "compact": "摘要並壓縮中間對話歷史，回收 context 空間",
    "config": "顯示或更改全域設定檔 (/config set <key> <value>)",
    "file": "附加一般檔案",
    "help": "指令說明",
    "learn": "從近期對話萃取為共用技能",
    "logout": "登出",
    "model": "切換當次提供者與模型",
    "quit": "離開",
    "session": "查看／載入歷史對話",
    "skills": "列出目前所有可用的全域與專案技能",
    "subagent": "強制委派任務給指定的子代理",
    "tools": "工具清單",
    "undo": "時光機：還原上一次被 AI 修改的檔案",
    "workspace": "顯示目前的工作區路徑清單",
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
        self.page = 0
        self.last_text = ""

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
        if text != self.last_text:
            self.page = 0
            self.last_text = text

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
        from pocket_agent.extensions.skills import list_skills
        from pocket_agent.system.commands import list_commands

        commands = dict(BUILTINS)
        if self.state.provider == "openai":
            commands["image"] = "產生圖片（待送附件作參考圖）"
        for command in list_commands():
            commands.setdefault(command["name"], command["description"])

        # Add skills as slash commands
        for skill in list_skills(None):
            commands.setdefault(skill["name"], f"[Skill] {skill.get('description', '')}")

        # Add subagents as slash commands
        from pocket_agent.core.subagents import list_subagents

        for sa in list_subagents():
            commands.setdefault(sa["name"], f"[Subagent] {sa.get('description', '')}")

        matches = []
        for name, description in commands.items():
            if name.startswith(text[1:]):
                matches.append((name, description))

        PAGE_SIZE = 5
        total = len(matches)

        if total == 0:
            return

        start_idx = self.page * PAGE_SIZE
        if start_idx >= total:
            self.page = (total - 1) // PAGE_SIZE
            start_idx = self.page * PAGE_SIZE

        end_idx = start_idx + PAGE_SIZE
        page_items = matches[start_idx:end_idx]

        if self.page > 0:
            yield Completion(
                text[1:],
                start_position=-(len(text) - 1),
                display=f"  ↑ {start_idx} before",
                display_meta="",
                style="class:completion-menu.more",
            )

        for name, description in page_items:
            yield Completion(name, start_position=-(len(text) - 1), display_meta=plain(description))

        if end_idx < total:
            yield Completion(
                text[1:],
                start_position=-(len(text) - 1),
                display=f"  ↓ {total - end_idx} more",
                display_meta="",
                style="class:completion-menu.more",
            )


def key_bindings(
    selecting: Callable[[], bool],
    on_clear_screen: Callable[[], None] | None = None,
    completer: SlashCommandCompleter | None = None,
) -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("enter")
    def submit(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if completer and buffer.complete_state and buffer.complete_state.current_completion:
            c = buffer.complete_state.current_completion
            display_str = c.display_text
            if "more" in display_str or "before" in display_str:
                if display_str.lstrip().startswith("↓"):
                    completer.page += 1
                    buffer.complete_state = None
                    buffer.start_completion(select_first=True)
                    return
                elif display_str.lstrip().startswith("↑"):
                    completer.page = max(0, completer.page - 1)
                    buffer.complete_state = None
                    buffer.start_completion(select_first=True)
                    return
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

    @bindings.add("up", filter=Condition(selecting))
    def up_completer(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if not buffer.complete_state:
            buffer.start_completion(select_first=True)
            if buffer.complete_state:
                buffer.complete_previous()
        else:
            buffer.complete_previous()

    @bindings.add("down", filter=Condition(selecting))
    def down_completer(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if not buffer.complete_state:
            buffer.start_completion(select_first=True)
        else:
            buffer.complete_next()

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
            key_bindings=key_bindings(
                lambda: self.selecting, on_clear_screen=on_clear_screen, completer=self.completer
            ),
            erase_when_done=True,
            color_depth=ColorDepth.DEPTH_1_BIT if "NO_COLOR" in os.environ or not sys.stdout.isatty() else None,
        )

    async def read(self, message: str = "› ", default: str = "") -> str:
        return await self.session.prompt_async(
            message,
            default=default,
            completer=self.completer,
            multiline=True,
        )

    async def choose(self, label: str, choices: list[str]) -> str | None:
        from prompt_toolkit.completion import WordCompleter

        self.selecting = True

        def pre_run():
            # 自動展開光棒並選取第一項，不需使用者手動按方向鍵
            self.session.app.current_buffer.start_completion(select_first=True)

        try:
            return (
                await self.session.prompt_async(
                    label + "：",
                    completer=WordCompleter(choices, sentence=True),
                    default="",
                    multiline=False,
                    pre_run=pre_run,
                )
            ).strip()
        except (KeyboardInterrupt, EOFError):
            return None
        finally:
            self.selecting = False

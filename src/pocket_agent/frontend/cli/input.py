import os
import re
import shutil
import sys
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pocket_agent.core.agent import Agent

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.output import ColorDepth

from pocket_agent.frontend.cli.render import plain, toolbar
from pocket_agent.frontend.cli.state import CliState
from pocket_agent.system.constants import PROVIDER_OPENAI
from pocket_agent.system.i18n import t

BUILTIN_COMMAND_NAMES = (
    "activity",
    "add-dir",
    "agents",
    "attachments",
    "clean",
    "clear",
    "code-search",
    "compact",
    "config",
    "exit",
    "file",
    "goal",
    "help",
    "learn",
    "logout",
    "mcp",
    "model",
    "new",
    "parallel",
    "prune",
    "reasoning",
    "rename",
    "rm-dir",
    "rm-file",
    "session",
    "skills",
    "subagent",
    "undo",
    "usage",
    "workspace",
)


def get_builtin_commands() -> dict[str, str]:
    """依目前語系動態取得內建斜線指令與說明。"""
    return {name: t(f"commands.{name}") for name in BUILTIN_COMMAND_NAMES}


BUILTINS = {name: f"commands.{name}" for name in BUILTIN_COMMAND_NAMES}
QUOTE = chr(34)
REFERENCE = re.compile(r"(?:^|\s)@(?:" + QUOTE + "([^" + QUOTE + "]+)" + QUOTE + r"(?=\s|$)|([^\s" + QUOTE + "]+))")


def file_argument(value: str) -> str:
    value = value.strip()
    if value.startswith(QUOTE):
        if not value.endswith(QUOTE) or len(value) < 3:
            raise ValueError(t("cli.file_arg_unclosed"))
        value = value[1:-1]
    if not value or QUOTE in value:
        raise ValueError(t("cli.file_arg_usage"))
    return value


def references(text: str) -> list[str]:
    matches = list(REFERENCE.finditer(text))
    for start in re.finditer(r"(?:^|\s)@" + QUOTE, text):
        if not any(m.start() == start.start() for m in matches):
            raise ValueError(t("cli.at_ref_unclosed"))
    return [m.group(1) or m.group(2) for m in matches]


def is_image_command_supported(state: CliState | None, agent: object = None) -> bool:
    """查詢 Agent 能力旗標判斷是否支援 /image 指令；無 Agent 實例時後備參考 state。"""
    if agent is not None:
        flag = getattr(agent, "supports_image_command", None)
        if isinstance(flag, bool):
            return flag
        gen_flag = getattr(agent, "supports_image_generation", None)
        if isinstance(gen_flag, bool) and not gen_flag:
            return False
    if state is not None:
        return str(getattr(state, "provider", "")).lower() == PROVIDER_OPENAI
    return False


class SlashCommandCompleter(Completer):
    def __init__(self, state: CliState, agent: "Agent | None" = None) -> None:
        self.state = state
        self.agent = agent

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
                        display_meta=t("cli.completion_dir") if directory else t("cli.completion_file"),
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
        from pocket_agent.extensions.skills import list_skills
        from pocket_agent.system.commands import list_commands
        from pocket_agent.system.paths import is_portable

        commands = get_builtin_commands()
        if is_image_command_supported(self.state, self.agent):
            commands["image"] = t("commands.image")
        if is_portable():
            commands["update"] = t("commands.update")
        for command in list_commands():
            commands.setdefault(command["name"], command["description"])

        # Add skills as slash commands
        for skill in list_skills(None):
            commands.setdefault(skill["name"], f"[Skill] {skill.get('description', '')}")

        # Add subagents as slash commands
        from pocket_agent.core.subagents import list_subagents

        for sa in list_subagents():
            commands.setdefault(sa["name"], f"[Subagent] {sa.get('description', '')}")

        aliases = {
            "activity": "changes",
            "code-search": "cs",
            "exit": "quit",
            "mcp": "tools",
            "reasoning": "effort",
            "usage": "quota",
        }
        prefix = text[1:]
        matches = []
        for name, description in commands.items():
            alias = aliases.get(name)
            if name.startswith(prefix) or (alias and alias.startswith(prefix)):
                matches.append((name, description))

        # 提供完整候選，讓原生補齊選單隨光棒移動自動捲動。
        for name, description in matches:
            alias = aliases.get(name)
            display_name = f"{name} ({alias})" if alias else name
            yield Completion(name, start_position=-len(prefix), display=display_name, display_meta=plain(description))


def key_bindings(
    selecting: Callable[[], bool],
    state: CliState,
    agent: "Agent",
    on_clear_screen: Callable[[], None] | None = None,
    completer: SlashCommandCompleter | None = None,
) -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("escape", "v")
    def paste_attachments(event: KeyPressEvent) -> None:
        """Alt+V：從剪貼簿讀取檔案、記憶體截圖或路徑，作為附件加入。支援跨平台。"""
        from pathlib import Path

        from pocket_agent.frontend.cli.clipboard import get_clipboard_attachments

        valid_paths, is_screenshot = get_clipboard_attachments()
        if not valid_paths:
            return

        if is_screenshot:
            added = state.add_files(valid_paths)
            if added:
                from prompt_toolkit.application.run_in_terminal import run_in_terminal

                def notify_screenshot():
                    from rich.console import Console

                    console = Console()
                    for p in added:
                        console.print(f"✓ 已從剪貼簿擷取截圖並加入待送附件：[cyan]{Path(p).name}[/cyan]")

                run_in_terminal(notify_screenshot)
                event.app.invalidate()
            return

        unauthorized_paths = []
        authorized_paths = []
        for p in valid_paths:
            status, msg, target_dir = agent.check_new_workspace_dir(str(Path(p).parent))
            if status in ("in_default", "already_authorized"):
                authorized_paths.append(p)
            else:
                unauthorized_paths.append((p, target_dir))

        if unauthorized_paths:
            from prompt_toolkit.application.run_in_terminal import run_in_terminal

            def ask_workspace_auth():
                from rich.console import Console
                from rich.panel import Panel
                from rich.text import Text

                console = Console()
                content = t("cli.paste_outside_ws") + "\n"
                for p, _ in unauthorized_paths:
                    content += f"  📄 {p}\n"

                panel = Panel(
                    Text(content.strip(), style="yellow"),
                    title=t("misc_cmd.file_ws_warning_title"),
                    title_align="left",
                    border_style="bold red",
                    expand=False,
                )
                console.print()
                console.print(panel)

                try:
                    ans = input(t("cli.paste_ws_confirm")).strip().lower()
                    if ans == "y":
                        for p, t_dir in unauthorized_paths:
                            if t_dir:
                                agent.add_workspace_dir(t_dir)
                                authorized_paths.append(p)
                        console.print(f"[green]{t('misc_cmd.file_ws_authorized')}[/green]")
                    else:
                        console.print(f"[red]{t('cli.paste_ws_cancelled')}[/red]")
                except KeyboardInterrupt, EOFError:
                    console.print(f"\n[red]{t('cli.op_cancelled')}[/red]")

                # 在回呼函式內（取得授權後）才執行加入，避免非同步時序問題
                if authorized_paths:
                    try:
                        added = state.add_files(authorized_paths)
                        for added_path in added:
                            console.print(t("misc_cmd.file_added", path=added_path))
                    except Exception:
                        pass

            run_in_terminal(ask_workspace_auth)
            event.app.invalidate()
        elif authorized_paths:
            from prompt_toolkit.application.run_in_terminal import run_in_terminal

            def notify_added():
                from rich.console import Console

                console = Console()
                try:
                    added = state.add_files(authorized_paths)
                    for added_path in added:
                        console.print(t("misc_cmd.file_added", path=added_path))
                except Exception:
                    pass

            run_in_terminal(notify_added)
            event.app.invalidate()

    @bindings.add("enter")
    def submit(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if completer and buffer.complete_state:
            c = buffer.complete_state.current_completion
            # 如果選單已開啟但沒有反白項目，自動選取第一個
            if c is None and buffer.complete_state.completions:
                c = buffer.complete_state.completions[0]

            if c:
                # 正確套用補齊項目
                buffer.apply_completion(c)

                # 如果不是在選擇模式，則關閉選單且不送出對話
                if not selecting():
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
        if completer and buffer.complete_state:
            c = buffer.complete_state.current_completion
            if c is None and buffer.complete_state.completions:
                c = buffer.complete_state.completions[0]

            if c:
                buffer.apply_completion(c)

                # 自動補空白的邏輯
                # 若補齊的是目錄（以斜線結尾）或字串已有空白，則不補空白，方便繼續往下層輸入；
                # 若是檔案或斜線指令，則自動補空白。
                # 注意：若目前是在選擇模式 (selecting())，則不自動補空白，以免影響選項比對。
                text_before = buffer.document.text_before_cursor
                if not selecting():
                    if (
                        not text_before.endswith(os.sep)
                        and not text_before.endswith("/")
                        and not text_before.endswith(" ")
                    ):
                        buffer.insert_text(" ")
                return
        else:
            buffer.start_completion(select_first=True)

    @bindings.add("s-tab", filter=Condition(lambda: not selecting()))
    def toggle_accept_edits(event: KeyPressEvent) -> None:
        """Shift+Tab：切換 Accept-edits mode（當次會話自動核准檔案新增與編輯）。"""
        new_mode = False
        if state is not None:
            new_mode = not getattr(state, "accept_edits", False)
            state.accept_edits = new_mode
        if agent is not None:
            agent.accept_edits = new_mode
            for sub in getattr(agent, "active_subagents", []):
                sub.accept_edits = new_mode
        event.app.invalidate()

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
    def __init__(self, state: CliState, agent: "Agent", on_clear_screen: Callable[[], None] | None = None) -> None:
        self.selecting = False
        self.completer = SlashCommandCompleter(state, agent=agent)
        self.session = PromptSession(
            multiline=True,
            completer=self.completer,
            complete_while_typing=True,
            bottom_toolbar=lambda: toolbar(state, shutil.get_terminal_size().columns),
            key_bindings=key_bindings(
                lambda: self.selecting,
                state=state,
                agent=agent,
                on_clear_screen=on_clear_screen,
                completer=self.completer,
            ),
            erase_when_done=True,
            color_depth=ColorDepth.DEPTH_1_BIT if "NO_COLOR" in os.environ or not sys.stdout.isatty() else None,
        )

    async def read(self, message: str = "› ", default: str = "", is_password: bool = False) -> str:
        return await self.session.prompt_async(
            message,
            default=default,
            completer=self.completer if not is_password else None,
            multiline=not is_password,
            is_password=is_password,
        )

    async def choose(self, label: str, choices: list[str]) -> str | None:
        from prompt_toolkit.completion import FuzzyCompleter, WordCompleter

        self.selecting = True

        def pre_run():
            # 自動展開光棒並選取第一項，不需使用者手動按方向鍵
            self.session.app.current_buffer.start_completion(select_first=True)

        try:
            return (
                await self.session.prompt_async(
                    label + "：",
                    completer=FuzzyCompleter(
                        WordCompleter(choices, sentence=True, match_middle=True, ignore_case=True)
                    ),
                    default="",
                    multiline=False,
                    pre_run=pre_run,
                )
            ).strip()
        except KeyboardInterrupt:
            return None
        except EOFError:
            return None
        finally:
            self.selecting = False

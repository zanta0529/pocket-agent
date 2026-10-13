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

BUILTINS = {
    "add-dir": "單次會話內，將外部資料夾加入工作範圍",
    "agents": "列出目前所有可用的子代理清單與能力",
    "attachments": "查看／移除待送附件",
    "clean": "手動清理過期日誌與暫存檔 (Housekeeping)",
    "clear": "清除對話，保留附件",
    "codesearch": "搜尋程式碼片段並加入對話上下文",
    "compact": "摘要並壓縮中間對話歷史，回收 context 空間",
    "config": "顯示或更改全域設定檔 (/config set <key> <value>)",
    "exit": "離開",
    "file": "附加一般檔案",
    "goal": "設定長期目標，Agent 會持續嘗試完成任務不輕易中斷",
    "help": "指令說明",
    "learn": "從近期對話萃取為共用技能",
    "logout": "登出",
    "model": "切換當次提供者與模型",
    "parallel": "手動平行啟動多個子代理 (用法: /parallel <name>:<task> | <name>:<task>)",
    "rm-dir": "單次會話內，將外部資料夾從工作範圍移除",
    "rm-file": "從待送區移除附件檔案",
    "session": "查看／載入歷史對話",
    "skills": "列出目前所有可用的全域與專案技能",
    "subagent": "強制委派任務給指定的子代理",
    "tools": "工具清單",
    "undo": "時光機：還原上一次被 AI 修改的檔案",
    "usage": "顯示當前對話累計 Token 用量",
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
            elif name == "codesearch" and "cs".startswith(text[1:]):
                matches.append((name, description))
            elif name == "exit" and "quit".startswith(text[1:]):
                matches.append((name, description))
            elif name == "usage" and "quota".startswith(text[1:]):
                matches.append((name, description))

        PAGE_SIZE = 10
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
            display_name = name
            if name == "codesearch":
                display_name = "codesearch (cs)"
            elif name == "exit":
                display_name = "exit (quit)"
            elif name == "usage":
                display_name = "usage (quota)"
            yield Completion(
                name, start_position=-(len(text) - 1), display=display_name, display_meta=plain(description)
            )

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
    state: CliState,
    agent: "Agent",
    on_clear_screen: Callable[[], None] | None = None,
    completer: SlashCommandCompleter | None = None,
) -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("escape", "v")
    def paste_attachments(event: KeyPressEvent) -> None:
        """Alt+V：從剪貼簿讀取路徑或檔案，作為附件加入。支援跨平台。"""
        import platform
        import subprocess
        import urllib.parse
        from pathlib import Path

        text_data = ""
        sys_name = platform.system()

        # 1. 根據不同作業系統提取剪貼簿內容
        if sys_name == "Windows":
            try:
                import win32clipboard

                win32clipboard.OpenClipboard()
                try:
                    # 優先檢查是否為實體檔案 (從檔案總管複製, CF_HDROP = 15)
                    if win32clipboard.IsClipboardFormatAvailable(15):
                        paths = win32clipboard.GetClipboardData(15)
                        text_data = "\n".join(paths)
                    # 檢查純文字
                    elif win32clipboard.IsClipboardFormatAvailable(13):
                        text_data = win32clipboard.GetClipboardData(13)
                finally:
                    win32clipboard.CloseClipboard()
            except Exception:
                pass
        elif sys_name == "Darwin":
            try:
                text_data = subprocess.run(["pbpaste"], capture_output=True, text=True, check=True).stdout
            except Exception:
                pass
        elif sys_name == "Linux":
            # 依序嘗試 Wayland 與 X11 的剪貼簿工具
            for cmd in [["wl-paste"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]]:
                try:
                    text_data = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
                    if text_data:
                        break
                except Exception:
                    continue

        # 2. 解析並驗證路徑
        if text_data:
            valid_paths = []
            for line in text_data.splitlines():
                clean_line = line.strip(" \t\r\n\"'")

                # 處理 Mac/Linux 檔案總管常產生的 file:// URI
                if clean_line.startswith("file://"):
                    # 移除 file:// 首綴，並解碼 URL 編碼 (如 %20 轉空白)
                    clean_line = urllib.parse.unquote(clean_line[7:])

                if not clean_line:
                    continue

                try:
                    pth = Path(clean_line).expanduser().resolve()
                    if pth.is_file():
                        valid_paths.append(str(pth))
                except Exception:
                    pass

            # 3. 匯入狀態並重繪 UI
            if valid_paths:
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
                        content = "以下貼上的檔案不在目前工作區內：\n"
                        for p, _ in unauthorized_paths:
                            content += f"  📄 {p}\n"

                        panel = Panel(
                            Text(content.strip(), style="yellow"),
                            title="【安全警告】工作區權限請求",
                            title_align="left",
                            border_style="bold red",
                            expand=False,
                        )
                        console.print()
                        console.print(panel)

                        try:
                            ans = input("是否將它們所在的資料夾加入工作區並掛載？[y/N] ").strip().lower()
                            if ans == "y":
                                for p, t_dir in unauthorized_paths:
                                    if t_dir:
                                        agent.add_workspace_dir(t_dir)
                                        authorized_paths.append(p)
                                console.print("[green]✓ 已授權並加入附件。[/green]")
                            else:
                                console.print("[red]✗ 已取消加入這些檔案。[/red]")
                        except KeyboardInterrupt:
                            console.print("\n[red]✗ 已取消操作。[/red]")
                        except EOFError:
                            console.print("\n[red]✗ 已取消操作。[/red]")

                        # 在回呼函式內（取得授權後）才執行加入，避免非同步時序問題
                        if authorized_paths:
                            try:
                                added = state.add_files(authorized_paths)
                                for added_path in added:
                                    console.print(f"已加入待送：{added_path}")
                            except Exception:
                                pass

                    run_in_terminal(ask_workspace_auth)
                else:
                    if authorized_paths:
                        from prompt_toolkit.application.run_in_terminal import run_in_terminal

                        def notify_added():
                            from rich.console import Console

                            console = Console()
                            try:
                                added = state.add_files(authorized_paths)
                                for added_path in added:
                                    console.print(f"已加入待送：{added_path}")
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
        self.completer = SlashCommandCompleter(state)
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

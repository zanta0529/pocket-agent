"""Safe, scrollback-friendly CLI presentation."""

import json
from pathlib import Path

from prompt_toolkit.formatted_text import FormattedText
from rich.cells import cell_len
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from pocket_agent.frontend.cli.state import CliState


class MultiStatus:
    """支援多個子代理平行執行的動態表格 Spinner。"""

    def __init__(self, title, task_names, console):
        from rich.live import Live

        self.title = title
        self.console = console
        self.task_names = task_names

        self._states = {name: "啟動中..." for name in task_names}
        self._live = Live(self._build_table(), console=console, refresh_per_second=10)
        self.is_running = False

    def _build_table(self):
        from rich.console import Group
        from rich.spinner import Spinner
        from rich.table import Table
        from rich.text import Text

        table = Table(show_header=False, show_edge=False, box=None)
        table.add_column("Spinner", width=3)
        table.add_column("Name", style="bold cyan", width=15)
        table.add_column("Status")

        for name in self.task_names:
            state = self._states[name]

            if "完成" in state or "✓" in state:
                icon = Text("✓", style="bold green")
            elif "錯誤" in state or "失敗" in state or "✗" in state:
                icon = Text("✗", style="bold red")
            else:
                icon = Spinner("dots")

            table.add_row(icon, Text(f"[{name}]", style="bold cyan"), state)

        return Group(Text(self.title, style="default"), table)

    def start(self):
        self._live.start()
        self.is_running = True

    def stop(self):
        if self.is_running:
            self._live.stop()
            self.is_running = False

    def update(self, status: str, **kwargs):
        import re

        match = re.match(r"^\[(.*?)\]\s+(.*)", status)
        if match:
            name, real_status = match.groups()
            if name in self._states:
                self._states[name] = real_status[:80]
        if self.is_running:
            self._live.update(self._build_table())


def plain(value: object) -> str:
    return "".join(c if c.isprintable() else " " for c in str(value))


def clip(value: str, width: int) -> str:
    if width <= 0:
        return ""
    text = Text(plain(value))
    text.truncate(width, overflow="ellipsis")
    return text.plain


def toolbar(state: CliState, width: int) -> FormattedText:
    width = max(0, width)

    if not state.pending_files:
        count = "待送 0"
    else:
        names = [Path(p).name for p in state.pending_files]
        count = f"待送 {len(names)} ({', '.join(names)})"

    if state.session_tokens > 0:
        max_tokens = 128000
        ratio = min(1.0, state.session_tokens / max_tokens)
        filled = int(ratio * 10)
        bar = "█" * filled + "░" * (10 - filled)
        tokens_str = f" · {state.session_tokens} tokens [{bar}]"
    else:
        tokens_str = ""

    model = f"[ {state.provider} ] {state.model}"
    budget = max(0, width - cell_len(count) - cell_len(tokens_str) - 3)
    line = f"{clip(model, budget)} · {count}{tokens_str}" if budget else f"{count}{tokens_str}"

    for extra in [" · / 指令  @ 檔案", " · Enter 送出  Esc+Enter 換行", f" · {Path.cwd().name}"]:
        if cell_len(plain(line + extra)) <= width:
            line += extra
    return FormattedText([("", clip(line, width))])


class CliRenderer:
    def __init__(self, console: Console):
        self.console = console
        # 串流狀態旗標：追蹤本輪是否已開始串流輸出
        self._stream_header_printed: bool = False
        self._stream_has_content: bool = False
        # 思考中 spinner：由 send_turn 啟動，由 event("text") 或 send_turn 結束前停止
        self._active_status = None
        # 當前 CLI 狀態引用（啟動後由 cli.py 設定），用於標頭顯示 model 名稱
        self._state: CliState | None = None
        self._thinking_buffer: str = ""

    def _stop_status(self) -> None:
        """停止思考中 spinner（若仍在執行），確保不與串流輸出衝突。"""
        if self._active_status is not None:
            try:
                self._active_status.stop()
            except Exception:
                pass
            self._active_status = None

    def _ensure_newline(self) -> None:
        """若文字串流游標未換行，補一個換行確保後續輸出從行首開始。

        text 事件以 end="" 輸出，游標停在行尾；tool_start、tool_done、
        read 等事件若緊接其後，不補換行會造成輸出黏在回應文字後方。
        """
        if self._stream_has_content:
            self.console.print()
            self._stream_has_content = False

    def _summarise_args(self, name: str, args_raw: str) -> str:
        """從工具的 JSON arguments 字串萃取最有意義的單一摘要欄位。

        依工具名稱特徵選擇最具代表性的欄位（如 run_command 取 command、
        read_file 取 file_path），讓使用者不必解讀完整 JSON 即可判斷意圖。
        JSON 解析失敗時回退到純字串截短，確保不 crash。
        """
        MAX = 70
        name_lower = name.lower()

        parsed: dict | None = None
        if args_raw:
            try:
                parsed = json.loads(args_raw)
            except json.JSONDecodeError:
                parsed = None
            except ValueError:
                parsed = None

        if parsed and isinstance(parsed, dict):
            # 依工具名稱萃取最具代表性的欄位
            if any(k in name_lower for k in ("run_command", "bash", "shell", "python", "powershell", "node", "cmd")):
                val = parsed.get("command") or parsed.get("cmd") or ""
            elif any(k in name_lower for k in ("replace", "edit", "patch", "write", "create")):
                val = parsed.get("target_file") or parsed.get("file_path") or parsed.get("path") or ""
            elif any(k in name_lower for k in ("read", "view", "list", "find", "grep", "search")):
                val = (
                    parsed.get("file_path")
                    or parsed.get("path")
                    or parsed.get("query")
                    or parsed.get("pattern")
                    or parsed.get("directory")
                    or ""
                )
            elif "subagent" in name_lower:
                if "tasks" in parsed and isinstance(parsed["tasks"], list) and parsed["tasks"]:
                    val = parsed["tasks"][0].get("task", "")
                else:
                    val = parsed.get("task", "")
            else:
                # 通用 fallback：取第一個非空的 value
                val = next((str(v) for v in parsed.values() if v), "")
            if val:
                return clip(str(val), MAX)

        # JSON 解析失敗或無可用欄位：回退截短原始字串
        return clip(args_raw, MAX) if args_raw else ""

    def message(self, value: object, style: str = "dim") -> None:
        self.console.print(Text(plain(value), style=style))

    def banner(self) -> None:
        width = self.console.width
        if width < 40:
            self.message("Pocket Agent", "bold #B8860B")
        else:
            # Pocket Agent 標誌與口號
            self.message("─" * min(width, 80), "")
            self.console.print(
                Text.assemble(
                    ("╭──────╮\n", ""),
                    ("│ >_   │  ", ""),
                    ("Pocket Agent\n", "bold #B8860B"),
                    ("│      │  ", ""),
                    ("你的終端 AI 工作夥伴\n", ""),
                    ("╰───╲──╯", ""),
                )
            )
        self.message(Path.cwd(), "")
        self.message("─" * min(width, 80), "")

    def welcome(self, state: CliState, tools: list[str]) -> None:
        self.message(f"[ {state.provider} ] {state.model} · {len(tools)} 個 MCP 工具 · 就緒")
        self.message("/ 指令 · @ 檔案 · Tab 補全 · Enter 送出 · Esc 後 Enter 換行")

    def user(self, raw: str) -> None:
        self.console.print()
        # Text keeps user markup literal while retaining intentional newlines.
        self.console.print(Text("› " + "\n".join(plain(line) for line in raw.split("\n")), style="bold blue"))

    def reply(self, reply: str, seconds: float, tokens: int, streamed: bool = False) -> None:
        if not streamed or (streamed and not self._stream_has_content and reply):
            # 非串流模式（fallback）或沒有產生任何文字（例如純後補產出）：印標頭 + 完整 Markdown 回答
            if self._stream_has_content:
                self.console.print()
            self.console.print()
            self.message("● Pocket Agent", "bold #B8860B")
            self.console.print(Markdown(reply))
        else:
            # 串流模式且有印出文字：內容已逐字印出，確保換行後只印 footer
            if self._stream_has_content:
                self.console.print()  # 確保 footer 從新行開始

        # 重置串流狀態
        self._stream_header_printed = False
        self._stream_has_content = False
        self._thinking_buffer = ""
        self.message(f"本輪已返回 · {seconds:.1f} 秒 · {tokens} tokens")

    def event(self, kind: str, data: object) -> None:
        if kind == "system":
            self.message(data)

        elif kind == "error_panel":
            self._ensure_newline()
            self._stop_status()

            from rich.panel import Panel

            title = data.get("title", "錯誤") if isinstance(data, dict) else "錯誤"
            text = data.get("text", str(data)) if isinstance(data, dict) else str(data)

            panel = Panel(
                Text(text, style="white"),
                title=f"⚠️ {title}",
                title_align="left",
                border_style="bold red",
                expand=False,
            )
            self.console.print()
            self.console.print(panel)
            self.console.print()

        elif kind in ("image_saved", "file_saved"):
            self._ensure_newline()
            self.message(f"已儲存檔案：{data}", "green")

        elif kind == "round_start":
            # 工具呼叫後重跑：若已串流了文字，換行確認，再開始新輪
            if self._stream_has_content:
                self.console.print()
            self._stream_header_printed = False
            self._stream_has_content = False
            self._thinking_buffer = ""

        elif kind == "text":
            if not self._stream_header_printed:
                # 第一個 token 到來：先停止思考中 spinner，再印標頭
                self._stop_status()
                self.console.print()  # 與上一條訊息間隔
                label = f"● Pocket Agent ({self._state.model})" if self._state is not None else "● Pocket Agent"
                self.console.print(Text(label, style="bold #B8860B"))
                self._stream_header_printed = True
            self.console.print(str(data), end="", highlight=False, markup=False)
            self._stream_has_content = True

        elif kind == "thinking":
            self._thinking_buffer += str(data)
            if self._active_status:
                snippet = self._thinking_buffer.replace("\n", " ").strip()
                if len(snippet) > 60:
                    snippet = "..." + snippet[-57:]
                self._active_status.update(f"思考中… {snippet}")

        elif kind == "read":
            self._ensure_newline()
            self.console.print(Text.assemble(("● ", "bold green"), ("Read", "bold yellow"), (f" ({plain(data)})", "")))

        elif kind == "tool_stream_start":
            self._ensure_newline()
            msg = f"準備執行 {data} 工具，正在產生參數..."
            if self._active_status:
                self._active_status.update(msg)
            else:
                from rich.status import Status

                status = Status(msg, spinner="dots", console=self.console)
                self._active_status = status
                status.start()

        elif kind == "tool_start":
            self._stop_status()
            self._ensure_newline()
            self._thinking_buffer = ""

            if isinstance(data, dict):
                name = data.get("name", str(data))
                args_raw = data.get("arguments", "") or ""
                is_safe_read = data.get("is_safe_read", False)
            else:
                name = str(data)
                args_raw = ""
                is_safe_read = False

            real_name = name.split("::")[-1]
            name_lower = real_name.lower()

            if any(kw in name_lower for kw in ["read", "list", "search", "view", "find", "grep"]):
                action = "Read"
            elif any(kw in name_lower for kw in ["write", "replace", "edit", "patch", "delete", "create"]):
                action = "Write"
            else:
                action = "Action"

            action_color = "bold yellow"
            name_color = "bold green" if is_safe_read else "bold cyan"
            bullet_color = "bold red" if action == "Write" or not is_safe_read else "bold green"
            summary = self._summarise_args(name, args_raw)

            if "::" in name:
                sa_name, _ = name.split("::", 1)
                parts = [
                    ("● ", bullet_color),
                    (action, action_color),
                    (f" [{sa_name}]", "bold magenta"),
                    (" (", ""),
                    (real_name, name_color),
                    (")", ""),
                ]
            else:
                parts = [("● ", bullet_color), (action, action_color), (" (", ""), (real_name, name_color), (")", "")]

            if summary:
                summary_text = plain(summary)
                if summary_text.startswith("【安全警告】"):
                    parts.append((" 【安全警告】", "bold red"))
                    parts.append((summary_text[6:], ""))
                else:
                    parts.append((f" {summary_text}", ""))

            # 參照 Antigravity 設計：將工具呼叫明確印出並常駐在畫面上，不再自動隱藏
            tool_text = Text.assemble(*parts)
            self.console.print(tool_text)

            if self.console.is_terminal:
                self._stop_status()
                from rich.status import Status

                self._active_status = Status("執行工具中…", spinner="dots", console=self.console)
                self._active_status.start()

        elif kind == "ask_question":
            self._ensure_newline()
            self._stop_status()

            questions = data.get("questions", [])
            callback = data.get("callback")

            def _ask_worker():
                try:
                    from rich.panel import Panel

                    answers = []
                    for q in questions:
                        text = q.get("question", "")
                        options = q.get("options", [])
                        is_multi = q.get("is_multi_select", False)

                        self.console.print()
                        self.console.print(
                            Panel(Text(text), title="🤔 代理提問", title_align="left", border_style="bold blue")
                        )
                        for i, opt in enumerate(options):
                            self.console.print(f"  [{i + 1}] {opt}")

                        hint = "請輸入選項編號，或自由作答"
                        if is_multi:
                            hint += " (多選可用逗號分隔)"

                        from prompt_toolkit import prompt

                        ans = prompt(f"👉 {hint}: ")

                        # 若輸入為編號，嘗試轉換為對應選項文字，讓模型更好理解
                        if ans.isdigit() and 1 <= int(ans) <= len(options):
                            ans = options[int(ans) - 1]

                        answers.append(ans)
                        self.console.print()

                    if callback:
                        callback({"answers": answers})
                except BaseException as e:
                    if callback:
                        callback({"error": str(e) or "User cancelled"})

            import threading

            threading.Thread(target=_ask_worker, daemon=True).start()

        elif kind == "tool_done":
            self._stop_status()

            # 恢復思考中狀態
            from rich.status import Status

            self._active_status = Status("思考中…", spinner="dots", console=self.console)
            self._active_status.start()

            if isinstance(data, dict):
                name = data.get("name", "")
                ok = data.get("ok", True)
                if not ok:
                    if "::" in name:
                        sa_name, real_name = name.split("::", 1)
                        formatted_name = f"[{sa_name}] {real_name}"
                    else:
                        formatted_name = name

                    result_text = str(data.get("result", ""))

                    from rich.panel import Panel
                    from rich.syntax import Syntax

                    # 嘗試以 Syntax 高亮，若為 JSON 格式
                    is_json = False
                    if result_text.strip().startswith("{") or result_text.strip().startswith("["):
                        try:
                            import json

                            json.loads(result_text)
                            is_json = True
                        except Exception:
                            pass

                    if is_json:
                        content = Syntax(
                            result_text, "json", theme="monokai", background_color="default", word_wrap=True
                        )
                    else:
                        content = Text(result_text, style="red")

                    self.console.print()
                    self.console.print(
                        Panel(
                            content,
                            title=f"✗ 工具執行失敗：{formatted_name}",
                            title_align="left",
                            border_style="bold red",
                            expand=False,
                        )
                    )

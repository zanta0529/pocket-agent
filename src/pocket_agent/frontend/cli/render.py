"""Safe, scrollback-friendly CLI presentation."""

import json
from pathlib import Path
from time import monotonic

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


def format_display_path(path_str: str, base_dir: Path | None = None) -> str:
    """將檔案路徑轉為相對於工作目錄的簡短路徑，無法相對化時安全保留原路徑。"""
    if not path_str or not isinstance(path_str, str):
        return ""
    raw = Path(path_str.strip())
    base = (base_dir or Path.cwd()).resolve()
    try:
        if raw.is_absolute():
            rel = raw.resolve().relative_to(base)
            return rel.as_posix()
    except ValueError, RuntimeError:
        pass

    try:
        home = Path.home().resolve()
        if raw.is_absolute():
            rel_home = raw.resolve().relative_to(home)
            return f"~/{rel_home.as_posix()}"
    except ValueError, RuntimeError:
        pass

    return raw.as_posix() if hasattr(raw, "as_posix") else str(path_str)


def middle_truncate(value: str, max_width: int) -> str:
    """中段智慧截斷：保留開頭與結尾關鍵資訊，中間以 ... 省略，保證末端檔名/副檔名不遺失。"""
    if max_width <= 0:
        return ""
    s = plain(value).strip()
    if cell_len(s) <= max_width:
        return s
    if max_width <= 5:
        return clip(s, max_width)

    target_len = max_width - 3
    # 路徑或指令的尾端通常更重要（如檔名、指令參數），預留 40% 給開頭，60% 給結尾
    head_budget = max(1, int(target_len * 0.4))
    tail_budget = max(1, target_len - head_budget)

    head_chars: list[str] = []
    cur = 0
    for ch in s:
        w = cell_len(ch)
        if cur + w > head_budget:
            break
        head_chars.append(ch)
        cur += w

    tail_chars: list[str] = []
    cur = 0
    for ch in reversed(s):
        w = cell_len(ch)
        if cur + w > tail_budget:
            break
        tail_chars.append(ch)
        cur += w
    tail_chars.reverse()

    return "".join(head_chars) + "..." + "".join(tail_chars)


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
        count = f"待送 {len(state.pending_files)}"

    if state.session_tokens > 0:
        max_tokens = max(1, getattr(state, "context_window", 128000) or 128000)
        ratio = min(1.0, state.session_tokens / max_tokens)
        filled = int(ratio * 10)
        bar = "█" * filled + "░" * (10 - filled)
        tokens_str = f" · {state.session_tokens} tokens [{bar}]"
    else:
        tokens_str = ""

    if (
        getattr(state, "quota_supported", False)
        and getattr(state, "quota_limit", None) is not None
        and getattr(state, "quota_remaining", None) is not None
    ):
        used = state.quota_limit - state.quota_remaining
        tokens_str += f" · 額度 ${used:.3f}/${state.quota_limit:.3f}"

    accept_status = "ON" if getattr(state, "accept_edits", False) else "OFF"
    reasoning_val = str(getattr(state, "reasoning_effort", "auto") or "auto").strip().capitalize()
    tokens_str += f" · 推理: {reasoning_val} · Accept-edits: {accept_status}"

    if getattr(state, "staged_update_version", None):
        tokens_str += f" · ✨ 新版 ({state.staged_update_version}) 就緒"

    model = f"[ {state.provider} ] {state.model}"
    budget = max(0, width - cell_len(count) - cell_len(tokens_str) - 3)
    line = f"{clip(model, budget)} · {count}{tokens_str}" if budget else f"{count}{tokens_str}"

    for extra in [
        " · / 指令  @ 檔案",
        " · Shift+Tab 自動編輯",
        " · Enter 送出  Esc+Enter 換行",
        f" · {Path.cwd().name}",
    ]:
        if cell_len(plain(line + extra)) <= width:
            line += extra
    return FormattedText([("", clip(line, width))])


class CliRenderer:
    def __init__(self, console: Console):
        self.console = console
        # 串流狀態旗標：追蹤本輪是否已開始串流輸出
        self._stream_header_printed: bool = False
        self._round_text_printed: bool = False
        self._round_is_dim: bool = False
        self._stream_has_content: bool = False
        # 思考中 spinner：由 send_turn 啟動，由 event("text") 或 send_turn 結束前停止
        self._active_status = None
        # 當前 CLI 狀態引用（啟動後由 cli.py 設定），用於標頭顯示 model 名稱
        self._state: CliState | None = None
        self._thinking_buffer: str = ""
        self._pending_tools: dict[str, dict] = {}
        self._text_buffer: list[str] = []
        self._text_buffer_len: int = 0
        self._last_text_flush: float = 0.0
        self._last_thinking_update: float = 0.0

    def _stop_status(self) -> None:
        """停止思考中 spinner（若仍在執行），確保不與串流輸出衝突。"""
        if self._active_status is not None:
            try:
                self._active_status.stop()
            except Exception:
                pass
            self._active_status = None
        # 強制確保終端機光標恢復顯示，避免 Live / Status 異常中斷時隱藏光標殘留
        try:
            self.console.show_cursor(True)
        except Exception:
            pass

    def _flush_text_buffer(self) -> None:
        """將累積的串流文字微批次輸出至終端機，降低逐字重繪背壓。"""
        if not self._text_buffer:
            return
        text_str = "".join(self._text_buffer)
        self._text_buffer.clear()
        self._text_buffer_len = 0
        self._last_text_flush = monotonic()
        if self._round_is_dim:
            if "\n" in text_str:
                parts = text_str.split("\n", 1)
                if parts[0]:
                    self.console.print(Text(parts[0], style="dim"), end="", highlight=False)
                self.console.print("\n", end="", highlight=False, markup=False)
                if parts[1]:
                    self.console.print(parts[1], end="", highlight=False, markup=False)
                self._round_is_dim = False
            else:
                self.console.print(Text(text_str, style="dim"), end="", highlight=False)
        else:
            self.console.print(text_str, end="", highlight=False, markup=False)
        self._stream_has_content = True

    def _ensure_newline(self) -> None:
        """若文字串流游標未換行，補一個換行確保後續輸出從行首開始。

        text 事件以 end="" 輸出，游標停在行尾；tool_start、tool_done、
        read 等事件若緊接其後，不補換行會造成輸出黏在回應文字後方。
        """
        self._flush_text_buffer()
        if self._stream_has_content:
            self.console.print()
            self._stream_has_content = False

    def _extract_tool_target(self, name: str, args_raw: str, max_width: int | None = None) -> str:
        """從工具的 JSON arguments 字串萃取核心目標，並進行路徑相對化與中段智慧截斷。"""
        if max_width is None:
            term_w = getattr(self.console, "width", 80) or 80
            max_width = max(70, min(140, term_w - 10))

        name_lower = name.lower()
        parsed: dict | None = None
        if args_raw:
            try:
                parsed = json.loads(args_raw)
            except json.JSONDecodeError, ValueError, TypeError:
                parsed = None

        val = ""
        is_path = False

        if parsed and isinstance(parsed, dict):
            if parsed.get("owner") and parsed.get("repo"):
                issue_no = parsed.get("issue_number")
                if issue_no is not None and str(issue_no).strip() != "":
                    val = f"{parsed['owner']}/{parsed['repo']}#{issue_no}"
                else:
                    val = f"{parsed['owner']}/{parsed['repo']}"
            elif any(k in name_lower for k in ("run_command", "bash", "shell", "python", "powershell", "node", "cmd")):
                val = parsed.get("command") or parsed.get("cmd") or ""
                val = " ".join(val.splitlines()).strip()
            elif any(k in name_lower for k in ("replace", "edit", "patch", "write", "create")):
                val = parsed.get("target_file") or parsed.get("file_path") or parsed.get("path") or ""
                is_path = True
            elif any(k in name_lower for k in ("read", "view", "list", "find", "grep", "search")):
                path_val = parsed.get("file_path") or parsed.get("path") or parsed.get("directory") or ""
                if path_val:
                    val = path_val
                    is_path = True
                    start_l = parsed.get("start_line") or parsed.get("offset")
                    end_l = parsed.get("end_line") or parsed.get("limit")
                    if start_l is not None and end_l is not None:
                        val = f"{val}#L{start_l}-L{end_l}"
                    elif start_l is not None:
                        val = f"{val}#L{start_l}"
                else:
                    val = parsed.get("query") or parsed.get("pattern") or ""
            elif any(k in name_lower for k in ("web", "fetch", "curl", "url", "browser")):
                val = parsed.get("url") or parsed.get("endpoint") or ""
            elif "subagent" in name_lower:
                if "tasks" in parsed and isinstance(parsed["tasks"], list) and parsed["tasks"]:
                    val = parsed["tasks"][0].get("task", "")
                else:
                    val = parsed.get("task", "")
            else:
                val = next((str(v) for v in parsed.values() if v), "")

        if not val and args_raw:
            val = args_raw

        if not val:
            return ""

        if is_path:
            if "#" in val:
                p_part, line_part = val.split("#", 1)
                val = f"{format_display_path(p_part)}#{line_part}"
            else:
                val = format_display_path(val)

        return middle_truncate(val, max_width)

    def _summarise_args(self, name: str, args_raw: str) -> str:
        """向後相容：從工具的 JSON arguments 字串萃取最有意義的單一摘要欄位。"""
        return self._extract_tool_target(name, args_raw, max_width=70)

    def _format_tool_lines(self, prefix: tuple[str, str], info: dict) -> list[Text]:
        """格式化工具行：未超過終端寬度時單行接續，超過時改為樹狀結構兩行。"""
        parts = [prefix, (info["action"], info["action_color"])]
        if info.get("sa_name"):
            parts.append((f" [{info['sa_name']}]", "bold magenta"))
        if info.get("server_tag"):
            parts.append((f" [{info['server_tag']}]", "bold cyan"))
        parts.extend([(" (", ""), (info["real_name"], info["name_color"]), (")", "")])

        target_text = info.get("target") or info.get("summary") or ""
        if not target_text:
            return [Text.assemble(*parts)]

        term_w = getattr(self.console, "width", 80) or 80
        header_text = Text.assemble(*parts)
        header_w = cell_len(header_text.plain)
        target_w = cell_len(target_text)

        # 預留 1 格空白與 1 格終端右側緩衝
        if header_w + 1 + target_w <= max(20, term_w - 1):
            if target_text.startswith("【安全警告】"):
                parts.extend([(" ", ""), ("【安全警告】", "bold red"), (f" {target_text[6:]}", "dim")])
            else:
                parts.extend([(" ", ""), (target_text, "dim")])
            return [Text.assemble(*parts)]

        # 超過寬度時降級為樹狀結構兩行
        if target_text.startswith("【安全警告】"):
            sub_parts = [
                ("  └─ ", "dim"),
                ("【安全警告】", "bold red"),
                (f" {target_text[6:]}", "dim"),
            ]
        else:
            sub_parts = [("  └─ ", "dim"), (target_text, "dim")]
        return [header_text, Text.assemble(*sub_parts)]

    def message(self, value: object, style: str = "dim") -> None:
        self.console.print(Text(plain(value), style=style))

    def banner(self) -> None:
        from pocket_agent.system.paths import is_portable

        title_text = "Pocket Agent"
        if is_portable():
            from pocket_agent.system.updater import get_current_build_info

            info = get_current_build_info()
            if info.commit:
                title_text = f"Pocket Agent ({info.commit[:7]})"
            elif info.version:
                title_text = f"Pocket Agent {info.version}"

        width = self.console.width
        if width < 40:
            self.message(title_text, "bold #B8860B")
        else:
            # Pocket Agent 標誌與口號
            self.message("─" * min(width, 80), "")
            self.console.print(
                Text.assemble(
                    ("╭──────╮\n", ""),
                    ("│ >_   │  ", ""),
                    (f"{title_text}\n", "bold #B8860B"),
                    ("│      │  ", ""),
                    ("你的終端 AI 工作夥伴\n", ""),
                    ("╰───╲──╯", ""),
                )
            )
        self.message(Path.cwd(), "")
        self.message("─" * min(width, 80), "")

    def welcome(self, state: CliState, tools: list[str]) -> None:
        self.message(f"[ {state.provider} ] {state.model} · {len(tools)} 個 MCP 工具 · 就緒")
        self.message("/ 指令 · @ 檔案 · Tab 補全 · Shift+Tab 切換自動編輯 · Enter 送出 · Esc 後 Enter 換行")

    def user(self, raw: str) -> None:
        self.console.print()
        # Text keeps user markup literal while retaining intentional newlines.
        self.console.print(Text("› " + "\n".join(plain(line) for line in raw.split("\n")), style="bold blue"))

    def reply(self, reply: str, seconds: float, tokens: int, streamed: bool = False) -> None:
        self._flush_text_buffer()
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
            self._flush_text_buffer()
            # 工具呼叫後重跑：若已串流了文字，換行確認，再開始新輪
            if self._stream_has_content:
                self.console.print()
            # 不重置 self._stream_header_printed，讓主標頭保留
            self._round_text_printed = False
            self._round_is_dim = False
            self._stream_has_content = False
            self._thinking_buffer = ""

        elif kind == "text":
            # 判斷是否為「非首次」的中途輸出文字
            is_intermediate = self._stream_header_printed and not self._round_text_printed
            is_first_token = not self._stream_header_printed or is_intermediate

            if not self._stream_header_printed:
                # 第一個 token 到來：先停止思考中 spinner，再印標頭
                self._stop_status()
                self.console.print()  # 與上一條訊息間隔
                label = f"● Pocket Agent ({self._state.model})" if self._state is not None else "● Pocket Agent"
                self.console.print(Text(label, style="bold #B8860B"))
                self._stream_header_printed = True
                self._round_text_printed = True
                self._round_is_dim = False
            elif is_intermediate:
                # 後續的工具迴圈中途有文字輸出
                self._stop_status()
                self.console.print("[dim]│ 💭 [/dim]", end="", highlight=False, markup=True)
                self._round_text_printed = True
                self._round_is_dim = True

            text_str = str(data)
            self._text_buffer.append(text_str)
            self._text_buffer_len += len(text_str)
            now = monotonic()
            if (
                is_first_token
                or "\n" in text_str
                or self._text_buffer_len >= 16
                or (now - self._last_text_flush) >= 0.04
            ):
                self._flush_text_buffer()
            else:
                self._stream_has_content = True

        elif kind == "thinking":
            self._thinking_buffer += str(data)
            if self._active_status:
                now = monotonic()
                if (now - self._last_thinking_update) < 0.1:
                    return
                self._last_thinking_update = now
                snippet = self._thinking_buffer.replace("\n", " ").strip()
                if len(snippet) > 60:
                    snippet = "..." + snippet[-57:]
                if type(self._active_status).__name__ in ("Status", "MultiStatus", "Mock", "MagicMock", "AsyncMock"):
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
                call_id = data.get("call_id", name)
            else:
                name = str(data)
                args_raw = ""
                is_safe_read = False
                call_id = name

            real_name = name.split("::")[-1]
            sa_name = name.split("::", 1)[0] if "::" in name else None
            server_tag = None
            if real_name.startswith("mcp__"):
                mcp_parts = real_name.split("__", 2)
                if len(mcp_parts) == 3:
                    server_tag = mcp_parts[1]
                    real_name = mcp_parts[2]

            name_lower = real_name.lower()

            if any(kw in name_lower for kw in ["read", "list", "search", "view", "find", "grep"]):
                action = "Read"
            elif any(kw in name_lower for kw in ["write", "replace", "edit", "patch", "delete", "create"]):
                action = "Write"
            else:
                action = "Action"

            action_color = "bold yellow"
            name_color = "bold green" if is_safe_read else "bold cyan"
            target = self._extract_tool_target(name, args_raw)

            # 暫存供 tool_done 使用
            self._pending_tools[call_id] = {
                "name": name,
                "real_name": real_name,
                "action": action,
                "action_color": action_color,
                "name_color": name_color,
                "target": plain(target) if target else "",
                "summary": plain(target) if target else "",
                "sa_name": sa_name,
                "server_tag": server_tag,
            }

            if self.console.is_terminal:
                is_multi = self._active_status and type(self._active_status).__name__ == "MultiStatus"
                if not is_multi:
                    self._stop_status()
                    from rich.console import Group
                    from rich.live import Live
                    from rich.spinner import Spinner

                    group_items = list(self._format_tool_lines(("  ", ""), self._pending_tools[call_id]))
                    spinner = Spinner("dots", text=Text("執行工具中…", style="default"))
                    group_items.append(spinner)

                    self._active_status = Live(
                        Group(*group_items), console=self.console, refresh_per_second=12, transient=True
                    )
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
            is_multi = self._active_status and type(self._active_status).__name__ == "MultiStatus"
            if not is_multi:
                self._stop_status()

            if isinstance(data, dict):
                name = data.get("name", "")
                call_id = data.get("call_id", name)
                ok = data.get("ok", True)

                info = self._pending_tools.pop(call_id, None)
                if info:
                    bullet_color = "bold green" if ok else "bold red"
                    lines = self._format_tool_lines(("● ", bullet_color), info)
                    for line in lines:
                        self.console.print(line)

                if not ok:
                    if "::" in name:
                        sa_name, real_name = name.split("::", 1)
                        formatted_name = f"[{sa_name}] {real_name}"
                    elif name.startswith("mcp__"):
                        mcp_parts = name.split("__", 2)
                        formatted_name = f"[{mcp_parts[1]}] {mcp_parts[2]}" if len(mcp_parts) == 3 else name
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

            is_cancelled = False
            if isinstance(data, dict):
                res = str(data.get("result", ""))
                is_cancelled = not data.get("ok", True) or "[操作已由使用者取消中斷" in res

            if self.console.is_terminal and not is_multi and not is_cancelled:
                from rich.status import Status

                self._active_status = Status("思考中…", spinner="dots", console=self.console)
                self._active_status.start()

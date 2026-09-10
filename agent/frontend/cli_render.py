"""Safe, scrollback-friendly CLI presentation."""

import json
from pathlib import Path

from prompt_toolkit.formatted_text import FormattedText
from rich.cells import cell_len
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from .cli_state import CliState


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
    count = f"待送 {len(state.pending_files)}"
    tokens_str = f" · {state.session_tokens} tokens" if state.session_tokens > 0 else ""
    model = f"{state.provider}/{state.model}"
    budget = max(0, width - cell_len(count) - cell_len(tokens_str) - 3)
    line = f"{clip(model, budget)} · {count}{tokens_str}" if budget else f"{count}{tokens_str}"
    if state.pending_files:
        extra = f" · {Path(state.pending_files[0]).name}"
        if cell_len(plain(line + extra)) <= width:
            line += extra
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
            except (json.JSONDecodeError, ValueError):
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
        self.message(f"{state.provider} / {state.model} · {len(tools)} 個 MCP 工具 · 就緒")
        self.message("/ 指令 · @ 檔案 · Tab 補全 · Enter 送出 · Esc 後 Enter 換行")

    def user(self, raw: str) -> None:
        self.console.print()
        # Text keeps user markup literal while retaining intentional newlines.
        self.console.print(Text("› " + "\n".join(plain(line) for line in raw.split("\n")), style="bold blue"))

    def reply(self, reply: str, seconds: float, tokens: int, streamed: bool = False) -> None:
        if not streamed:
            # 非串流模式（fallback）：印標頭 + 完整 Markdown 回答
            self.console.print()
            self.message("● Pocket Agent", "bold #B8860B")
            self.console.print(Markdown(reply))
        else:
            # 串流模式：內容已逐字印出，確保換行後只印 footer
            if self._stream_has_content:
                self.console.print()  # 確保 footer 從新行開始

        # 重置串流狀態
        self._stream_header_printed = False
        self._stream_has_content = False
        self.message(f"本輪已返回 · {seconds:.1f} 秒 · {tokens} tokens")

    def event(self, kind: str, data: object) -> None:
        if kind == "system":
            self.message(data)

        elif kind == "round_start":
            # 工具呼叫後重跑：若已串流了文字，換行確認，再開始新輪
            if self._stream_has_content:
                self.console.print()
            self._stream_header_printed = False
            self._stream_has_content = False

        elif kind == "text":
            if not self._stream_header_printed:
                # 第一個 token 到來：先停止思考中 spinner，再印標頭
                self._stop_status()
                self.console.print()  # 與上一條訊息間隔
                label = f"● Pocket Agent ({self._state.model})" if self._state is not None else "● Pocket Agent"
                self.console.print(Text(label, style="bold #B8860B"))
                self._stream_header_printed = True
            self.console.print(str(data), end="", highlight=False)
            self._stream_has_content = True

        elif kind == "thinking":
            snippet = str(data).strip().split("\n")[0][:80]
            if snippet:
                self.console.print(Text(f"  ∴ {snippet}", style="dim italic"))

        elif kind == "read":
            self._ensure_newline()
            self.console.print(Text.assemble(("● ", "bold green"), ("Read", "bold yellow"), (f" ({plain(data)})", "")))

        elif kind == "tool_start":
            self._ensure_newline()

            if isinstance(data, dict):
                name = data.get("name", str(data))
                args_raw = data.get("arguments", "") or ""
            else:
                name = str(data)
                args_raw = ""

            name_lower = name.lower()
            native_tools = {
                "read_file",
                "write_to_file",
                "replace_file_content",
                "list_dir",
                "find_by_name",
                "grep_search",
                "search_web",
                "read_url_content",
            }
            is_native = name in native_tools

            danger_kws = [
                "bash",
                "python",
                "node",
                "powershell",
                "shell",
                "run",
                "delete",
                "move",
                "rm",
                "mv",
                "command",
                "cmd",
            ]
            bullet_color = "bold red" if any(kw in name_lower for kw in danger_kws) else "bold green"

            if any(kw in name_lower for kw in ["read", "list", "search", "view", "find", "grep"]):
                action = "Read"
                action_color = "bold yellow"
            elif any(kw in name_lower for kw in ["write", "replace", "edit", "patch", "delete", "create"]):
                action = "Write"
                action_color = "bold red"
            else:
                action = "Action"
                action_color = "bold cyan"

            name_color = "bold green" if is_native else ""
            summary = self._summarise_args(name, args_raw)

            parts = [("● ", bullet_color), (action, action_color), (" (", ""), (name, name_color), (")", "")]
            if summary:
                parts.append((f" {plain(summary)}", ""))

            self.console.print(Text.assemble(*parts))

        elif kind == "tool_done":
            self._ensure_newline()
            if isinstance(data, dict):
                name = data.get("name", "")
                ok = data.get("ok", True)
                if ok:
                    # 成功時不印出冗餘的 ✓ name
                    return
                icon = "✗"
                color = "red"
                self.console.print(Text(f"  {icon} {name}", style=color))

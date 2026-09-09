"""Safe, scrollback-friendly CLI presentation."""

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
    model = f"{state.provider}/{state.model}"
    budget = max(0, width - cell_len(count) - 3)
    line = f"{clip(model, budget)} · {count}" if budget else count
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
                    ("╭──────╮\n", "dim"),
                    ("│ >_   │  ", "dim"),
                    ("Pocket Agent\n", "bold #B8860B"),
                    ("│      │  ", "dim"),
                    ("你的終端 AI 工作夥伴\n", "dim"),
                    ("╰───╲──╯", "dim"),
                )
            )
        self.message(Path.cwd())
        self.message("─" * min(width, 80), "dim")

    def welcome(self, state: CliState, tools: list[str]) -> None:
        self.message(f"{state.provider} / {state.model} · {len(tools)} 個 MCP 工具 · 就緒")
        self.message("/ 指令 · @ 檔案 · Tab 補全 · Enter 送出 · Esc 後 Enter 換行")

    def user(self, raw: str) -> None:
        self.console.print()
        # Text keeps user markup literal while retaining intentional newlines.
        self.console.print(Text("› " + "\n".join(plain(line) for line in raw.split("\n")), style="bold blue"))

    def reply(self, reply: str, seconds: float, tokens: int) -> None:
        self.message("● Pocket Agent", "bold #B8860B")
        self.console.print(Markdown(reply))
        self.message(f"本輪已返回 · {seconds:.1f} 秒 · {tokens} tokens")

    def event(self, kind: str, data: object) -> None:
        if kind == "system":
            self.message(data)
        elif kind == "read":
            self.console.print(Text.assemble(("● ", "bold green"), ("Read", "bold yellow"), (f" ({plain(data)})", "")))
        elif kind == "tool_start":
            if isinstance(data, dict):
                name = data.get("name", str(data))
                args = data.get("arguments", "")
                if isinstance(args, str) and len(args) > 50:
                    args = args[:47] + "..."
            else:
                name = str(data)
                args = ""

            name_lower = name.lower()

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

            arg_str = f" ({name})" if not args else f" ({name} {args})"

            self.console.print(Text.assemble(("● ", bullet_color), (action, action_color), (plain(arg_str), "")))

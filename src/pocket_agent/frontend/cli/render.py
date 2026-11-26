"""Safe, scrollback-friendly CLI presentation."""

import json
from collections.abc import Callable
from pathlib import Path
from time import monotonic, time
from typing import Any

from prompt_toolkit.formatted_text import FormattedText
from rich.cells import cell_len
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from pocket_agent.extensions.mcp import parse_mcp_tool_name
from pocket_agent.frontend.cli.state import CliState
from pocket_agent.system.constants import DEFAULT_CONTEXT_WINDOW
from pocket_agent.system.paths import format_display_path


class MultiStatus:
    """支援多個子代理平行執行的動態表格 Spinner。"""

    def __init__(self, title, task_names, console):
        from rich.live import Live

        from pocket_agent.core.subagents import build_subagent_labels
        from pocket_agent.system.i18n import t

        self.title = title
        self.console = console
        self.task_names = build_subagent_labels([{"name": n} for n in task_names])

        self._states = {name: t("cli.status_starting") for name in self.task_names}
        self._live = Live(self._build_table(), console=console, refresh_per_second=10)
        self.is_running = False

    def _build_table(self):
        from rich.console import Group
        from rich.spinner import Spinner
        from rich.table import Table
        from rich.text import Text

        from pocket_agent.system.i18n import t

        table = Table(show_header=False, show_edge=False, box=None)
        table.add_column("Spinner", width=3)
        table.add_column("Name", style="bold cyan", width=15)
        table.add_column("Status")

        completed_word = t("cli.status_completed")
        error_word = t("cli.status_error")
        failed_word = t("cli.status_failed")

        for name in self.task_names:
            state = self._states[name]

            if "完成" in state or completed_word in state or "✓" in state:
                icon = Text("✓", style="bold green")
            elif "錯誤" in state or "失敗" in state or error_word in state or failed_word in state or "✗" in state:
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


_BG_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def _bg_status_badge(state: CliState | None) -> str:
    """組裝背景作業精簡狀態徽章字串；無活動工作時回傳空字串。"""
    if state is None:
        return ""
    from pocket_agent.system.i18n import t

    bg_running = getattr(state, "bg_jobs_running", 0)
    bg_done = getattr(state, "bg_jobs_done", 0)
    valid_run = isinstance(bg_running, int) and not isinstance(bg_running, bool) and bg_running > 0
    valid_done = isinstance(bg_done, int) and not isinstance(bg_done, bool) and bg_done > 0
    if valid_run and valid_done:
        return t("cli.toolbar_bg_both", running=bg_running, done=bg_done)
    if valid_run:
        return t("cli.toolbar_bg_running", running=bg_running)
    if valid_done:
        return t("cli.toolbar_bg_done", done=bg_done)
    return ""


def _format_bg_status_line(state: CliState, width: int) -> tuple[str, str] | None:
    """若存在執行中或待取回背景子代理，產生專屬高對比狀態列（方案 A）。"""
    from pocket_agent.system.i18n import t

    bg_running = getattr(state, "bg_jobs_running", 0)
    bg_done = getattr(state, "bg_jobs_done", 0)
    valid_run = isinstance(bg_running, int) and not isinstance(bg_running, bool) and bg_running > 0
    valid_done = isinstance(bg_done, int) and not isinstance(bg_done, bool) and bg_done > 0
    if not valid_run and not valid_done:
        return None

    summaries = getattr(state, "bg_job_summaries", None)
    parts: list[str] = []
    now_ts = time()
    if isinstance(summaries, list):
        for item in summaries:
            if not isinstance(item, dict):
                continue
            jid = str(item.get("job_id") or "")
            name = str(item.get("name") or "")
            status = str(item.get("status") or "")
            if not jid:
                continue
            if status == "running":
                created = item.get("created_at")
                elapsed = max(0.0, now_ts - float(created)) if isinstance(created, (int, float)) else 0.0
                prog = plain(item.get("progress") or "").strip()
                if len(prog) > 28:
                    prog = prog[:27] + "…"
                prog_str = f" {prog}" if prog else ""
                parts.append(f"{jid} [{name}]{prog_str} ({elapsed:.0f}s)")
            else:
                icon = "✓" if status == "completed" else "✗"
                parts.append(f"{jid} [{name}] {icon}")

    body = " │ ".join(parts)
    if valid_run:
        frame = _BG_SPINNER_FRAMES[int(monotonic() * 2) % len(_BG_SPINNER_FRAMES)]
        if valid_done:
            prefix = t("cli.toolbar_bg_bar_both", spinner=frame, running=bg_running, done=bg_done)
        else:
            prefix = t("cli.toolbar_bg_bar_running", spinner=frame, running=bg_running)
        return ("bold ansicyan", clip(f"{prefix}{body}", width))

    prefix = t("cli.toolbar_bg_bar_done", done=bg_done)
    hint = t("cli.toolbar_bg_bar_hint")
    suffix = f"{body}{hint}" if body else hint.lstrip(" ·")
    return ("bold ansigreen", clip(f"{prefix}{suffix}", width))


def toolbar(state: CliState, width: int) -> FormattedText:
    from pocket_agent.system.i18n import t

    width = max(0, width)
    if width <= 0:
        return FormattedText([("", "")])

    count = t("cli.toolbar_pending", count=len(state.pending_files) if state.pending_files else 0)

    if state.session_tokens > 0:
        max_tokens = max(1, getattr(state, "context_window", DEFAULT_CONTEXT_WINDOW) or DEFAULT_CONTEXT_WINDOW)
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
        tokens_str += t("cli.toolbar_quota", used=used, limit=state.quota_limit)

    accept_status = "ON" if getattr(state, "accept_edits", False) else "OFF"
    reasoning_val = str(getattr(state, "reasoning_effort", "auto") or "auto").strip().capitalize()
    tokens_str += t("cli.toolbar_reasoning", reasoning=reasoning_val, accept=accept_status)

    if getattr(state, "staged_update_version", None):
        tokens_str += t("cli.toolbar_update_ready", version=state.staged_update_version)

    model = f"[ {state.provider} ] {state.model}"
    budget = max(0, width - cell_len(count) - cell_len(tokens_str) - 3)
    line = f"{clip(model, budget)} · {count}{tokens_str}" if budget else f"{count}{tokens_str}"

    for extra in [
        t("cli.toolbar_hint_cmd"),
        t("cli.toolbar_hint_accept"),
        t("cli.toolbar_hint_keys"),
        f" · {Path.cwd().name}",
    ]:
        if cell_len(plain(line + extra)) <= width:
            line += extra

    bg_line = _format_bg_status_line(state, width)
    if bg_line is not None:
        return FormattedText([bg_line, ("", "\n"), ("", clip(line, width))])
    return FormattedText([("", clip(line, width))])


_SOFT_TOOL_STATUS_STYLES: dict[str, tuple[str, str]] = {
    "not_found": ("bold green", "cli.tool_not_found_suffix"),
    "overwrite_required": ("bold yellow", "cli.tool_overwrite_required_suffix"),
    "target_not_found": ("bold yellow", "cli.tool_target_not_found_suffix"),
}

TargetExtractor = Callable[[str, dict[str, Any]], tuple[str, bool] | None]

_SHELL_TOOL_KEYWORDS = ("run_command", "bash", "shell", "python", "powershell", "node", "cmd")
_FILE_WRITE_KEYWORDS = ("replace", "edit", "patch", "write", "create")
_FILE_READ_KEYWORDS = ("read", "view", "list", "find", "grep", "search")
_WEB_TOOL_KEYWORDS = ("web", "fetch", "curl", "url", "browser")

_TOOL_ACTION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Read", ("read", "list", "search", "view", "find", "grep", "get", "prepare")),
    ("Write", ("write", "replace", "edit", "patch", "delete", "create")),
)


def _classify_tool_action(name_lower: str) -> str:
    """依工具名稱關鍵字分類顯示動作（Read / Write / Action）。"""
    for action, keywords in _TOOL_ACTION_RULES:
        if any(kw in name_lower for kw in keywords):
            return action
    return "Action"


def _extract_github_target(_name_lower: str, parsed: dict[str, Any]) -> tuple[str, bool] | None:
    owner = parsed.get("owner")
    repo = parsed.get("repo")
    if not owner or not repo:
        return None
    base_repo = f"{owner}/{repo}"
    issue_no = parsed.get("issue_number")
    if issue_no is None or str(issue_no).strip() == "":
        issue_no = parsed.get("pull_number")
    if issue_no is not None and str(issue_no).strip() != "":
        return f"{base_repo}#{issue_no}", False
    if parsed.get("head") and str(parsed["head"]).strip():
        head_s = str(parsed["head"]).strip()
        base_s = str(parsed.get("base") or "").strip() or "default"
        return f"{base_repo} ({head_s} -> {base_s})", False
    if parsed.get("check_run_id") is not None and str(parsed["check_run_id"]).strip():
        return f"{base_repo} check#{parsed['check_run_id']}", False
    if parsed.get("run_id") is not None and str(parsed["run_id"]).strip():
        return f"{base_repo} run#{parsed['run_id']}", False
    if parsed.get("path") and str(parsed["path"]).strip():
        path_s = str(parsed["path"]).strip()
        ref_s = str(parsed.get("ref") or "").strip()
        return f"{base_repo}:{path_s}" + (f"@{ref_s}" if ref_s else ""), False
    if parsed.get("ref") and str(parsed["ref"]).strip():
        return f"{base_repo}@{str(parsed['ref']).strip()}", False
    return base_repo, False


def _extract_shell_target(name_lower: str, parsed: dict[str, Any]) -> tuple[str, bool] | None:
    if not any(k in name_lower for k in _SHELL_TOOL_KEYWORDS):
        return None
    cmd = parsed.get("command") or parsed.get("cmd") or ""
    return " ".join(str(cmd).splitlines()).strip(), False


def _extract_file_write_target(name_lower: str, parsed: dict[str, Any]) -> tuple[str, bool] | None:
    if not any(k in name_lower for k in _FILE_WRITE_KEYWORDS):
        return None
    path_val = parsed.get("target_file") or parsed.get("file_path") or parsed.get("path") or ""
    return str(path_val), True


def _extract_file_read_target(name_lower: str, parsed: dict[str, Any]) -> tuple[str, bool] | None:
    if not any(k in name_lower for k in _FILE_READ_KEYWORDS):
        return None
    path_val = parsed.get("file_path") or parsed.get("path") or parsed.get("directory") or ""
    if not path_val:
        return str(parsed.get("query") or parsed.get("pattern") or ""), False
    val = str(path_val)
    start_l = parsed.get("start_line") or parsed.get("offset")
    end_l = parsed.get("end_line") or parsed.get("limit")
    if start_l is not None and end_l is not None:
        return f"{val}#L{start_l}-L{end_l}", True
    if start_l is not None:
        return f"{val}#L{start_l}", True
    return val, True


def _extract_web_target(name_lower: str, parsed: dict[str, Any]) -> tuple[str, bool] | None:
    if not any(k in name_lower for k in _WEB_TOOL_KEYWORDS):
        return None
    return str(parsed.get("url") or parsed.get("endpoint") or ""), False


def _extract_subagent_target(name_lower: str, parsed: dict[str, Any]) -> tuple[str, bool] | None:
    if "subagent" not in name_lower:
        return None
    tasks = parsed.get("tasks")
    if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict):
        return str(tasks[0].get("task", "")), False
    return str(parsed.get("task", "")), False


_TOOL_TARGET_EXTRACTORS: tuple[TargetExtractor, ...] = (
    _extract_github_target,
    _extract_shell_target,
    _extract_file_write_target,
    _extract_file_read_target,
    _extract_web_target,
    _extract_subagent_target,
)


def reset_live_render_shape(status: object) -> None:
    """清除 Rich Live / Status 停止後殘留的 _live_render._shape，避免重啟時上移游標抹除既有輸出。"""
    from rich.live_render import LiveRender

    for candidate in (status, getattr(status, "_live", None)):
        if candidate is None:
            continue
        live_render = getattr(candidate, "_live_render", None)
        if isinstance(live_render, LiveRender):
            live_render._shape = None


class CliRenderer:
    def __init__(self, console: Console):
        self.console = console
        # 串流狀態旗標：追蹤本輪是否已開始串流輸出
        self._stream_header_printed: bool = False
        self._round_text_printed: bool = False
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
        self._auto_multi_status: bool = False

    def _stop_status(self) -> None:
        """停止思考中 spinner（若仍在執行），確保不與串流輸出衝突。"""
        if self._active_status is not None:
            try:
                self._active_status.stop()
                reset_live_render_shape(self._active_status)
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

        raw_name = name.split("::")[-1]
        _, raw_name = parse_mcp_tool_name(raw_name)
        name_lower = raw_name.lower()
        parsed: dict | None = None
        if args_raw:
            try:
                parsed = json.loads(args_raw)
            except json.JSONDecodeError, ValueError, TypeError:
                parsed = None

        val = ""
        is_path = False

        if parsed and isinstance(parsed, dict):
            extracted: tuple[str, bool] | None = None
            for extractor in _TOOL_TARGET_EXTRACTORS:
                extracted = extractor(name_lower, parsed)
                if extracted is not None:
                    break
            if extracted is not None:
                val, is_path = extracted
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

    def _format_tool_lines(self, prefix: tuple[str, str], info: dict, status_note: str = "") -> list[Text]:
        """格式化工具行：未超過終端寬度時單行接續，超過時改為樹狀結構兩行。"""
        parts = [prefix, (info["action"], info["action_color"])]
        if info.get("sa_name"):
            parts.append((f" [{info['sa_name']}]", "bold magenta"))
        if info.get("server_tag"):
            parts.append((f" [{info['server_tag']}]", "bold cyan"))
        parts.extend([(" (", ""), (info["real_name"], info["name_color"]), (")", "")])

        target_text = info.get("target") or info.get("summary") or ""
        if not target_text:
            if status_note:
                parts.append((status_note, "dim"))
            return [Text.assemble(*parts)]

        term_w = getattr(self.console, "width", 80) or 80
        header_text = Text.assemble(*parts)
        header_w = cell_len(header_text.plain)
        target_w = cell_len(target_text) + cell_len(status_note)

        # 預留 1 格空白與 1 格終端右側緩衝
        if header_w + 1 + target_w <= max(20, term_w - 1):
            if target_text.startswith("【安全警告】"):
                parts.extend([(" ", ""), ("【安全警告】", "bold red"), (f" {target_text[6:]}", "dim")])
            else:
                parts.extend([(" ", ""), (target_text, "dim")])
            if status_note:
                parts.append((status_note, "dim"))
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
        if status_note:
            sub_parts.append((status_note, "dim"))
        return [header_text, Text.assemble(*sub_parts)]

    def message(self, value: object, style: str = "dim") -> None:
        self.console.print(Text(plain(value), style=style))

    def banner(self) -> None:
        from pocket_agent.system.i18n import t
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
                    (f"{t('cli.banner_tagline')}\n", ""),
                    ("╰───╲──╯", ""),
                )
            )
        self.message(Path.cwd(), "")
        self.message("─" * min(width, 80), "")

    def welcome(self, state: CliState, tools: list[str]) -> None:
        from pocket_agent.system.i18n import t

        self.message(
            t(
                "cli.welcome_status",
                provider=state.provider,
                model=state.model,
                tool_count=len(tools),
            )
        )
        self.message(t("cli.welcome_shortcuts"))

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
        self._round_text_printed = False
        self._stream_has_content = False
        self._thinking_buffer = ""
        from pocket_agent.system.i18n import t

        footer = t("cli.reply_footer", seconds=seconds, tokens=tokens)
        bg_badge = _bg_status_badge(self._state)
        if bg_badge:
            footer += bg_badge
        self.message(footer)

    def _is_multi_status(self) -> bool:
        return self._active_status is not None and type(self._active_status).__name__ == "MultiStatus"

    def _on_system(self, data: object) -> None:
        self.message(data)

    def _on_error_panel(self, data: object) -> None:
        self._ensure_newline()
        self._stop_status()

        from rich.panel import Panel

        from pocket_agent.system.i18n import t

        default_err = t("cli.status_error")
        title = data.get("title", default_err) if isinstance(data, dict) else default_err
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

    def _on_file_saved(self, data: object) -> None:
        self._ensure_newline()
        from pocket_agent.system.i18n import t

        self.message(t("cli.file_saved", path=data), "green")

    def _on_round_start(self, _data: object) -> None:
        self._flush_text_buffer()
        # 工具呼叫後重跑：若已串流了文字，換行確認，再開始新輪
        if self._stream_has_content:
            self.console.print()
        # 不重置 self._stream_header_printed，讓主標頭保留
        self._round_text_printed = False
        self._stream_has_content = False
        self._thinking_buffer = ""

    def _on_text(self, data: object) -> None:
        # 判斷是否為「非首次」的後續輪次文字（例如工具執行完畢後的延續或最終回覆）
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
        elif is_intermediate:
            # 工具迴圈後續輪次文字：停止 spinner 並空一行區隔，以正常前景顏色輸出
            self._stop_status()
            self.console.print()
            self._round_text_printed = True

        text_str = str(data)
        self._text_buffer.append(text_str)
        self._text_buffer_len += len(text_str)
        now = monotonic()
        if is_first_token or "\n" in text_str or self._text_buffer_len >= 16 or (now - self._last_text_flush) >= 0.04:
            self._flush_text_buffer()
        else:
            self._stream_has_content = True

    def _on_parallel_subagents_start(self, data: object) -> None:
        labels = data.get("labels", []) if isinstance(data, dict) else []
        if self.console.is_terminal and labels and not self._is_multi_status():
            self._stop_status()
            self._ensure_newline()
            from pocket_agent.system.i18n import t

            title = (data.get("title") if isinstance(data, dict) else None) or t(
                "subagent_cmd.parallel_running", count=len(labels)
            )
            status = MultiStatus(title, labels, console=self.console)
            self._active_status = status
            self._auto_multi_status = True
            status.start()

    def _on_parallel_subagents_done(self, _data: object) -> None:
        if self._auto_multi_status:
            self._stop_status()
            self._auto_multi_status = False

    def _on_thinking(self, data: object) -> None:
        if self._is_multi_status():
            line = " ".join(str(data).splitlines()).strip()
            if line and self._active_status is not None:
                self._active_status.update(line)
            return
        self._thinking_buffer += str(data)
        if not self._active_status:
            return
        now = monotonic()
        if (now - self._last_thinking_update) < 0.1:
            return
        self._last_thinking_update = now
        snippet = self._thinking_buffer.replace("\n", " ").strip()
        if len(snippet) > 60:
            snippet = "..." + snippet[-57:]
        if type(self._active_status).__name__ in ("Status", "MultiStatus", "Mock", "MagicMock", "AsyncMock"):
            from pocket_agent.system.i18n import t

            self._active_status.update(f"{t('cli.status_thinking')} {snippet}")

    def _on_read(self, data: object) -> None:
        self._ensure_newline()
        self.console.print(Text.assemble(("● ", "bold green"), ("Read", "bold yellow"), (f" ({plain(data)})", "")))

    def _on_tool_stream_start(self, data: object) -> None:
        self._ensure_newline()
        from pocket_agent.system.i18n import t

        msg = t("cli.tool_preparing", name=data)
        if self._active_status:
            self._active_status.update(msg)
            return
        from rich.status import Status

        status = Status(msg, spinner="dots", console=self.console)
        self._active_status = status
        status.start()

    def _on_tool_start(self, data: object) -> None:
        is_multi = self._is_multi_status()
        if not is_multi:
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
        server_tag, real_name = parse_mcp_tool_name(real_name)

        action = _classify_tool_action(real_name.lower())
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

        if is_multi:
            if sa_name and self._active_status is not None:
                target_suffix = f" {plain(target)}" if target else ""
                self._active_status.update(f"[{sa_name}] {action} ({real_name}){target_suffix}")
            return
        if self.console.is_terminal:
            self._stop_status()
            from rich.console import Group
            from rich.live import Live
            from rich.spinner import Spinner

            from pocket_agent.system.i18n import t

            group_items = list(self._format_tool_lines(("  ", ""), self._pending_tools[call_id]))
            spinner = Spinner("dots", text=Text(t("cli.tool_running"), style="default"))
            group_items.append(spinner)

            self._active_status = Live(Group(*group_items), console=self.console, refresh_per_second=12, transient=True)
            self._active_status.start()

    def _on_ask_question(self, data: object) -> None:
        self._ensure_newline()
        self._stop_status()

        questions = data.get("questions", []) if isinstance(data, dict) else []
        callback = data.get("callback") if isinstance(data, dict) else None

        def _ask_worker() -> None:
            try:
                from prompt_toolkit import prompt
                from rich.panel import Panel

                from pocket_agent.system.i18n import t

                answers = []
                for q in questions:
                    text = q.get("question", "")
                    options = q.get("options", [])
                    is_multi = q.get("is_multi_select", False)

                    self.console.print()
                    self.console.print(
                        Panel(
                            Text(text),
                            title=t("cli.ask_question_title"),
                            title_align="left",
                            border_style="bold blue",
                        )
                    )
                    for i, opt in enumerate(options):
                        self.console.print(f"  [{i + 1}] {opt}")

                    hint = t("cli.ask_question_hint")
                    if is_multi:
                        hint += t("cli.ask_question_multi")

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

    def _on_tool_done(self, data: object) -> None:
        is_multi = self._is_multi_status()
        if not is_multi:
            self._stop_status()

        if isinstance(data, dict):
            name = data.get("name", "")
            call_id = data.get("call_id", name)
            ok = data.get("ok", True)
            status = data.get("status") or ("ok" if ok else "error")

            info = self._pending_tools.pop(call_id, None)
            if info:
                from pocket_agent.system.i18n import t

                is_error = status == "error" or not ok
                if is_error:
                    bullet_color = "bold red"
                    status_note = ""
                elif status in _SOFT_TOOL_STATUS_STYLES:
                    bullet_color, i18n_key = _SOFT_TOOL_STATUS_STYLES[status]
                    status_note = t(i18n_key)
                else:
                    bullet_color = "bold green"
                    status_note = ""
                lines = self._format_tool_lines(("● ", bullet_color), info, status_note=status_note)
                for line in lines:
                    self.console.print(line)

            if status == "error" or not ok:
                if is_multi and info and info.get("sa_name") and self._active_status is not None:
                    self._active_status.update(f"[{info['sa_name']}] ✗ {info['real_name']}")
                if "::" in name:
                    sa_name, inner_name = name.split("::", 1)
                    server_tag, real_name = parse_mcp_tool_name(inner_name)
                    if server_tag:
                        formatted_name = f"[{sa_name}] [{server_tag}] {real_name}"
                    else:
                        formatted_name = f"[{sa_name}] {real_name}"
                else:
                    server_tag, real_name = parse_mcp_tool_name(name)
                    formatted_name = f"[{server_tag}] {real_name}" if server_tag else name

                result_text = str(data.get("result", ""))

                from rich.panel import Panel
                from rich.syntax import Syntax

                # 嘗試以 Syntax 高亮，若為 JSON 格式
                is_json = False
                if result_text.strip().startswith("{") or result_text.strip().startswith("["):
                    try:
                        json.loads(result_text)
                        is_json = True
                    except Exception:
                        pass

                if is_json:
                    content = Syntax(result_text, "json", theme="monokai", background_color="default", word_wrap=True)
                else:
                    content = Text(result_text, style="red")

                from pocket_agent.system.i18n import t

                self.console.print()
                self.console.print(
                    Panel(
                        content,
                        title=t("cli.tool_failed_title", name=formatted_name),
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

            from pocket_agent.system.i18n import t

            self._active_status = Status(t("cli.status_thinking"), spinner="dots", console=self.console)
            self._active_status.start()

    _EVENT_HANDLERS: dict[str, Callable[["CliRenderer", object], None]] = {
        "system": _on_system,
        "error_panel": _on_error_panel,
        "image_saved": _on_file_saved,
        "file_saved": _on_file_saved,
        "round_start": _on_round_start,
        "text": _on_text,
        "parallel_subagents_start": _on_parallel_subagents_start,
        "parallel_subagents_done": _on_parallel_subagents_done,
        "thinking": _on_thinking,
        "read": _on_read,
        "tool_stream_start": _on_tool_stream_start,
        "tool_start": _on_tool_start,
        "ask_question": _on_ask_question,
        "tool_done": _on_tool_done,
    }

    def event(self, kind: str, data: object) -> None:
        handler = self._EVENT_HANDLERS.get(kind)
        if handler is not None:
            handler(self, data)

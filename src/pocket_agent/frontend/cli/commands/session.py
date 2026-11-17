import time

from rich.text import Text

from pocket_agent.frontend.cli.commands.core import (
    CliCommand,
    CliCommandRegistry,
    CommandContext,
    CommandResult,
)
from pocket_agent.system.i18n import t


def _load_session_into_context(ctx: CommandContext, session_id: str) -> None:
    """載入指定 session 至當前上下文，並同步重設相關狀態。"""
    from pocket_agent.core.sessions import load_session, save_session

    data = load_session(session_id)
    if data is None:
        raise ValueError(t("session_cmd.not_found", id=session_id))
    if ctx.agent.history and getattr(ctx.agent, "is_sensitive_session", False) is not True:
        save_session(ctx.state.session_id, ctx.agent.history)

    if callable(getattr(type(ctx.agent), "load_session_history", None)):
        ctx.agent.load_session_history(data["history"])
    else:
        ctx.agent.history = list(data["history"])
        ctx.agent.is_sensitive_session = False
        ctx.agent.context_usage = None
        if hasattr(ctx.agent, "activity") and ctx.agent.activity is not None:
            ctx.agent.activity.rebuild_from_history(ctx.agent.history)

    ctx.state.session_id = data["id"]
    ctx.state.session_tokens = 0
    ctx.state.last_failed_input = ""
    ctx.renderer.message(
        t(
            "session_cmd.loaded",
            title=data.get("title", t("cli.session_untitled")),
            count=len(data["history"]),
        ),
        "green",
    )


def _delete_session_by_id(ctx: CommandContext, session_id: str) -> None:
    """刪除指定 session；若為當前工作階段則同步重置上下文。"""
    from pocket_agent.core.sessions import delete_session, load_session, new_session_id

    data = load_session(session_id)
    title = data.get("title", session_id) if data else session_id
    delete_session(session_id)
    if ctx.state.session_id == session_id:
        if callable(getattr(type(ctx.agent), "reset_session", None)):
            ctx.agent.reset_session()
        else:
            ctx.agent.history = []
            ctx.agent.is_sensitive_session = False
            ctx.agent.context_usage = None
            if hasattr(ctx.agent, "activity") and ctx.agent.activity is not None:
                ctx.agent.activity.clear()
        ctx.state.session_id = new_session_id()
        ctx.state.session_tokens = 0
        ctx.state.last_failed_input = ""
    ctx.renderer.message(t("session_cmd.deleted", title=title))


class _SessionInteractiveCommand(CliCommand):
    """互動式分頁瀏覽歷史對話並執行載入或刪除。"""

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from pocket_agent.core.sessions import list_sessions

        sessions = list_sessions()
        if not sessions:
            ctx.renderer.message(t("session_cmd.no_history"))
            return CommandResult()

        selected = await self._pick_session(ctx, sessions)
        if not selected:
            return CommandResult()

        load_label = t("session_cmd.action_load")
        delete_label = t("session_cmd.action_delete")
        cancel_label = t("session_cmd.cancel")
        actions = {
            load_label: _load_session_into_context,
            delete_label: _delete_session_by_id,
        }
        action = await ctx.reader.choose(
            t("session_cmd.pick_action", title=selected["title"][:30]),
            [load_label, delete_label, cancel_label],
        )
        handler = actions.get(action)
        if handler is not None:
            handler(ctx, selected["id"])
        return CommandResult()

    @staticmethod
    async def _pick_session(ctx: CommandContext, sessions: list[dict]) -> dict | None:
        page = 0
        page_size = 5
        more_label = t("session_cmd.load_more")

        while True:
            start = page * page_size
            end = start + page_size
            chunk = sessions[start:end]

            choices = [
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(s['updated_at']))}  {s['title'][:40]}" for s in chunk
            ]
            if end < len(sessions):
                choices.append(more_label)

            selected_label = await ctx.reader.choose(t("session_cmd.pick_session"), choices)
            if not selected_label:
                return None
            if selected_label == more_label:
                page += 1
                continue
            if selected_label not in choices:
                return None
            return chunk[choices.index(selected_label)]


class _SessionListCommand(CliCommand):
    """列出所有歷史對話摘要（/session list）。"""

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from pocket_agent.core.sessions import list_sessions

        sessions = list_sessions()
        if not sessions:
            ctx.renderer.message(t("session_cmd.no_history"))
            return CommandResult()

        for i, s in enumerate(sessions, 1):
            dt = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["updated_at"]))
            ctx.renderer.message(f"{i:2}. [{s['id']}]  {s['title']}  ({dt})")
        return CommandResult()


class _SessionLoadCommand(CliCommand):
    """載入指定歷史對話（/session load <id>）。"""

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if not args:
            raise ValueError(t("session_cmd.usage_load"))
        _load_session_into_context(ctx, args)
        return CommandResult()


class _SessionSaveCommand(CliCommand):
    """手動儲存目前對話（/session save）。"""

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from pocket_agent.core.sessions import save_session

        if not ctx.agent.history:
            ctx.renderer.message(t("session_cmd.empty_history_skip"))
        elif getattr(ctx.agent, "is_sensitive_session", False) is True:
            ctx.renderer.message(t("cli.sensitive_session_warning"), "yellow")
        else:
            save_session(ctx.state.session_id, ctx.agent.history)
            ctx.renderer.message(t("session_cmd.saved", id=ctx.state.session_id), "green")
        return CommandResult()


class _SessionDeleteCommand(CliCommand):
    """刪除指定歷史對話（/session delete <id>）。"""

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if not args:
            raise ValueError(t("session_cmd.usage_delete"))
        _delete_session_by_id(ctx, args)
        return CommandResult()


class SessionCommand(CliCommand):
    """透過子指令註冊表派發 /session 各項操作（Command Pattern）。"""

    def __init__(self) -> None:
        self._registry = CliCommandRegistry()
        self._registry.register("", _SessionInteractiveCommand())
        self._registry.register("list", _SessionListCommand())
        self._registry.register("load", _SessionLoadCommand())
        self._registry.register("save", _SessionSaveCommand())
        self._registry.register("delete", _SessionDeleteCommand())
        self._registry.register("prune", PruneCommand())

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        parts = args.split(maxsplit=1)
        sub = parts[0] if parts else ""
        sub_arg = parts[1] if len(parts) > 1 else ""

        command = self._registry.get(sub)
        if command is None:
            raise ValueError(t("session_cmd.usage_session"))
        return await command.execute(ctx, sub_arg)


def _parse_prune_args(args: str) -> tuple[int | None, int | None]:
    """解析 /prune 參數，回傳 (days, keep)。"""
    raw = args.strip()
    if not raw:
        return (None, None)
    parts = raw.split()
    if len(parts) == 1 and parts[0].isdigit():
        days = int(parts[0])
        if days < 1:
            raise ValueError(t("session_cmd.prune_days_invalid"))
        return (days, None)
    if len(parts) == 2 and parts[0] == "--keep" and parts[1].isdigit():
        keep = int(parts[1])
        return (None, keep)
    raise ValueError(t("session_cmd.usage_prune"))


def _format_bytes_human(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} MB"
    return f"{max(0.0, num_bytes / 1024):.1f} KB"


async def _run_prune_flow(ctx: CommandContext, args: str) -> None:
    from pocket_agent.core.sessions import find_prunable_sessions, prune_sessions
    from pocket_agent.system.constants import DEFAULT_SESSION_PRUNE_DAYS, DEFAULT_SESSION_PRUNE_KEEP

    days, keep = _parse_prune_args(args)
    if days is None and keep is None:
        opt_days = t("session_cmd.prune_opt_days", days=DEFAULT_SESSION_PRUNE_DAYS)
        opt_keep = t("session_cmd.prune_opt_keep", keep=DEFAULT_SESSION_PRUNE_KEEP)
        cancel_label = t("session_cmd.cancel")
        choice = await ctx.reader.choose(
            t("session_cmd.prune_choose_rule"),
            [opt_days, opt_keep, cancel_label],
        )
        if not choice or choice == cancel_label:
            ctx.renderer.message(t("session_cmd.prune_cancelled"))
            return
        if choice == opt_days:
            days = DEFAULT_SESSION_PRUNE_DAYS
        else:
            keep = DEFAULT_SESSION_PRUNE_KEEP

    current_sid = getattr(ctx.state, "session_id", None)
    candidates, total_count, total_bytes = find_prunable_sessions(
        days=days,
        keep=keep,
        exclude_session_id=current_sid,
    )
    rule_desc = (
        t("session_cmd.prune_rule_days", days=days) if days is not None else t("session_cmd.prune_rule_keep", keep=keep)
    )

    if not candidates:
        ctx.renderer.message(
            t(
                "session_cmd.prune_none",
                rule=rule_desc,
                total=total_count,
                size=_format_bytes_human(total_bytes),
            ),
            "green",
        )
        return

    est_bytes = sum(c.get("size_bytes", 0) for c in candidates)
    ctx.renderer.message(
        t(
            "session_cmd.prune_found",
            count=len(candidates),
            rule=rule_desc,
            size=_format_bytes_human(est_bytes),
        ),
        "yellow",
    )
    for item in candidates[:5]:
        dt = time.strftime("%Y-%m-%d %H:%M", time.localtime(item.get("updated_at", 0)))
        ctx.renderer.message(f"  • [{dt}] {item.get('title', t('cli.session_untitled'))}", "dim")
    if len(candidates) > 5:
        ctx.renderer.message(t("session_cmd.prune_more", count=len(candidates) - 5), "dim")

    yes_label = t("session_cmd.prune_confirm_yes")
    cancel_label = t("session_cmd.cancel")
    confirm = await ctx.reader.choose(
        t("session_cmd.prune_confirm_prompt", count=len(candidates)),
        [yes_label, cancel_label],
    )
    if confirm != yes_label:
        ctx.renderer.message(t("session_cmd.prune_cancelled"))
        return

    deleted, freed = prune_sessions([c["id"] for c in candidates], exclude_session_id=current_sid)
    ctx.renderer.message(
        t("session_cmd.prune_done", deleted=deleted, size=_format_bytes_human(freed)),
        "green",
    )


class PruneCommand(CliCommand):
    """手動清理過期或過多的歷史對話（/prune [<天數> | --keep <筆數>]）。"""

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        await _run_prune_flow(ctx, args)
        return CommandResult()


class CompactCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError(t("session_cmd.compact_no_args"))
        history = ctx.agent.history
        if len(history) < 6:
            ctx.renderer.message(t("session_cmd.compact_too_short"))
            return CommandResult()

        ctx.renderer.message(t("session_cmd.compact_running"), "dim")
        original_len = len(history)
        keep_recent = 2 if len(history) < 8 else 4

        from pocket_agent.frontend.cli.interrupts import OperationInterrupted, run_operation

        def stopping() -> None:
            ctx.agent.request_stop()
            ctx.renderer._stop_status()
            ctx.renderer.message(t("cli.stopping"), "yellow")

        try:
            success, orig_tokens, comp_tokens = await run_operation(
                ctx.agent.compact_history(keep_recent=keep_recent), stopping
            )
        except OperationInterrupted:
            ctx.renderer._stop_status()
            ctx.renderer.message(t("session_cmd.compact_interrupted"), "yellow")
            return CommandResult()
        except Exception as e:
            ctx.renderer.message(t("session_cmd.compact_failed", error=e), "red")
            return CommandResult()

        if not success:
            ctx.renderer.message(t("session_cmd.compact_no_gain"), "yellow")
            return CommandResult()

        saved_tokens = max(0, orig_tokens - comp_tokens)
        ctx.renderer.message(
            t(
                "session_cmd.compact_done",
                orig_len=original_len,
                new_len=len(ctx.agent.history),
                orig_tokens=orig_tokens,
                comp_tokens=comp_tokens,
                saved_tokens=saved_tokens,
            ),
            "green",
        )
        return CommandResult()


class NewCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError(t("session_cmd.new_no_args"))
        from pocket_agent.core.sessions import new_session_id, save_session

        old_session_id = ctx.state.session_id
        # 若原先對話有內容，先自動儲存保護工作成果
        if ctx.agent.history:
            if getattr(ctx.agent, "is_sensitive_session", False) is True:
                saved_msg = t("session_cmd.new_prev_sensitive")
            else:
                try:
                    save_session(old_session_id, ctx.agent.history)
                    saved_msg = t("session_cmd.new_prev_saved", id=old_session_id)
                except Exception as e:
                    saved_msg = t("session_cmd.new_prev_save_failed", error=e)
        else:
            saved_msg = ""

        # 徹底清空當前上下文與會話狀態
        if callable(getattr(type(ctx.agent), "reset_session", None)):
            ctx.agent.reset_session()
        else:
            ctx.agent.clear_history()
            ctx.agent.is_sensitive_session = False
            ctx.agent.accept_edits = False
            ctx.agent.context_usage = None
            ctx.agent.last_images.clear()
            ctx.agent.last_files.clear()
            if hasattr(ctx.agent, "user_instructions") and isinstance(ctx.agent.user_instructions, list):
                ctx.agent.user_instructions.clear()
            if hasattr(ctx.agent, "activity") and ctx.agent.activity is not None:
                ctx.agent.activity.clear()
        ctx.state.session_id = new_session_id()
        ctx.state.pending_files.clear()
        ctx.state.last_failed_input = ""
        ctx.state.session_tokens = 0
        ctx.state.session_approved_commands.clear()
        ctx.state.accept_edits = False

        info = t("session_cmd.new_started", id=ctx.state.session_id)
        if saved_msg:
            info += f"\n  {saved_msg}"
        ctx.renderer.message(info, "green")
        return CommandResult()


class RenameCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        new_title = args.strip()
        if not new_title:
            try:
                prompt_input = await ctx.reader.read(t("session_cmd.rename_prompt"))
                new_title = prompt_input.strip()
            except KeyboardInterrupt, EOFError:
                ctx.renderer.message(t("session_cmd.rename_cancelled"))
                return CommandResult()

        if not new_title:
            ctx.renderer.message(t("session_cmd.rename_empty"), "yellow")
            return CommandResult()

        if getattr(ctx.agent, "is_sensitive_session", False) is True:
            ctx.renderer.message(t("session_cmd.rename_sensitive"), "yellow")
            return CommandResult()

        clean_title = new_title.replace("\n", " ")[:60]
        from pocket_agent.core.sessions import rename_session, save_session

        # 若檔案已存在於磁碟則直接更新；否則以當前 history 儲存帶標題之 session
        renamed = rename_session(ctx.state.session_id, clean_title)
        if not renamed:
            save_session(ctx.state.session_id, ctx.agent.history, title=clean_title)

        ctx.renderer.message(t("session_cmd.rename_done", title=clean_title), "green")
        return CommandResult()


class ActivityCommand(CliCommand):
    """列出本次會話中新增、修改、刪除的檔案清單，以及上網查詢的完整網址與狀態清單。"""

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args.strip():
            raise ValueError(t("session_cmd.activity_no_args"))

        from pocket_agent.core.activity import SessionActivityTracker

        tracker = getattr(ctx.agent, "activity", None)
        if tracker is None:
            tracker = SessionActivityTracker()
            ctx.agent.activity = tracker

        if hasattr(tracker, "ensure_rebuilt"):
            tracker.ensure_rebuilt(getattr(ctx.agent, "history", None))

        file_changes = tracker.get_file_changes(sync_disk=True)
        url_items = tracker.get_url_activities()

        console = ctx.renderer.console
        console.print()

        # ── 區塊一：本次會話檔案異動清單 ─────────────────────────────
        console.print(Text(t("session_cmd.activity_files_header"), style="bold cyan"))
        total_files = sum(len(v) for v in file_changes.values())
        if total_files == 0:
            console.print(Text(t("session_cmd.activity_files_empty"), style="dim"))
        else:
            categories = (
                ("added", t("session_cmd.activity_added"), "+", "bold green"),
                ("modified", t("session_cmd.activity_modified"), "~", "bold yellow"),
                ("deleted", t("session_cmd.activity_deleted"), "-", "bold red"),
            )
            for key, label, symbol, color in categories:
                paths = file_changes.get(key, [])
                header = Text("  ")
                header.append(f"[{label}]", style=color)
                header.append(f" ({len(paths)})", style="dim")
                console.print(header)
                if not paths:
                    console.print(Text(t("session_cmd.activity_none"), style="dim"))
                else:
                    for p in paths:
                        line = Text("    ")
                        line.append(f"{symbol} ", style=color)
                        line.append(p)
                        console.print(line, soft_wrap=True, overflow="ignore", crop=False)

        console.print()

        # ── 區塊二：本次會話上網查詢網址清單 ─────────────────────────
        console.print(Text(t("session_cmd.activity_urls_header"), style="bold magenta"))
        if not url_items:
            console.print(Text(t("session_cmd.activity_urls_empty"), style="dim"))
        else:
            for idx, item in enumerate(url_items, 1):
                is_ok = item.status == "success"
                status_label = t("session_cmd.activity_url_ok") if is_ok else t("session_cmd.activity_url_fail")
                status_style = "bold green" if is_ok else "bold red"
                line = Text(f"  {idx}. ")
                line.append(status_label, style=status_style)
                line.append(f" {item.url}")
                if item.count > 1:
                    line.append(t("session_cmd.activity_url_count", count=item.count), style="dim")
                console.print(line, soft_wrap=True, overflow="ignore", crop=False)

        console.print()
        return CommandResult()

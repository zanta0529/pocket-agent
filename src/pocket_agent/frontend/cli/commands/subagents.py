from rich.status import Status
from rich.text import Text

from pocket_agent.frontend.cli.commands.core import CliCommand, CommandContext, CommandResult
from pocket_agent.system.constants import DEFAULT_BG_JOB_TASK_PREVIEW_CHARS
from pocket_agent.system.i18n import t


class AgentsCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError(t("subagent_cmd.agents_no_args"))
        from pocket_agent.core.subagents import list_subagents

        sa_list = list_subagents()
        if not sa_list:
            ctx.renderer.message(t("subagent_cmd.no_subagents"))
            return CommandResult()

        ctx.renderer.console.print()
        header = Text("Available Subagents\n", style="bold magenta")
        header.append(f"{len(sa_list)} subagents registered", style="dim")
        ctx.renderer.console.print(header)
        ctx.renderer.console.print()

        for sa in sa_list:
            title = Text(f"🤖 {sa['name']}", style="bold cyan")
            ctx.renderer.console.print(title)
            ctx.renderer.console.print(f"   [dim]{t('subagent_cmd.label_desc')}[/dim] {sa['description']}")
            if sa.get("model"):
                ctx.renderer.console.print(f"   [dim]{t('subagent_cmd.label_model')}[/dim] {sa['model']}")
            if sa.get("allowed_tools"):
                tools_str = ", ".join(sa["allowed_tools"])
                ctx.renderer.console.print(f"   [dim]{t('subagent_cmd.label_tools')}[/dim] {tools_str}")
            else:
                ctx.renderer.console.print(
                    f"   [dim]{t('subagent_cmd.label_tools')}[/dim] {t('subagent_cmd.tools_unlimited')}"
                )

            rounds = sa.get("max_tool_rounds") or ctx.agent.max_tool_rounds
            ctx.renderer.console.print(f"   [dim]{t('subagent_cmd.label_max_rounds')}[/dim] {rounds}\n")

        return CommandResult()


class SubagentCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from pocket_agent.core.subagents import list_subagents

        if not args:
            sa_list = list_subagents()
            if not sa_list:
                raise ValueError(t("subagent_cmd.no_subagents"))

            choices = [sa["name"] for sa in sa_list]
            subagent_name = await ctx.reader.choose(t("subagent_cmd.choose_subagent"), choices)
            if not subagent_name:
                return CommandResult("handled")

            ctx.renderer.console.print(f"[bold cyan]{t('subagent_cmd.selected_subagent')}[/bold cyan] {subagent_name}")
            task = await ctx.reader.read(message=t("subagent_cmd.task_prompt"))
            if not task:
                return CommandResult("handled")
            display_text = f"/subagent {subagent_name} {task}"
        else:
            parts = args.split(maxsplit=1)
            subagent_name = parts[0]
            task = parts[1] if len(parts) > 1 else t("subagent_cmd.default_task")
            display_text = f"/subagent {args}"

        return await execute_subagent_flow(ctx, subagent_name, task, display_text)


async def execute_subagent_flow(ctx: CommandContext, subagent_name: str, task: str, display_text: str) -> CommandResult:
    ctx.renderer.user(display_text)

    try:
        status = Status(
            t("subagent_cmd.running", name=subagent_name),
            spinner="dots",
            console=ctx.renderer.console,
        )
        ctx.renderer._active_status = status
        status.start()
        try:
            reply, require_save = await ctx.agent.run_subagent_turn(subagent_name, task)
        finally:
            ctx.renderer._stop_status()

        ctx.renderer.reply(reply, 0, 0, streamed=False)

        if require_save:
            auto_prompt = ctx.agent.build_save_report_prompt(is_parallel=False).lstrip()
            from pocket_agent.frontend.cli.main import send_turn

            await send_turn(
                ctx.agent,
                ctx.state,
                ctx.renderer,
                t("subagent_cmd.auto_save_display"),
                CommandResult("chat", prompt=auto_prompt),
            )
    except KeyboardInterrupt:
        ctx.agent.request_stop()
        ctx.state.last_failed_input = display_text
        ctx.renderer.message(t("cli.stopped"), "yellow")
    except Exception as exc:
        ctx.state.last_failed_input = display_text
        ctx.renderer._ensure_newline()
        ctx.renderer._stop_status()

        from rich.markdown import Markdown
        from rich.panel import Panel

        err_text = t("subagent_cmd.failed", name=subagent_name, error=exc)

        panel = Panel(
            Markdown(err_text),
            title=t("subagent_cmd.error_title"),
            title_align="left",
            border_style="bold red",
            expand=False,
        )
        ctx.renderer.console.print()
        ctx.renderer.console.print(panel)
        ctx.renderer.message(t("subagent_cmd.partial_warning"), "yellow")

    return CommandResult("handled")


class ParallelCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if not args:
            raise ValueError(t("subagent_cmd.parallel_usage"))

        tasks = []
        for part in args.split("|"):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                sub_name, sub_task = part.split(":", 1)
                tasks.append({"name": sub_name.strip(), "task": sub_task.strip()})
            else:
                raise ValueError(t("subagent_cmd.parallel_format_error", part=part))

        if not tasks:
            raise ValueError(t("subagent_cmd.parallel_empty"))

        display_text = f"/parallel {args}"
        ctx.renderer.user(display_text)

        try:
            from pocket_agent.core.subagents import build_subagent_labels
            from pocket_agent.frontend.cli.render import MultiStatus

            task_names = build_subagent_labels(tasks)
            status = MultiStatus(
                t("subagent_cmd.parallel_running", count=len(tasks)),
                task_names,
                console=ctx.renderer.console,
            )
            ctx.renderer._active_status = status
            status.start()

            def _on_summarizing() -> None:
                ctx.renderer._stop_status()
                sum_status = Status(
                    t("subagent_cmd.parallel_summarizing"),
                    spinner="dots",
                    console=ctx.renderer.console,
                )
                ctx.renderer._active_status = sum_status
                sum_status.start()

            try:
                reply, require_save = await ctx.agent.run_parallel_subagents(
                    tasks,
                    on_summarizing=_on_summarizing,
                    user_prompt=display_text,
                )
            finally:
                ctx.renderer._stop_status()

            ctx.renderer.reply(reply, 0, 0, streamed=False)

            if require_save:
                auto_prompt = ctx.agent.build_save_report_prompt(is_parallel=True).lstrip()
                from pocket_agent.frontend.cli.main import send_turn

                await send_turn(
                    ctx.agent,
                    ctx.state,
                    ctx.renderer,
                    t("subagent_cmd.auto_save_display"),
                    CommandResult("chat", prompt=auto_prompt),
                )
        except KeyboardInterrupt:
            ctx.agent.request_stop()
            ctx.state.last_failed_input = display_text
            ctx.renderer.message(t("cli.stopped"), "yellow")

        return CommandResult("handled")


def _sync_bg_counts(ctx: CommandContext) -> None:
    if callable(getattr(type(ctx.agent), "background_job_counts", None)):
        counts = ctx.agent.background_job_counts()
        if isinstance(counts, tuple) and len(counts) == 2:
            ctx.state.bg_jobs_running, ctx.state.bg_jobs_done = counts
    from pocket_agent.core.subagents import BackgroundSubagentManager

    bg_mgr = getattr(ctx.agent, "background_jobs", None)
    if isinstance(bg_mgr, BackgroundSubagentManager):
        ctx.state.bg_job_summaries = bg_mgr.summarize_active_jobs()


class BgCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from pocket_agent.core.subagents import list_subagents

        if not args.strip():
            sa_list = list_subagents()
            if not sa_list:
                raise ValueError(t("subagent_cmd.no_subagents"))
            choices = [sa["name"] for sa in sa_list]
            subagent_name = await ctx.reader.choose(t("subagent_cmd.choose_subagent"), choices)
            if not subagent_name:
                return CommandResult("handled")
            ctx.renderer.console.print(f"[bold cyan]{t('subagent_cmd.selected_subagent')}[/bold cyan] {subagent_name}")
            task = await ctx.reader.read(message=t("subagent_cmd.task_prompt"))
            if not task or not task.strip():
                return CommandResult("handled")
        else:
            parts = args.strip().split(maxsplit=1)
            subagent_name = parts[0]
            task = parts[1] if len(parts) > 1 else t("subagent_cmd.default_task")

        job = ctx.agent.start_background_subagent(subagent_name, task)
        _sync_bg_counts(ctx)
        ctx.renderer.message(
            t("subagent_cmd.bg_started", job_id=job.job_id, name=job.subagent_name),
            "green",
        )
        return CommandResult("handled")


_TERMINAL_PROGRESS_LABELS = frozenset({"completed", "failed", "cancelled"})


def _summarize_task(task: str, max_chars: int = DEFAULT_BG_JOB_TASK_PREVIEW_CHARS) -> str:
    """壓縮空白並將背景任務描述截斷為單行摘要。"""
    clean = " ".join((task or "").split())
    if not clean or len(clean) <= max_chars:
        return clean
    return f"{clean[: max(1, max_chars - 1)].rstrip()}…"


def _job_status_meta(status: str) -> tuple[str, str]:
    """回傳背景工作狀態對應的圖示與本地化標籤。"""
    status_meta = {
        "running": ("⏳", t("subagent_cmd.job_status_running")),
        "completed": ("✓", t("subagent_cmd.job_status_completed")),
        "failed": ("✗", t("subagent_cmd.job_status_failed")),
        "cancelled": ("⏹", t("subagent_cmd.job_status_cancelled")),
    }
    return status_meta.get(status, ("•", status))


def _format_job_choice(job) -> str:
    """格式化互動選單用的單列背景工作選項。"""
    icon, status_label = _job_status_meta(job.status)
    collected_tag = t("subagent_cmd.jobs_collected_tag") if job.collected else ""
    snippet = _summarize_task(getattr(job, "task", ""))
    task_note = f' — "{snippet}"' if snippet else ""
    return f"{icon} {job.job_id} [{job.subagent_name}] {status_label}{collected_tag}{task_note}"


def _collect_and_render_job(ctx: CommandContext, job_id: str) -> None:
    """取回單一背景工作並注入主對話歷史後輸出摘要。"""
    collected = ctx.agent.collect_background_job(job_id, inject_history=True)
    body = collected.result if collected.status == "completed" else (collected.error or collected.result)
    ctx.renderer.reply(
        body or t("subagent_cmd.jobs_no_output"),
        collected.elapsed_seconds(),
        0,
        streamed=False,
    )


def _format_job_line(job) -> tuple[str, str | None]:
    """格式化 /jobs 單列輸出並回傳 (文字, Rich 樣式)。"""
    icon, status_label = _job_status_meta(job.status)
    prog = ""
    if job.status == "running" and job.last_progress:
        if job.last_progress.strip().lower() not in _TERMINAL_PROGRESS_LABELS:
            prog = f" · {job.last_progress}"
    collected_tag = t("subagent_cmd.jobs_collected_tag") if job.collected else ""
    snippet = _summarize_task(getattr(job, "task", ""))
    task_note = f' — "{snippet}"' if snippet else ""
    line = (
        f"  {icon} {job.job_id} [{job.subagent_name}] {status_label}"
        f" ({job.elapsed_seconds():.1f}s){collected_tag}{prog}{task_note}"
    )
    if job.status != "running" and job.collected:
        return line, "dim"
    if job.status == "failed":
        return line, "red"
    if job.status == "completed":
        return line, "green"
    return line, None


def _build_jobs_hint(
    *,
    has_running: bool,
    has_uncollected: bool,
    has_finished: bool,
    has_archived_hidden: bool,
    has_any_shown: bool,
) -> str:
    """依目前工作清單狀態動態組合 /jobs 底部提示。"""
    parts: list[str] = []
    if has_any_shown:
        parts.append(t("subagent_cmd.jobs_hint_show"))
    if has_uncollected:
        parts.append(t("subagent_cmd.jobs_hint_collect"))
    if has_running:
        parts.append(t("subagent_cmd.jobs_hint_cancel"))
    if has_archived_hidden:
        parts.append(t("subagent_cmd.jobs_hint_all"))
    if has_finished:
        parts.append(t("subagent_cmd.jobs_hint_clear"))
    if not parts:
        return t("subagent_cmd.jobs_hint")
    return t("subagent_cmd.jobs_hint_prefix") + " · ".join(parts)


class JobsCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        raw = args.strip()
        lower_raw = raw.lower()
        if not raw or lower_raw in ("list", "all", "history"):
            show_all = lower_raw in ("all", "history")
            jobs = ctx.agent.list_background_jobs()
            if not jobs:
                ctx.renderer.message(t("subagent_cmd.jobs_empty"))
                return CommandResult("handled")

            active_jobs = [j for j in jobs if j.status == "running" or not j.collected]
            archived_jobs = [j for j in jobs if j.status != "running" and j.collected]
            has_running = any(j.status == "running" for j in jobs)
            has_uncollected = any(j.status != "running" and not j.collected for j in jobs)
            has_finished = any(j.status != "running" for j in jobs)

            if not show_all and not active_jobs:
                ctx.renderer.message(t("subagent_cmd.jobs_archived_only", archived=len(archived_jobs)), "dim")
                ctx.renderer.message(
                    _build_jobs_hint(
                        has_running=False,
                        has_uncollected=False,
                        has_finished=True,
                        has_archived_hidden=True,
                        has_any_shown=False,
                    ),
                    "dim",
                )
                return CommandResult("handled")

            display_jobs = jobs if show_all else active_jobs
            if show_all:
                ctx.renderer.message(t("subagent_cmd.jobs_header_all", count=len(jobs)), "bold cyan")
            else:
                ctx.renderer.message(
                    t("subagent_cmd.jobs_header_active", active=len(active_jobs), total=len(jobs)),
                    "bold cyan",
                )

            for job in display_jobs:
                line, style = _format_job_line(job)
                ctx.renderer.message(line, style or "")

            archived_hidden = not show_all and bool(archived_jobs)
            if archived_hidden:
                ctx.renderer.message(t("subagent_cmd.jobs_hidden_archived", archived=len(archived_jobs)), "dim")
            ctx.renderer.message(
                _build_jobs_hint(
                    has_running=has_running,
                    has_uncollected=has_uncollected,
                    has_finished=has_finished,
                    has_archived_hidden=archived_hidden,
                    has_any_shown=True,
                ),
                "dim",
            )
            return CommandResult("handled")

        parts = raw.split(maxsplit=1)
        sub_cmd = parts[0].lower()
        target = parts[1].strip() if len(parts) > 1 else ""

        if sub_cmd == "clear":
            if target:
                raise ValueError(t("subagent_cmd.jobs_usage"))
            removed = ctx.agent.clear_finished_background_jobs()
            _sync_bg_counts(ctx)
            if removed > 0:
                ctx.renderer.message(t("subagent_cmd.jobs_cleared", count=removed), "green")
            else:
                ctx.renderer.message(t("subagent_cmd.jobs_clear_empty"))
            return CommandResult("handled")

        if sub_cmd == "cancel":
            if not target:
                raise ValueError(t("subagent_cmd.jobs_cancel_usage"))
            if target.lower() == "all":
                cancelled = await ctx.agent.cancel_all_background_jobs()
                _sync_bg_counts(ctx)
                ctx.renderer.message(t("subagent_cmd.jobs_cancelled_all", count=cancelled), "yellow")
                return CommandResult("handled")
            ok = await ctx.agent.cancel_background_job(target)
            _sync_bg_counts(ctx)
            if ok:
                ctx.renderer.message(t("subagent_cmd.jobs_cancelled", job_id=target), "yellow")
            else:
                ctx.renderer.message(t("subagent_cmd.jobs_not_running", job_id=target), "yellow")
            return CommandResult("handled")

        if sub_cmd == "collect":
            if target.lower() == "all":
                ready_jobs = [j for j in ctx.agent.list_background_jobs() if j.status != "running" and not j.collected]
                if not ready_jobs:
                    ctx.renderer.message(t("subagent_cmd.jobs_none_ready"))
                    return CommandResult("handled")
                for r_job in ready_jobs:
                    _collect_and_render_job(ctx, r_job.job_id)
                _sync_bg_counts(ctx)
                return CommandResult("handled")

            if target:
                job = ctx.agent.get_background_job(target)
                if job is None:
                    raise ValueError(t("subagent_cmd.jobs_not_found", job_id=target))
                if job.status == "running":
                    ctx.renderer.message(t("subagent_cmd.jobs_still_running", job_id=target), "yellow")
                    return CommandResult("handled")
                _collect_and_render_job(ctx, target)
                _sync_bg_counts(ctx)
                return CommandResult("handled")

            ready_jobs = [j for j in ctx.agent.list_background_jobs() if j.status != "running" and not j.collected]
            if not ready_jobs:
                ctx.renderer.message(t("subagent_cmd.jobs_none_ready"))
                return CommandResult("handled")

            choice_map = {_format_job_choice(j): j for j in ready_jobs}
            choices = list(choice_map.keys())
            collect_all_label = ""
            if len(ready_jobs) > 1:
                collect_all_label = t("subagent_cmd.jobs_collect_all_choice", count=len(ready_jobs))
                choices.append(collect_all_label)

            selected = await ctx.reader.choose(t("subagent_cmd.jobs_choose_collect"), choices)
            if not selected:
                return CommandResult("handled")
            if collect_all_label and selected == collect_all_label:
                for r_job in ready_jobs:
                    _collect_and_render_job(ctx, r_job.job_id)
                _sync_bg_counts(ctx)
                return CommandResult("handled")
            picked = choice_map.get(selected)
            if picked is not None:
                _collect_and_render_job(ctx, picked.job_id)
                _sync_bg_counts(ctx)
            return CommandResult("handled")

        if sub_cmd == "show" and not target:
            all_jobs = ctx.agent.list_background_jobs()
            if not all_jobs:
                ctx.renderer.message(t("subagent_cmd.jobs_empty"))
                return CommandResult("handled")
            ordered_jobs = sorted(all_jobs, key=lambda j: j.status != "running" and j.collected)
            choice_map = {_format_job_choice(j): j for j in ordered_jobs}
            selected = await ctx.reader.choose(t("subagent_cmd.jobs_choose_show"), list(choice_map.keys()))
            if not selected or selected not in choice_map:
                return CommandResult("handled")
            job_id = choice_map[selected].job_id
        else:
            job_id = target if sub_cmd == "show" else raw
            if sub_cmd == "show" and not job_id:
                raise ValueError(t("subagent_cmd.jobs_usage"))
        job = ctx.agent.get_background_job(job_id)
        if job is None:
            raise ValueError(t("subagent_cmd.jobs_not_found", job_id=job_id))

        ctx.renderer.message(
            f"{job.job_id} [{job.subagent_name}] {job.status} ({job.elapsed_seconds():.1f}s)",
            "bold cyan",
        )
        if job.status == "running":
            if job.last_progress:
                ctx.renderer.message(f"  {job.last_progress}")
            return CommandResult("handled")
        body = job.result if job.status == "completed" else (job.error or job.result)
        if body:
            ctx.renderer.reply(body, job.elapsed_seconds(), 0, streamed=False)
        else:
            ctx.renderer.message(t("subagent_cmd.jobs_no_output"))
        return CommandResult("handled")

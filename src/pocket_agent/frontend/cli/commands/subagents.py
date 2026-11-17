from rich.status import Status
from rich.text import Text

from pocket_agent.frontend.cli.commands.core import CliCommand, CommandContext, CommandResult
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
            from pocket_agent.frontend.cli.render import MultiStatus

            task_names = [task_item["name"] for task_item in tasks]
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

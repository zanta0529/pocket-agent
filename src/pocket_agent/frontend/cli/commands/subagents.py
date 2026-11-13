from rich.status import Status
from rich.text import Text

from pocket_agent.frontend.cli.commands.core import CliCommand, CommandContext, CommandResult


class AgentsCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError("/agents 不接受參數")
        from pocket_agent.core.subagents import list_subagents

        sa_list = list_subagents()
        if not sa_list:
            ctx.renderer.message("目前沒有可用的子代理。")
            return CommandResult()

        ctx.renderer.console.print()
        header = Text("Available Subagents\n", style="bold magenta")
        header.append(f"{len(sa_list)} subagents registered", style="dim")
        ctx.renderer.console.print(header)
        ctx.renderer.console.print()

        for sa in sa_list:
            title = Text(f"🤖 {sa['name']}", style="bold cyan")
            ctx.renderer.console.print(title)
            ctx.renderer.console.print(f"   [dim]描述:[/dim] {sa['description']}")
            if sa.get("model"):
                ctx.renderer.console.print(f"   [dim]模型:[/dim] {sa['model']}")
            if sa.get("allowed_tools"):
                tools_str = ", ".join(sa["allowed_tools"])
                ctx.renderer.console.print(f"   [dim]工具權限:[/dim] {tools_str}")
            else:
                ctx.renderer.console.print("   [dim]工具權限:[/dim] 全開 (無限制)")

            rounds = sa.get("max_tool_rounds") or ctx.agent.max_tool_rounds
            ctx.renderer.console.print(f"   [dim]最大輪數:[/dim] {rounds}\n")

        return CommandResult()


class SubagentCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from pocket_agent.core.subagents import list_subagents

        if not args:
            sa_list = list_subagents()
            if not sa_list:
                raise ValueError("目前沒有可用的子代理。")

            choices = [sa["name"] for sa in sa_list]
            subagent_name = await ctx.reader.choose("請選擇要委派的子代理", choices)
            if not subagent_name:
                return CommandResult("handled")

            ctx.renderer.console.print(f"[bold cyan]已選擇子代理：[/bold cyan] {subagent_name}")
            task = await ctx.reader.read(message="👉 請輸入要交辦的任務描述 (Esc+Enter 換行, Enter 送出): ")
            if not task:
                return CommandResult("handled")
            display_text = f"/subagent {subagent_name} {task}"
        else:
            parts = args.split(maxsplit=1)
            subagent_name = parts[0]
            task = parts[1] if len(parts) > 1 else "請執行你的預設任務"
            display_text = f"/subagent {args}"

        return await execute_subagent_flow(ctx, subagent_name, task, display_text)


async def execute_subagent_flow(ctx: CommandContext, subagent_name: str, task: str, display_text: str) -> CommandResult:
    ctx.renderer.user(display_text)

    try:
        status = Status(f"系統路由：正在執行子代理 `{subagent_name}`...", spinner="dots", console=ctx.renderer.console)
        ctx.renderer._active_status = status
        status.start()
        try:
            reply, require_save = await ctx.agent.run_subagent_turn(subagent_name, task)
        finally:
            ctx.renderer._stop_status()

        ctx.renderer.reply(reply, 0, 0, streamed=False)

        if require_save:
            auto_prompt = ctx.agent._build_save_report_prompt(is_parallel=False).lstrip()
            from pocket_agent.frontend.cli.main import send_turn

            await send_turn(
                ctx.agent,
                ctx.state,
                ctx.renderer,
                "(系統自動觸發存檔流程)",
                CommandResult("chat", prompt=auto_prompt),
            )
    except KeyboardInterrupt:
        ctx.agent.request_stop()
        ctx.state.last_failed_input = display_text
        ctx.renderer.message("⏹ 已停止", "yellow")
    except Exception as exc:
        ctx.state.last_failed_input = display_text
        ctx.renderer._ensure_newline()
        ctx.renderer._stop_status()

        from rich.markdown import Markdown
        from rich.panel import Panel

        err_text = f"子代理「{subagent_name}」執行失敗：{exc}"

        panel = Panel(
            Markdown(err_text),
            title="⚠️ 本輪未完成 (錯誤)",
            title_align="left",
            border_style="bold red",
            expand=False,
        )
        ctx.renderer.console.print()
        ctx.renderer.console.print(panel)
        ctx.renderer.message("待送附件保留；本輪可能部分完成，重送前請檢查結果。未自動重試。", "yellow")

    return CommandResult("handled")


class ParallelCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if not args:
            raise ValueError(
                "用法: /parallel <子代理1>:<任務1> | <子代理2>:<任務2>\n"
                "範例: /parallel researcher:查市場趨勢 | coder:寫分析腳本\n"
                "提示: 請使用管線符號 '|' 來分隔不同子代理的任務。"
            )

        tasks = []
        for part in args.split("|"):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                sub_name, sub_task = part.split(":", 1)
                tasks.append({"name": sub_name.strip(), "task": sub_task.strip()})
            else:
                raise ValueError(f"平行任務格式錯誤，缺少冒號 (子代理名稱:任務內容) => 錯誤段落: {part}")

        if not tasks:
            raise ValueError("解析不到有效的平行任務。請確保使用管線符號 '|' 分隔。")

        display_text = f"/parallel {args}"
        ctx.renderer.user(display_text)
        ctx.agent.history.append({"role": "user", "content": display_text})

        try:
            from pocket_agent.frontend.cli.render import MultiStatus

            task_names = [t["name"] for t in tasks]
            status = MultiStatus(
                f"系統路由：正在平行執行 {len(tasks)} 個子代理...", task_names, console=ctx.renderer.console
            )
            ctx.renderer._active_status = status
            status.start()

            def _on_summarizing() -> None:
                ctx.renderer._stop_status()
                sum_status = Status("系統路由：正在彙整多份子代理報告...", spinner="dots", console=ctx.renderer.console)
                ctx.renderer._active_status = sum_status
                sum_status.start()

            try:
                reply, require_save = await ctx.agent.run_parallel_subagents(tasks, on_summarizing=_on_summarizing)
            finally:
                ctx.renderer._stop_status()

            ctx.renderer.reply(reply, 0, 0, streamed=False)

            if require_save:
                auto_prompt = ctx.agent._build_save_report_prompt(is_parallel=True).lstrip()
                from pocket_agent.frontend.cli.main import send_turn

                await send_turn(
                    ctx.agent,
                    ctx.state,
                    ctx.renderer,
                    "(系統自動觸發存檔流程)",
                    CommandResult("chat", prompt=auto_prompt),
                )
        except KeyboardInterrupt:
            ctx.agent.request_stop()
            ctx.state.last_failed_input = display_text
            ctx.renderer.message("⏹ 已停止", "yellow")

        return CommandResult("handled")

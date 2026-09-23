import asyncio

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
    from pocket_agent.core.subagents import get_subagent

    ctx.renderer.user(display_text)

    ctx.agent.history.append({"role": "user", "content": f"/{subagent_name} {task}"})
    try:
        status = Status(f"系統路由：正在執行子代理 `{subagent_name}`...", spinner="dots", console=ctx.renderer.console)
        ctx.renderer._active_status = status
        status.start()
        try:
            reply = await ctx.agent._run_subagent({"name": subagent_name, "task": task})
        finally:
            ctx.renderer._stop_status()

        wrapped_reply = (
            f"[系統備註：以下報告由系統背景調度之子代理 ({subagent_name}) 獨立執行後回傳，為合法可信來源。]\n\n{reply}"
        )
        ctx.agent.history.append({"role": "assistant", "content": wrapped_reply})
        ctx.renderer.reply(reply, 0, 0, streamed=False)

        sa_def = get_subagent(subagent_name)
        if sa_def and sa_def.get("post_actions", {}).get("require_save_prompt"):
            auto_prompt = (
                "[系統指示 (System Instruction)]\n"
                "上述子任務已完成。根據系統設定，你現在**必須**根據該報告內容，"
                "為這份報告想一個最合適的動態檔名（例如 docs/xxx-analysis.md），"
                "並立刻呼叫 `ask_question` 詢問使用者是否要將這份報告存檔。"
                "選項請提供：「(Recommended) 存檔至建議路徑」與「不存檔，僅保留於對話」。"
            )
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
            try:
                coros = [ctx.agent._run_subagent(t) for t in tasks]
                results = await asyncio.gather(*coros, return_exceptions=True)

                combined = []
                for idx, res in enumerate(results):
                    t_name = tasks[idx]["name"]
                    t_task = tasks[idx]["task"]
                    if isinstance(res, Exception):
                        combined.append(f"【子代理 {t_name} (任務: {t_task}) 錯誤】\n{res}")
                    else:
                        combined.append(f"【子代理 {t_name} 回報 (任務: {t_task})】\n{res}")
                raw_combined = "\n\n---\n\n".join(combined)
            finally:
                ctx.renderer._stop_status()

            status = Status("系統路由：正在彙整多份子代理報告...", spinner="dots", console=ctx.renderer.console)
            ctx.renderer._active_status = status
            status.start()
            try:
                from pocket_agent.core.agent import Agent

                summarizer = Agent(
                    client=ctx.agent.client,
                    mcp=ctx.agent.mcp,
                    model=ctx.agent.model,
                    system_prompt=(
                        "你是一個專業的報告彙整助手。請閱讀以下由多個平行子代理回傳的獨立原始報告，"
                        "並為使用者彙整成一份結構清晰、去蕪存菁的最終總結報告。請直接輸出報告內容，不需多餘的問候語。"
                    ),
                    is_subagent=True,
                    reasoning_effort="none",
                )
                try:
                    _, reply, _ = await summarizer.chat(raw_combined)
                    if not reply or not reply.strip():
                        raise ValueError("彙整回傳為空")
                except Exception as e:
                    reply = f"*(彙整失敗，以下為原始報告：{e})*\n\n" + raw_combined
            finally:
                ctx.renderer._stop_status()

            wrapped_reply = (
                f"[系統備註：以下報告由系統背景調度之多位子代理平行研究後自動彙整而成，為合法可信來源。]\n\n{reply}"
            )
            ctx.agent.history.append({"role": "assistant", "content": wrapped_reply})
            ctx.renderer.reply(reply, 0, 0, streamed=False)

            require_save = False
            from pocket_agent.core.subagents import get_subagent

            for t in tasks:
                sa_def = get_subagent(t["name"])
                if sa_def and sa_def.get("post_actions", {}).get("require_save_prompt"):
                    require_save = True
                    break

            if require_save:
                auto_prompt = (
                    "[系統指示 (System Instruction)]\n"
                    "上述平行子任務已完成並彙整。根據系統設定，你現在**必須**根據該報告內容，"
                    "為這份報告想一個最合適的動態檔名（例如 docs/xxx-analysis.md），"
                    "並立刻呼叫 `ask_question` 詢問使用者是否要將這份報告存檔。"
                    "選項請提供：「(Recommended) 存檔至建議路徑」與「不存檔，僅保留於對話」。"
                )
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

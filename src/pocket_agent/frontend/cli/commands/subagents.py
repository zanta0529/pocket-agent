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
            display_text = None

        prompt = (
            f"指令要求：請「直接且立即」使用 `run_subagent` 工具，委派 `{subagent_name}` 子代理來處理以下任務。\n"
            f"（注意：系統已確認該代理存在，請勿自行尋找、讀取或檢查任何子代理設定檔！）\n\n"
            f"任務內容：\n{task}"
        )
        return CommandResult("chat", prompt, display_text)


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

        return CommandResult("direct_subagents_parallel", prompt=args, display_text=None, extra={"tasks": tasks})

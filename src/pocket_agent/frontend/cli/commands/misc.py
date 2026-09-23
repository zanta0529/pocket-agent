from pathlib import Path

from rich.markdown import Markdown
from rich.text import Text

from pocket_agent.frontend.cli.commands.core import CliCommand, CommandContext, CommandResult
from pocket_agent.frontend.cli.input import BUILTINS, file_argument


class QuitCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError("/quit 不接受參數")
        return CommandResult("quit")


class LogoutCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError("/logout 不接受參數")
        from pocket_agent.system.auth import logout

        logout()
        return CommandResult("quit")


class ClearCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError("/clear 不接受參數")
        ctx.agent.clear_history()
        ctx.renderer.message("已清除對話；待送附件保留。")
        return CommandResult()


class FileCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        for path in ctx.state.add_files([file_argument(args)]):
            ctx.renderer.message(f"已加入待送：{path}")
        return CommandResult()


class AttachmentsCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        parts = args.split()
        if not parts:
            if not ctx.state.pending_files:
                ctx.renderer.message("沒有待送附件。")
            for i, path in enumerate(ctx.state.pending_files, 1):
                ctx.renderer.message(f"{i}. {path}")
        elif parts == ["clear"]:
            ctx.state.pending_files.clear()
            ctx.renderer.message("已清空待送佇列，未刪除實體檔案。")
        elif len(parts) == 2 and parts[0] == "remove":
            try:
                number = int(parts[1])
            except ValueError as exc:
                raise ValueError("附件編號必須是整數") from exc
            ctx.renderer.message(f"已移除待送附件：{ctx.state.remove(number)}")
        else:
            raise ValueError("用法：/attachments [clear|remove <編號>]")
        return CommandResult()


class ToolsCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError("/tools 不接受參數")
        ctx.renderer.message("MCP 工具：" + (", ".join(ctx.tools) or "無"))
        return CommandResult()


class SkillsCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args and args.strip() == "":
            raise ValueError("/skills 參數錯誤")

        from pocket_agent.extensions.skills import list_skills, load_skill
        from pocket_agent.system.paths import bundle_dir, user_dir

        if args:
            skill_name = args.strip()
            body = load_skill(skill_name, ctx.agent.workspace_dirs)
            if body:
                ctx.renderer.console.print(f"\n[bold white]Skill: {skill_name}[/bold white]")
                ctx.renderer.console.print(Markdown(body))
            else:
                ctx.renderer.message(f"無法載入技能 {skill_name}")
            return CommandResult()

        skills = list_skills(ctx.agent.workspace_dirs)
        if not skills:
            ctx.renderer.message("目前沒有可用的全域或專案技能。")
            return CommandResult()

        ctx.renderer.console.print()
        header = Text("Skills\n", style="bold magenta")
        header.append(f"{len(skills)} skills", style="dim")
        ctx.renderer.console.print(header)
        ctx.renderer.console.print()

        create = Text("Create new skills\n", style="bold cyan")
        create.append("  Workspace: ", style="dim")
        create.append(f"{Path.cwd().resolve().as_posix()}/.agents/skills/{{skill_name}}/SKILL.md\n", style="blue")
        create.append("  Global:    ", style="dim")
        create.append("~/.pocket-agent/skills/{skill_name}/SKILL.md\n", style="blue")
        ctx.renderer.console.print(create)

        user_path = user_dir() / "skills"
        bundle_path = bundle_dir() / "skills"

        workspace_skills = []
        global_skills = []
        builtin_skills = []

        for s in skills:
            p = s["path"]
            try:
                p.relative_to(user_path)
                global_skills.append(s)
            except ValueError:
                try:
                    p.relative_to(bundle_path)
                    builtin_skills.append(s)
                except ValueError:
                    workspace_skills.append(s)

        def print_group(title, subtitle, items):
            if not items:
                return
            t = Text()
            t.append(f"{title} ", style="bold yellow")
            t.append(f"• {subtitle}", style="dim")
            ctx.renderer.console.print(t)
            for item in items:
                line = Text()
                line.append(f"  {item['name']}", style="blue")
                line.append(f": {item.get('description', '')}")
                ctx.renderer.console.print(line)
            ctx.renderer.console.print()

        print_group("Workspace skills", "Workspace config", workspace_skills)
        print_group("Global skills", "Global config", global_skills)
        print_group("Built-in skills", "Built-in", builtin_skills)

        ctx.renderer.message("提示：若要檢視某個技能的完整內容，請輸入 /skills <skill_name>", "dim")
        return CommandResult()


class HelpCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError("/help 不接受參數")
        from pocket_agent.system.commands import list_commands

        for command, description in BUILTINS.items():
            ctx.renderer.message(f"/{command}  {description}")
        if ctx.state.provider == "openai":
            ctx.renderer.message("/image <描述>  生圖，待送附件作參考圖")
        ctx.renderer.message("/attachments remove <編號> · /attachments clear")
        ctx.renderer.message("Enter 送出 · Esc 後 Enter 換行 · Ctrl+C 清空輸入 · 空輸入 Ctrl+D 離開")
        for command in list_commands():
            ctx.renderer.message("/" + command["name"] + "  " + command["description"])
        return CommandResult()


class PlanCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if not args:
            raise ValueError("用法：/plan <任務描述>")
        plan_prompt = (
            f"[System] 使用者啟動了 /plan 指令。請你為以下任務擬定一份詳細的實作計畫（Plan）。\n"
            f"在開始任何實際的檔案修改或指令執行之前，請你「必須」先使用 `ask_question` 工具將計畫摘要給使用者，"
            f"並提供「核准 (Approve)」與「拒絕 (Reject)」等選項，等待使用者同意後再繼續。\n\n"
            f"任務內容：\n{args}"
        )
        return CommandResult("chat", plan_prompt, display_text=f"/plan {args}")


class GoalCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if not args:
            raise ValueError("用法：/goal <長期目標描述>")
        goal_prompt = (
            f"[System] 使用者啟動了 /goal 指令。這是一個長期且可能包含多個步驟的目標。\n"
            f"請你獨立作業，持續使用工具直到達成目標為止。期間若遇到錯誤，請自行嘗試修復，"
            f"除非遇到需要人類授權或完全無法繞過的阻礙，否則請不要提早結束你的回應。\n\n"
            f"終極目標：\n{args}"
        )
        return CommandResult("chat", goal_prompt, display_text=f"/goal {args}")


class CodeSearchCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        import asyncio

        from rich.panel import Panel

        from pocket_agent.tools.rag import call_indexed_search

        query = args.strip()
        if not query:
            try:
                query = await ctx.reader.read("請輸入搜尋關鍵字（或按 Ctrl+C 取消）：")
                query = query.strip()
            except KeyboardInterrupt, EOFError:
                return CommandResult()
            if not query:
                return CommandResult()

        ctx.renderer.message(f"正在搜尋工作區：'{query}'...", "yellow")

        result = await asyncio.to_thread(call_indexed_search, {"query": query, "top_k": 5}, ctx.agent.workspace_dirs)

        clean_result = result
        if clean_result.startswith("<external_content>"):
            clean_result = clean_result.replace("<external_content>\n", "").replace("\n</external_content>", "")

        if "[ERROR]" in clean_result:
            ctx.renderer.console.print(Panel(clean_result, title="搜尋失敗", border_style="bold red"))
        else:
            ctx.renderer.console.print(Panel(clean_result, title="🔎 程式碼搜尋結果", border_style="bold green"))

            context_msg = (
                f"【系統提示】使用者剛剛使用了程式碼檢索功能，找到了以下關於 `{query}` 的片段。\n"
                "請在後續的回答、分析或修改中，將這些片段作為可信的參考資料：\n\n" + clean_result
            )
            ctx.agent.history.append({"role": "user", "content": context_msg})
            ctx.agent.history.append(
                {
                    "role": "assistant",
                    "content": "好的，我已經將這些程式碼片段載入記憶中。請告訴我接下來需要怎麼分析或處理它們？",
                }
            )

            ctx.renderer.message(
                "💡 (提示) 搜尋結果已自動加入上下文，您可以直接請 AI 對這些片段進行分析或修改。", "cyan"
            )

        return CommandResult()

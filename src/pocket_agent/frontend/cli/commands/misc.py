from pathlib import Path

from rich.markdown import Markdown
from rich.text import Text

from pocket_agent.frontend.cli.commands.core import CliCommand, CommandContext, CommandResult
from pocket_agent.frontend.cli.input import BUILTINS, file_argument

# /mcp sqlite <db_path> 所呼叫的 SQLite MCP 切換工具名稱
_SQLITE_SWITCH_TOOL = "sqlite_switch_database"


class QuitCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError("此指令不接受參數")
        return CommandResult("quit")


class UsageCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError("/usage 不接受參數")
        ctx.renderer.message(f"目前對話累計 Token 用量: {ctx.state.session_tokens}", "cyan")

        usage = getattr(ctx.agent, "context_usage", None)
        if usage is None:
            ctx.renderer.message("上下文：尚無資料（送出第一則訊息後即可顯示）。", "yellow")
        else:
            level_style = {"ok": "green", "warn": "yellow", "critical": "red"}[usage.level]
            for line in usage.display_lines():
                ctx.renderer.message(line, level_style)

        if hasattr(ctx.agent, "get_quota"):
            try:
                quota = await ctx.agent.get_quota()
                if quota and quota.is_supported:
                    for line in quota.get_display_lines():
                        ctx.renderer.message(line, "green")

                    # 同步更新狀態列 (只取大額度供精簡顯示)
                    ctx.state.quota_supported = True
                    ctx.state.quota_limit = quota.limit
                    ctx.state.quota_remaining = quota.remaining
                else:
                    msg = getattr(quota, "message", None) if quota else None
                    ctx.renderer.message(msg or "此 Provider 不支援透過 API 查詢額度。", "yellow")
            except Exception as e:
                ctx.renderer.message(f"查詢額度失敗: {e}", "red")
        else:
            ctx.renderer.message("此 Provider 或引擎尚未支援額度查詢。", "yellow")

        return CommandResult()


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


class CleanCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError("/clean 不接受參數")
        from pocket_agent.core.cancellation import run_sync
        from pocket_agent.system.housekeeping import default_housekeeper

        res = await run_sync(default_housekeeper.run_cleanup, force=True)
        files = res.get("cleaned_files", 0)
        mb = res.get("freed_mb", 0.0)
        if files > 0:
            ctx.renderer.message(
                f"✓ Housekeeping 完成：已清理 {files} 個過期檔案，釋放 {mb:.2f} MB 磁碟空間。",
                "green",
            )
        else:
            ctx.renderer.message("✓ 日誌與暫存目錄狀態良好，無過期檔案需清理。", "green")
        return CommandResult()


class UpdateCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args.strip():
            raise ValueError("/update 不接受參數")
        from pocket_agent.system.updater import get_current_build_info, update_manager

        if not update_manager.is_supported():
            ctx.renderer.message(
                "線上更新僅支援 Modern Portable 打包版；開發環境請直接使用 git pull 更新。",
                "yellow",
            )
            return CommandResult()

        staged = await update_manager.download_and_stage(
            on_status=lambda msg: ctx.renderer.message(msg, "cyan"),
        )
        if staged is None:
            curr = get_current_build_info()
            label = curr.commit[:7] or curr.version
            ctx.renderer.message(f"✓ 目前已是最新版本 ({label})。", "green")
            return CommandResult()

        ctx.state.staged_update_version = staged.short_sha
        ctx.state.pending_update_restart = True
        ctx.renderer.message(
            f"✓ 最新更新 ({staged.short_sha}) 已準備就緒，正在自動儲存對話並重啟套用…",
            "green",
        )
        return CommandResult("quit")


class FileCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from pathlib import Path

        path_str = file_argument(args)

        try:
            resolved_path = Path(path_str).expanduser().resolve(strict=True)
        except Exception:
            raise ValueError(f"找不到檔案：{path_str}")

        if not resolved_path.is_file():
            raise ValueError(f"不是一般檔案：{path_str}")

        status, msg, target_dir = ctx.agent.check_new_workspace_dir(str(resolved_path.parent))

        if status not in ("in_default", "already_authorized"):
            from rich.panel import Panel
            from rich.text import Text

            ctx.renderer._ensure_newline()
            panel = Panel(
                Text(f"檔案不在目前工作區內：\n  📄 {resolved_path}", style="yellow"),
                title="【安全警告】工作區權限請求",
                title_align="left",
                border_style="bold red",
                expand=False,
            )
            ctx.renderer.console.print(panel)
            try:
                ans = input("\n是否將它所在的資料夾加入工作區並掛載？[y/N] ").strip().lower()
                if ans == "y":
                    if target_dir:
                        ctx.agent.add_workspace_dir(target_dir)
                    ctx.renderer.message("✓ 已授權並加入附件。")
                else:
                    ctx.renderer.message("✗ 已取消加入。")
                    return CommandResult()
            except KeyboardInterrupt:
                ctx.renderer.message("\n✗ 已取消操作。")
                return CommandResult()
            except EOFError:
                ctx.renderer.message("\n✗ 已取消操作。")
                return CommandResult()

        for path in ctx.state.add_files([str(resolved_path)]):
            ctx.renderer.message(f"已加入待送：{path}")
        return CommandResult()


class RmFileCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if not args.strip():
            if not ctx.state.pending_files:
                ctx.renderer.message("待送區沒有任何附件可移除。")
                return CommandResult()

            choices = []
            for i, p in enumerate(ctx.state.pending_files, 1):
                choices.append(f"{i}. {p}")
            choices.append("取消")

            selected = await ctx.reader.choose("選擇要移除的待送附件", choices)
            if not selected or selected == "取消":
                ctx.renderer.message("已取消移除。")
                return CommandResult()

            idx = choices.index(selected) + 1
            removed = ctx.state.remove(idx)
            ctx.renderer.message(f"已移除待送附件：{removed}")
            return CommandResult()

        arg = args.strip()
        try:
            # 嘗試作為編號移除
            number = int(arg)
            removed = ctx.state.remove(number)
            ctx.renderer.message(f"已移除待送附件：{removed}")
            return CommandResult()
        except ValueError:
            pass

        # 作為路徑移除
        from pathlib import Path

        try:
            target = str(Path(arg).expanduser().resolve(strict=False))
        except Exception:
            raise ValueError(f"無效的路徑格式：{arg}")

        for i, p in enumerate(ctx.state.pending_files, 1):
            if str(Path(p).expanduser().resolve(strict=False)) == target:
                removed = ctx.state.remove(i)
                ctx.renderer.message(f"已移除待送附件：{removed}")
                return CommandResult()

        raise ValueError(f"在待送區中找不到該檔案：{arg}")


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


class McpCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        mcp_manager = getattr(ctx.agent, "mcp", None)
        grouped: dict[str, list[dict[str, str]]] = {}

        if mcp_manager is not None and hasattr(mcp_manager, "get_grouped_tools"):
            grouped = mcp_manager.get_grouped_tools()
        elif mcp_manager is not None and hasattr(mcp_manager, "_tools") and mcp_manager._tools:
            for entry in mcp_manager._tools.values():
                grouped.setdefault(entry.server_name, []).append(
                    {
                        "name": entry.name,
                        "description": (entry.schema.get("description") or "").strip(),
                    }
                )

        # Fallback: 若無 MCPManager 物件但有 ctx.tools (例如測試環境或直接傳遞)
        if not grouped and ctx.tools:
            for tool_name in ctx.tools:
                server_key = "mcp"
                if tool_name.startswith("mcp__") and len(tool_name.split("__")) >= 3:
                    server_key = tool_name.split("__")[1]
                else:
                    for prefix in ("rhel_", "win_rdp_", "browser_", "sqlite_", "mysql_", "mssql_"):
                        if tool_name.startswith(prefix):
                            server_key = prefix.rstrip("_").replace("_", "-")
                            break
                grouped.setdefault(server_key, []).append({"name": tool_name, "description": ""})

        if not grouped:
            ctx.renderer.message("目前未掛載任何 MCP 工具。")
            return CommandResult()

        arg = args.strip()
        if arg:
            # /mcp sqlite <db_path>：零 Token 直接切換 SQLite 資料庫檔案
            head, _, rest = arg.partition(" ")
            if head.lower() == "sqlite" and rest.strip():
                return await self._switch_sqlite(ctx, mcp_manager, rest.strip())

            # 支援查看特定 MCP 工具的詳細功能說明
            matched_server = None
            for s_name in grouped:
                if s_name.lower() == arg.lower():
                    matched_server = s_name
                    break
            if not matched_server:
                ctx.renderer.message(
                    f"找不到 MCP 工具「{arg}」。目前啟用的工具：{', '.join(grouped.keys())}",
                    "yellow",
                )
                return CommandResult()

            tools = grouped[matched_server]
            ctx.renderer.console.print()
            header = Text(f"📦 {matched_server}", style="bold cyan")
            header.append(f" (共 {len(tools)} 個功能)\n", style="dim")
            ctx.renderer.console.print(header)

            for t in tools:
                line = Text(f"  • {t['name']}", style="bold green")
                desc = t.get("description")
                if desc:
                    first_line = desc.split("\n")[0].strip()
                    line.append(f"\n    {first_line}", style="dim")
                ctx.renderer.console.print(line)
            ctx.renderer.console.print()
            self._print_sqlite_status(ctx, mcp_manager, matched_server)
            return CommandResult()

        # 預設無參數：以分組方式清晰呈現啟用的 MCP 工具與功能清單
        total_tools = sum(len(t) for t in grouped.values())
        ctx.renderer.console.print()
        header = Text("🔌 已啟用的 MCP 工具\n", style="bold magenta")
        header.append(f"{len(grouped)} 個服務已連線，共提供 {total_tools} 個功能", style="dim")
        ctx.renderer.console.print(header)
        ctx.renderer.console.print()

        raw_width = getattr(ctx.renderer.console, "width", 80)
        width = raw_width if isinstance(raw_width, int) and raw_width > 0 else 80
        num_cols = max(1, min(3, (width - 4) // 32))
        col_width = max(24, (width - 6) // num_cols)

        for server_name, tools in grouped.items():
            title = Text(f"📦 {server_name} ", style="bold cyan")
            title.append(f"({len(tools)} 個功能)", style="bold yellow")
            ctx.renderer.console.print(title)

            for i in range(0, len(tools), num_cols):
                row_tools = tools[i : i + num_cols]
                line = Text("   ")
                for j, t in enumerate(row_tools):
                    tool_text = f"• {t['name']}"
                    if j < len(row_tools) - 1:
                        tool_text = tool_text.ljust(col_width)
                    line.append(tool_text, style="green")
                ctx.renderer.console.print(line)
            ctx.renderer.console.print()

        tip = Text("💡 提示：輸入 ", style="dim")
        tip.append("/mcp <工具名稱>", style="bold yellow")
        tip.append(" 可查看該 MCP 工具的詳細功能說明（例如 ", style="dim")
        sample_server = next(iter(grouped.keys()))
        tip.append(f"/mcp {sample_server}", style="cyan")
        tip.append("）", style="dim")
        ctx.renderer.console.print(tip)
        if mcp_manager is not None and hasattr(mcp_manager, "find_server_for_tool"):
            if isinstance(mcp_manager.find_server_for_tool(_SQLITE_SWITCH_TOOL), str):
                sqlite_tip = Text("💡 提示：輸入 ", style="dim")
                sqlite_tip.append("/mcp sqlite <資料庫路徑>", style="bold yellow")
                sqlite_tip.append(" 可直接切換 SQLite 資料庫檔案", style="dim")
                ctx.renderer.console.print(sqlite_tip)
        ctx.renderer.console.print()

        return CommandResult()

    @staticmethod
    def _print_sqlite_status(ctx: CommandContext, mcp_manager, server_name: str) -> None:
        """若指定伺服器提供 SQLite 切換工具，顯示目前生效的資料庫路徑與切換用法。"""
        if mcp_manager is None or not hasattr(mcp_manager, "find_server_for_tool"):
            return
        if mcp_manager.find_server_for_tool(_SQLITE_SWITCH_TOOL) != server_name:
            return
        current = mcp_manager.get_runtime_conn_param(server_name, "DB_PATH")
        ctx.renderer.message(f"目前 SQLite 資料庫：{current or '（尚未設定，首次查詢時詢問）'}", "cyan")
        ctx.renderer.message("提示：輸入 /mcp sqlite <資料庫路徑> 可切換資料庫檔案", "dim")

    @staticmethod
    async def _switch_sqlite(ctx: CommandContext, mcp_manager, raw_path: str) -> CommandResult:
        """處理 /mcp sqlite <db_path>：拒絕 UNC、工作區外需確認，並經 MCP 工具完成切換與稽核。"""
        server_name = None
        if mcp_manager is not None and hasattr(mcp_manager, "find_server_for_tool"):
            server_name = mcp_manager.find_server_for_tool(_SQLITE_SWITCH_TOOL)
        if not isinstance(server_name, str) or not server_name:
            ctx.renderer.message("目前未掛載支援切換資料庫的 SQLite MCP 服務。", "yellow")
            return CommandResult()

        clean = raw_path.strip().strip("'\"")
        # 必須早於任何 Path 解析，避免 UNC 路徑觸發 SMB 認證外洩
        if not clean or clean.startswith(("\\\\", "//")):
            ctx.renderer.message("✗ 安全限制：不接受空白或 UNC／網路路徑，請先將資料庫複製到本機。", "red")
            return CommandResult()

        from pocket_agent.tools.fs import _validate_safe_path

        try:
            target = Path(clean).expanduser().resolve(strict=False)
        except OSError, RuntimeError, ValueError:
            ctx.renderer.message(f"✗ 無效的路徑格式：{clean}", "red")
            return CommandResult()

        try:
            _validate_safe_path(str(target), getattr(ctx.agent, "workspace_dirs", None))
        except ValueError:
            from rich.panel import Panel

            ctx.renderer._ensure_newline()
            ctx.renderer.console.print(
                Panel(
                    Text(f"資料庫不在目前工作區內或位於受保護路徑：\n  🗄 {target}", style="yellow"),
                    title="【安全警告】SQLite 資料庫切換確認",
                    title_align="left",
                    border_style="bold red",
                    expand=False,
                )
            )
            try:
                ans = input("\n確定要切換到此資料庫嗎？[y/N] ").strip().lower()
            except KeyboardInterrupt, EOFError:
                ans = ""
            if ans != "y":
                ctx.renderer.message("✗ 已取消切換。")
                return CommandResult()

        try:
            result = await mcp_manager.call(_SQLITE_SWITCH_TOOL, {"target_db_path": str(target)})
        except Exception as e:
            ctx.renderer.message(f"✗ 切換 SQLite 資料庫失敗：{e}", "red")
            return CommandResult()
        clean_result = result.replace("<external_content>\n", "").replace("\n</external_content>", "").strip()
        style = "green" if clean_result.startswith("[SUCCESS]") else "red"
        ctx.renderer.message(clean_result, style)
        return CommandResult()


ToolsCommand = McpCommand


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
        supports_img = ctx.state.provider == "openai" and bool(getattr(ctx.agent, "supports_image_command", True))
        if supports_img:
            ctx.renderer.message("/image <描述>  生圖，待送附件作參考圖")
        from pocket_agent.system.paths import is_portable

        if is_portable():
            ctx.renderer.message("/update  檢查並套用 GitHub 最新更新（自動儲存並重啟）")

        ctx.renderer.message("/attachments remove <編號> · /attachments clear")
        ctx.renderer.message(
            "Shift+Tab 切換自動編輯 (Accept-edits) · Enter 送出 · Esc 後 Enter 換行 · "
            "Ctrl+C 清空輸入 · 空輸入 Ctrl+D 離開"
        )
        for command in list_commands():
            ctx.renderer.message("/" + command["name"] + "  " + command["description"])
        return CommandResult()


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


class ImageCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if not args:
            raise ValueError("用法：/image <描述>")
        supports_img = ctx.state.provider == "openai" and bool(getattr(ctx.agent, "supports_image_command", True))
        if not supports_img:
            raise ValueError("產生圖片功能目前僅限使用 openai (ChatGPT OAuth 訂閱配額) 引擎時支援")
        return CommandResult("image", args)


class CodeSearchCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from rich.panel import Panel

        from pocket_agent.tools.rag import call_indexed_search

        query = args.strip()
        if not query:
            try:
                query = await ctx.reader.read("請輸入搜尋關鍵字（或按 Ctrl+C 取消）：")
                query = query.strip()
            except KeyboardInterrupt:
                return CommandResult()
            except EOFError:
                return CommandResult()
            if not query:
                return CommandResult()

        ctx.renderer.message(f"正在搜尋工作區：'{query}'...", "yellow")

        from pocket_agent.core.cancellation import run_sync

        result = await run_sync(call_indexed_search, {"query": query, "top_k": 5}, ctx.agent.workspace_dirs)

        clean_result = result
        if clean_result.startswith("<external_content>"):
            clean_result = clean_result.replace("<external_content>\n", "").replace("\n</external_content>", "")

        if "[ERROR]" in clean_result:
            ctx.renderer.console.print()
            ctx.renderer.console.print(
                Panel(clean_result, title="搜尋失敗", title_align="left", border_style="bold red")
            )
        else:
            ctx.renderer.console.print()
            ctx.renderer.console.print(
                Panel(clean_result, title="🔎 程式碼搜尋結果", title_align="left", border_style="bold green")
            )

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

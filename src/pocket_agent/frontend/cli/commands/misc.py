from pathlib import Path

from rich.cells import cell_len
from rich.markdown import Markdown
from rich.text import Text

from pocket_agent.extensions.mcp import parse_mcp_tool_name
from pocket_agent.frontend.cli.commands.core import CliCommand, CommandContext, CommandResult
from pocket_agent.frontend.cli.input import file_argument, get_builtin_commands, is_image_command_supported
from pocket_agent.system.i18n import t

# /mcp sqlite <db_path> 所呼叫的 SQLite MCP 切換工具名稱
_SQLITE_SWITCH_TOOL = "sqlite_switch_database"


def _mcp_display_name(tool_info: dict[str, str]) -> str:
    """回傳精簡的工具顯示名稱（移除 mcp__<server>__ 前綴）。"""
    orig = (tool_info.get("original_name") or "").strip()
    if orig:
        return orig
    _, raw_name = parse_mcp_tool_name(tool_info.get("name", ""))
    return raw_name


def _compute_mcp_grid_layout(grouped: dict[str, list[dict[str, str]]], term_width: int) -> tuple[int, int]:
    """依實際最長工具名稱與終端機寬度動態計算 /mcp 清單的 (欄數, 單欄寬度)。"""
    avail_width = max(24, term_width - 4)
    gap = 3
    max_item_len = max(
        (cell_len(f"• {_mcp_display_name(t)}") for tools in grouped.values() for t in tools),
        default=20,
    )
    min_col_width = max(24, max_item_len + gap)
    num_cols = max(1, min(3, (avail_width + gap) // min_col_width))
    col_width = max(min_col_width, avail_width // num_cols)
    return num_cols, col_width


class QuitCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError(t("misc_cmd.no_args_generic"))
        return CommandResult("quit")


class UsageCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError(t("misc_cmd.usage_no_args"))
        ctx.renderer.message(t("misc_cmd.usage_tokens", tokens=ctx.state.session_tokens), "cyan")

        usage = getattr(ctx.agent, "context_usage", None)
        if usage is None:
            ctx.renderer.message(t("misc_cmd.usage_no_context"), "yellow")
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
                    ctx.renderer.message(msg or t("misc_cmd.usage_quota_unsupported"), "yellow")
            except Exception as e:
                ctx.renderer.message(t("misc_cmd.usage_quota_failed", error=e), "red")
        else:
            ctx.renderer.message(t("misc_cmd.usage_quota_unavailable"), "yellow")

        return CommandResult()


class LogoutCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError(t("misc_cmd.logout_no_args"))
        from pocket_agent.system.auth import logout

        logout()
        return CommandResult("quit")


class ClearCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError(t("misc_cmd.clear_no_args"))
        ctx.agent.clear_history()
        ctx.renderer.message(t("misc_cmd.clear_done"))
        return CommandResult()


class CleanCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError(t("misc_cmd.clean_no_args"))
        from pocket_agent.core.cancellation import run_sync
        from pocket_agent.system.housekeeping import default_housekeeper

        res = await run_sync(default_housekeeper.run_cleanup, force=True)
        files = res.get("cleaned_files", 0)
        mb = res.get("freed_mb", 0.0)
        remaining = res.get("remaining_files", 0)
        rem_mb = res.get("remaining_mb", 0.0)
        ttl_days = res.get("ttl_days", default_housekeeper.ttl_days)
        log_ttl_days = res.get("log_ttl_days", ttl_days * 2)

        if files > 0:
            ctx.renderer.message(t("misc_cmd.clean_freed", files=files, mb=mb), "green")
        elif remaining > 0:
            ctx.renderer.message(
                t(
                    "misc_cmd.clean_ok_remaining",
                    remaining=remaining,
                    rem_mb=rem_mb,
                    ttl=ttl_days,
                    log_ttl=log_ttl_days,
                ),
                "green",
            )
        else:
            ctx.renderer.message(
                t("misc_cmd.clean_ok_empty", ttl=ttl_days, log_ttl=log_ttl_days),
                "green",
            )
        ctx.renderer.message(t("misc_cmd.clean_tip"), "dim")
        return CommandResult()


class UpdateCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args.strip():
            raise ValueError(t("misc_cmd.update_no_args"))
        from pocket_agent.system.updater import get_current_build_info, update_manager

        if not update_manager.is_supported():
            ctx.renderer.message(t("misc_cmd.update_unsupported"), "yellow")
            return CommandResult()

        staged = await update_manager.download_and_stage(
            on_status=lambda msg: ctx.renderer.message(msg, "cyan"),
        )
        if staged is None:
            curr = get_current_build_info()
            label = curr.commit[:7] or curr.version
            ctx.renderer.message(t("misc_cmd.update_latest", label=label), "green")
            return CommandResult()

        ctx.state.staged_update_version = staged.short_sha
        ctx.state.pending_update_restart = True
        ctx.renderer.message(t("misc_cmd.update_ready", sha=staged.short_sha), "green")
        return CommandResult("quit")


class FileCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from pathlib import Path

        path_str = file_argument(args)

        try:
            resolved_path = Path(path_str).expanduser().resolve(strict=True)
        except Exception:
            raise ValueError(t("misc_cmd.file_not_found", path=path_str))

        if not resolved_path.is_file():
            raise ValueError(t("misc_cmd.file_not_regular", path=path_str))

        status, msg, target_dir = ctx.agent.check_new_workspace_dir(str(resolved_path.parent))

        if status not in ("in_default", "already_authorized"):
            from rich.panel import Panel
            from rich.text import Text

            ctx.renderer._ensure_newline()
            panel = Panel(
                Text(t("misc_cmd.file_outside_ws", path=resolved_path), style="yellow"),
                title=t("misc_cmd.file_ws_warning_title"),
                title_align="left",
                border_style="bold red",
                expand=False,
            )
            ctx.renderer.console.print(panel)
            try:
                ans = input(t("misc_cmd.file_ws_confirm")).strip().lower()
                if ans == "y":
                    if target_dir:
                        ctx.agent.add_workspace_dir(target_dir)
                    ctx.renderer.message(t("misc_cmd.file_ws_authorized"))
                else:
                    ctx.renderer.message(t("misc_cmd.file_ws_cancelled"))
                    return CommandResult()
            except KeyboardInterrupt, EOFError:
                ctx.renderer.message(t("misc_cmd.op_cancelled"))
                return CommandResult()

        for path in ctx.state.add_files([str(resolved_path)]):
            ctx.renderer.message(t("misc_cmd.file_added", path=path))
        return CommandResult()


class RmFileCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if not args.strip():
            if not ctx.state.pending_files:
                ctx.renderer.message(t("misc_cmd.rm_file_empty"))
                return CommandResult()

            cancel_label = t("session_cmd.cancel")
            choices = []
            for i, p in enumerate(ctx.state.pending_files, 1):
                choices.append(f"{i}. {p}")
            choices.append(cancel_label)

            selected = await ctx.reader.choose(t("misc_cmd.rm_file_choose"), choices)
            if not selected or selected == cancel_label:
                ctx.renderer.message(t("misc_cmd.rm_file_cancelled"))
                return CommandResult()

            idx = choices.index(selected) + 1
            removed = ctx.state.remove(idx)
            ctx.renderer.message(t("misc_cmd.rm_file_removed", path=removed))
            return CommandResult()

        arg = args.strip()
        try:
            # 嘗試作為編號移除
            number = int(arg)
            removed = ctx.state.remove(number)
            ctx.renderer.message(t("misc_cmd.rm_file_removed", path=removed))
            return CommandResult()
        except ValueError:
            pass

        # 作為路徑移除
        from pathlib import Path

        try:
            target = str(Path(arg).expanduser().resolve(strict=False))
        except Exception:
            raise ValueError(t("misc_cmd.rm_file_invalid_path", path=arg))

        for i, p in enumerate(ctx.state.pending_files, 1):
            if str(Path(p).expanduser().resolve(strict=False)) == target:
                removed = ctx.state.remove(i)
                ctx.renderer.message(t("misc_cmd.rm_file_removed", path=removed))
                return CommandResult()

        raise ValueError(t("misc_cmd.rm_file_not_found", path=arg))


class AttachmentsCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        parts = args.split()
        if not parts:
            if not ctx.state.pending_files:
                ctx.renderer.message(t("misc_cmd.attachments_empty"))
            for i, path in enumerate(ctx.state.pending_files, 1):
                ctx.renderer.message(f"{i}. {path}")
        elif parts == ["clear"]:
            ctx.state.pending_files.clear()
            ctx.renderer.message(t("misc_cmd.attachments_cleared"))
        elif len(parts) == 2 and parts[0] == "remove":
            try:
                number = int(parts[1])
            except ValueError as exc:
                raise ValueError(t("misc_cmd.attachments_invalid_num")) from exc
            ctx.renderer.message(t("misc_cmd.rm_file_removed", path=ctx.state.remove(number)))
        else:
            raise ValueError(t("misc_cmd.attachments_usage"))
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
                        "original_name": getattr(entry, "original_name", ""),
                        "description": (entry.schema.get("description") or "").strip(),
                    }
                )

        # Fallback: 若無 MCPManager 物件但有 ctx.tools (例如測試環境或直接傳遞)
        if not grouped and ctx.tools:
            for tool_name in ctx.tools:
                parsed_server, parsed_tool = parse_mcp_tool_name(tool_name)
                if parsed_server is not None:
                    server_key = parsed_server
                    display_name = parsed_tool
                else:
                    server_key = "mcp"
                    display_name = tool_name
                    for prefix in ("rhel_", "win_rdp_", "browser_", "sqlite_", "mysql_", "mssql_"):
                        if tool_name.startswith(prefix):
                            server_key = prefix.rstrip("_").replace("_", "-")
                            break
                grouped.setdefault(server_key, []).append(
                    {"name": tool_name, "original_name": display_name, "description": ""}
                )

        if not grouped:
            ctx.renderer.message(t("misc_cmd.mcp_none"))
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
                    t("misc_cmd.mcp_not_found", name=arg, tools=", ".join(grouped.keys())),
                    "yellow",
                )
                return CommandResult()

            tools = grouped[matched_server]
            ctx.renderer.console.print()
            header = Text(f"📦 {matched_server}", style="bold cyan")
            header.append(t("misc_cmd.mcp_server_count", count=len(tools)), style="dim")
            ctx.renderer.console.print(header)

            for tool_info in tools:
                line = Text(f"  • {_mcp_display_name(tool_info)}", style="bold green")
                desc = tool_info.get("description")
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
        header = Text(t("mcp_cmd.header"), style="bold magenta")
        header.append(t("mcp_cmd.summary", servers=len(grouped), tools=total_tools), style="dim")
        ctx.renderer.console.print(header)
        ctx.renderer.console.print()

        raw_width = getattr(ctx.renderer.console, "width", 80)
        width = raw_width if isinstance(raw_width, int) and raw_width > 0 else 80
        num_cols, col_width = _compute_mcp_grid_layout(grouped, width)

        for server_name, tools in grouped.items():
            title = Text(f"📦 {server_name} ", style="bold cyan")
            title.append(t("mcp_cmd.server_tools_count", count=len(tools)), style="bold yellow")
            ctx.renderer.console.print(title)

            for i in range(0, len(tools), num_cols):
                row_tools = tools[i : i + num_cols]
                line = Text("   ")
                for j, tool_item in enumerate(row_tools):
                    tool_text = f"• {_mcp_display_name(tool_item)}"
                    if j < len(row_tools) - 1:
                        pad = max(1, col_width - cell_len(tool_text))
                        tool_text = tool_text + (" " * pad)
                    line.append(tool_text, style="green")
                ctx.renderer.console.print(line)
            ctx.renderer.console.print()

        tip = Text(t("mcp_cmd.tip_prefix"), style="dim")
        tip.append(t("mcp_cmd.tip_cmd"), style="bold yellow")
        tip.append(t("mcp_cmd.tip_mid"), style="dim")
        sample_server = next(iter(grouped.keys()))
        tip.append(f"/mcp {sample_server}", style="cyan")
        tip.append(t("mcp_cmd.tip_suffix"), style="dim")
        ctx.renderer.console.print(tip)
        if mcp_manager is not None and hasattr(mcp_manager, "find_server_for_tool"):
            if isinstance(mcp_manager.find_server_for_tool(_SQLITE_SWITCH_TOOL), str):
                sqlite_tip = Text(t("mcp_cmd.tip_prefix"), style="dim")
                sqlite_tip.append(t("mcp_cmd.sqlite_tip_cmd"), style="bold yellow")
                sqlite_tip.append(t("mcp_cmd.sqlite_tip_desc"), style="dim")
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
        ctx.renderer.message(
            t("misc_cmd.mcp_sqlite_current", path=current or t("misc_cmd.mcp_sqlite_unset")),
            "cyan",
        )
        ctx.renderer.message(t("misc_cmd.mcp_sqlite_tip"), "dim")

    @staticmethod
    async def _switch_sqlite(ctx: CommandContext, mcp_manager, raw_path: str) -> CommandResult:
        """處理 /mcp sqlite <db_path>：拒絕 UNC、工作區外需確認，並經 MCP 工具完成切換與稽核。"""
        server_name = None
        if mcp_manager is not None and hasattr(mcp_manager, "find_server_for_tool"):
            server_name = mcp_manager.find_server_for_tool(_SQLITE_SWITCH_TOOL)
        if not isinstance(server_name, str) or not server_name:
            ctx.renderer.message(t("misc_cmd.mcp_sqlite_no_server"), "yellow")
            return CommandResult()

        clean = raw_path.strip().strip("'\"")
        # 必須早於任何 Path 解析，避免 UNC 路徑觸發 SMB 認證外洩
        if not clean or clean.startswith(("\\\\", "//")):
            ctx.renderer.message(t("misc_cmd.mcp_sqlite_unc_rejected"), "red")
            return CommandResult()

        from pocket_agent.tools.fs import _validate_safe_path

        try:
            target = Path(clean).expanduser().resolve(strict=False)
        except OSError, RuntimeError, ValueError:
            ctx.renderer.message(t("misc_cmd.mcp_sqlite_invalid_path", path=clean), "red")
            return CommandResult()

        try:
            _validate_safe_path(str(target), getattr(ctx.agent, "workspace_dirs", None))
        except ValueError:
            from rich.panel import Panel

            ctx.renderer._ensure_newline()
            ctx.renderer.console.print(
                Panel(
                    Text(t("misc_cmd.mcp_sqlite_outside_ws", path=target), style="yellow"),
                    title=t("misc_cmd.mcp_sqlite_ws_title"),
                    title_align="left",
                    border_style="bold red",
                    expand=False,
                )
            )
            try:
                ans = input(t("misc_cmd.mcp_sqlite_confirm")).strip().lower()
            except KeyboardInterrupt, EOFError:
                ans = ""
            if ans != "y":
                ctx.renderer.message(t("misc_cmd.mcp_sqlite_cancelled"))
                return CommandResult()

        try:
            result = await mcp_manager.call(_SQLITE_SWITCH_TOOL, {"target_db_path": str(target)})
        except Exception as e:
            ctx.renderer.message(t("misc_cmd.mcp_sqlite_failed", error=e), "red")
            return CommandResult()
        clean_result = result.replace("<external_content>\n", "").replace("\n</external_content>", "").strip()
        style = "green" if clean_result.startswith("[SUCCESS]") else "red"
        ctx.renderer.message(clean_result, style)
        return CommandResult()


ToolsCommand = McpCommand


class SkillsCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args and args.strip() == "":
            raise ValueError(t("misc_cmd.skills_bad_args"))

        from pocket_agent.extensions.skills import list_skills, load_skill
        from pocket_agent.system.paths import bundle_dir, user_dir

        if args:
            skill_name = args.strip()
            body = load_skill(skill_name, ctx.agent.workspace_dirs)
            if body:
                ctx.renderer.console.print(f"\n[bold white]Skill: {skill_name}[/bold white]")
                ctx.renderer.console.print(Markdown(body))
            else:
                ctx.renderer.message(t("skills_cmd.load_failed", name=skill_name))
            return CommandResult()

        skills = list_skills(ctx.agent.workspace_dirs)
        if not skills:
            ctx.renderer.message(t("skills_cmd.empty"))
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

        ctx.renderer.message(t("skills_cmd.hint"), "dim")
        return CommandResult()


class HelpCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError(t("misc_cmd.help_no_args"))
        from pocket_agent.system.commands import list_commands

        for command, description in get_builtin_commands().items():
            ctx.renderer.message(f"/{command}  {description}")
        if is_image_command_supported(ctx.state, ctx.agent):
            ctx.renderer.message(t("misc_cmd.help_image_line"))
        from pocket_agent.system.paths import is_portable

        if is_portable():
            ctx.renderer.message(t("misc_cmd.help_update_line"))

        ctx.renderer.message(t("misc_cmd.help_attachments_line"))
        ctx.renderer.message(t("misc_cmd.help_shortcuts_line"))
        for command in list_commands():
            ctx.renderer.message("/" + command["name"] + "  " + command["description"])
        return CommandResult()


class GoalCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if not args:
            raise ValueError(t("misc_cmd.goal_usage"))
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
            raise ValueError(t("misc_cmd.image_usage"))
        if not is_image_command_supported(ctx.state, ctx.agent):
            raise ValueError(t("misc_cmd.image_unsupported"))
        return CommandResult("image", args)


class CodeSearchCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from rich.panel import Panel

        from pocket_agent.tools.rag import call_indexed_search

        query = args.strip()
        if not query:
            try:
                query = await ctx.reader.read(t("misc_cmd.codesearch_prompt"))
                query = query.strip()
            except KeyboardInterrupt, EOFError:
                return CommandResult()
            if not query:
                return CommandResult()

        ctx.renderer.message(t("misc_cmd.codesearch_searching", query=query), "yellow")

        from pocket_agent.core.cancellation import run_sync

        result = await run_sync(call_indexed_search, {"query": query, "top_k": 5}, ctx.agent.workspace_dirs)

        clean_result = result
        if clean_result.startswith("<external_content>"):
            clean_result = clean_result.replace("<external_content>\n", "").replace("\n</external_content>", "")

        if "[ERROR]" in clean_result:
            ctx.renderer.console.print()
            ctx.renderer.console.print(
                Panel(
                    clean_result,
                    title=t("misc_cmd.codesearch_failed_title"),
                    title_align="left",
                    border_style="bold red",
                )
            )
        else:
            ctx.renderer.console.print()
            ctx.renderer.console.print(
                Panel(
                    clean_result,
                    title=t("misc_cmd.codesearch_result_title"),
                    title_align="left",
                    border_style="bold green",
                )
            )

            if callable(getattr(type(ctx.agent), "inject_code_search_context", None)):
                ctx.agent.inject_code_search_context(query, clean_result)
            elif hasattr(ctx.agent, "history") and isinstance(ctx.agent.history, list):
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

            ctx.renderer.message(t("misc_cmd.codesearch_tip"), "cyan")

        return CommandResult()

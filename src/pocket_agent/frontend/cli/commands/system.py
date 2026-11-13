import difflib
from pathlib import Path

from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text

from pocket_agent.frontend.cli.commands.core import CliCommand, CommandContext, CommandResult


async def switch_model(
    agent,
    state,
    provider: str,
    model: str,
    renderer,
) -> None:
    from pocket_agent.system.auth import get_provider_client
    from pocket_agent.system.providers import SUPPORTED_PROVIDERS

    if provider not in SUPPORTED_PROVIDERS or not model or any(c.isspace() for c in model):
        raise ValueError(f"用法：/model <{'|'.join(SUPPORTED_PROVIDERS)}> <模型名稱>")
    new_client = get_provider_client(provider=provider, model=model)
    old_client = agent.client
    # No await between construction and commit: all consumers see one state.
    agent.client, agent.model, agent.provider = new_client, model, provider
    state.provider, state.model = provider, model
    renderer.message(f"當次對話已切換：{provider} / {model}", "green")

    from pocket_agent.system.config import config

    if not config.get("system.context_window"):
        import asyncio

        try:
            task = asyncio.create_task(agent.engine.fetch_context_window())
            agent._bg_tasks.add(task)
            task.add_done_callback(agent._bg_tasks.discard)
        except Exception:
            pass

    try:
        await old_client.close()
    except Exception:
        renderer.message("已切換，但舊連線清理失敗；目前模型不受影響。", "yellow")


async def do_model_flow(ctx: CommandContext, args: str) -> None:
    from pocket_agent.system.auth import fetch_available_models
    from pocket_agent.system.providers import PROVIDER_STRATEGIES, SUPPORTED_PROVIDERS

    if args:
        parts = args.split()
        if len(parts) == 1:
            from pocket_agent.system.config import config

            provider = config.get("active_provider", "openai")
            model = parts[0]
        elif len(parts) == 2:
            provider, model = parts
        else:
            raise ValueError(f"用法：/model [<{'|'.join(SUPPORTED_PROVIDERS)}>] <模型名稱>")
    else:
        # 轉換為 display_name 顯示
        display_choices = [PROVIDER_STRATEGIES[p].display_name for p in SUPPORTED_PROVIDERS]
        chosen_display = await ctx.reader.choose("提供者", display_choices)
        if not chosen_display:
            return

        # 轉回 internal ID
        provider = next((p for p in SUPPORTED_PROVIDERS if PROVIDER_STRATEGIES[p].display_name == chosen_display), None)
        if not provider or provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"僅支援 {', '.join(SUPPORTED_PROVIDERS)}")

        try:
            models = fetch_available_models(provider)
        except Exception:
            models = []
        if not models:
            ctx.renderer.message("無法取得模型清單，請手動輸入名稱。", "yellow")
        model = await ctx.reader.choose("模型名稱 · Tab／方向鍵補全", models)
        if not model:
            return
    await switch_model(ctx.agent, ctx.state, provider, model, ctx.renderer)


class ReasoningCommand(CliCommand):
    """在對話中即時切換 LLM 推理強度 (auto / high / medium / low / none)。"""

    _CHOICES = [
        ("auto", "Auto (預設，交由模型自行決定)"),
        ("high", "High (深度推理)"),
        ("medium", "Medium (平衡)"),
        ("low", "Low (快速回應)"),
        ("none", "None (關閉推理)"),
    ]

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from pocket_agent.system.constants import VALID_REASONING_EFFORTS

        raw = args.strip().lower()
        if not raw:
            current = str(getattr(ctx.agent, "reasoning_effort", "auto") or "auto").lower()
            labels = [label for _, label in self._CHOICES]
            selected = await ctx.reader.choose(f"選擇推理強度（目前：{current.capitalize()}）", labels)
            if not selected:
                return CommandResult()
            level = next((key for key, label in self._CHOICES if label == selected), None)
            if not level:
                return CommandResult()
        else:
            if raw not in VALID_REASONING_EFFORTS:
                raise ValueError(f"用法：/reasoning [{'|'.join(VALID_REASONING_EFFORTS)}]")
            level = raw

        ctx.agent.reasoning_effort = level
        ctx.state.reasoning_effort = level
        ctx.renderer.message(f"✓ 推理強度已設為：{level.capitalize()}", "green")
        return CommandResult()


class AddDirCommand(CliCommand):
    """授權額外工作區目錄（/add-dir <path>）。"""

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if not args.strip():
            raise ValueError("用法：/add-dir <目錄路徑>")
        status, msg, target_dir = ctx.agent.check_new_workspace_dir(args.strip())
        if status in ("invalid", "not_found"):
            raise ValueError(msg)
        if status in ("in_default", "already_authorized"):
            ctx.renderer.message(f"✓ {msg}", "green")
            return CommandResult()

        warn_text = Text()
        warn_text.append("【安全警告】", style="bold red")
        warn_text.append(f"{msg}是否授權本次會話讀寫該目錄？")
        ctx.renderer.console.print(warn_text)

        confirm = await ctx.reader.choose("請選擇", ["允許", "拒絕"])
        if confirm == "允許" and target_dir is not None:
            ctx.agent.add_workspace_dir(target_dir)
            ctx.renderer.message(f"✓ 已授權：{target_dir}，可透過工具讀寫。", "green")
        else:
            ctx.renderer.message("⚠ 已拒絕授權。", "yellow")
        return CommandResult()


class RmDirCommand(CliCommand):
    """移除額外授權的工作區目錄（/rm-dir [<path>]）。"""

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        target = args.strip()
        if not target:
            dirs = list(getattr(ctx.agent, "workspace_dirs", []))
            if not dirs:
                ctx.renderer.message("目前沒有任何額外授權目錄可移除。")
                return CommandResult()
            selected = await ctx.reader.choose("選擇要移除的額外授權目錄", [*dirs, "取消"])
            if not selected or selected == "取消":
                ctx.renderer.message("已取消移除。")
                return CommandResult()
            target = selected

        success, msg = ctx.agent.remove_workspace_dir(target)
        if not success:
            raise ValueError(msg)
        ctx.renderer.message(f"✓ {msg}", "green")
        return CommandResult()


class WorkspaceCommand(CliCommand):
    """顯示目前預設與額外授權之工作區目錄（/workspace）。"""

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        lines = ["【預設工作區】", f"  {Path.cwd().resolve()}"]
        if ctx.agent.workspace_dirs:
            lines.append("\n【額外授權目錄】")
            for wd in ctx.agent.workspace_dirs:
                lines.append(f"  {wd}")
        else:
            lines.append("\n【額外授權目錄】無")
        ctx.renderer.message("\n".join(lines), "cyan")
        return CommandResult()


class ModelCommand(CliCommand):
    """切換供應商與模型（/model [<provider>] <model>）。"""

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        await do_model_flow(ctx, args)
        return CommandResult()


class LearnCommand(CliCommand):
    """從對話歷史萃取並儲存 Skill（/learn）。"""

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from pocket_agent.extensions.skills import save_skill
        from pocket_agent.system.registry import parse_frontmatter

        ctx.renderer.message("正在萃取對話知識，請稍候...", "dim")
        skill_content = await ctx.agent.extract_skill()
        meta, body = parse_frontmatter(skill_content)
        suggested_name = meta.get("name", "")
        suggested_desc = meta.get("description", "")

        ctx.renderer.console.print(Markdown(skill_content))

        skill_name = await ctx.reader.read(
            "請輸入要儲存的 Skill 名稱 (英數字中折線，例如 my-skill，留空取消): ",
            default=suggested_name,
        )
        if not skill_name:
            ctx.renderer.message("已取消儲存。")
            return CommandResult()

        skill_desc = await ctx.reader.read("請輸入簡短的 Skill 描述: ", default=suggested_desc)
        scope = await ctx.reader.choose("請選擇儲存範圍", ["workspace", "global"])
        if scope:
            save_path = save_skill(skill_name.strip(), skill_desc.strip(), body.strip(), scope)
            ctx.renderer.message(f"✓ 已儲存 Skill 至: {save_path}", "green")
        else:
            ctx.renderer.message("已取消儲存。")
        return CommandResult()


class UndoCommand(CliCommand):
    """檢視差異並還原最近一次備份之檔案（/undo [<path>]）。"""

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from pocket_agent.system.sys_commands import sys_registry

        target = args.strip()
        if target:
            res = sys_registry.execute(f"/undo {target}", ctx.agent)
            if res is None or res.status == "error":
                raise ValueError(res.message if res else "還原失敗")
            ctx.renderer.message(res.message, "green")
            return CommandResult()

        res = sys_registry.execute("/undo", ctx.agent)
        if res is None or res.status == "error":
            raise ValueError(res.message if res else "找不到可用的備份紀錄")

        mapping = res.payload.get("mapping", {})
        if not mapping:
            ctx.renderer.message("目前沒有可用的備份紀錄。")
            return CommandResult()

        choices = []
        choice_map = {}
        for orig, bkup in reversed(mapping.items()):
            parts = Path(bkup).name.split("_")
            if len(parts) >= 2 and len(parts[0]) == 8 and len(parts[1]) == 6:
                d_str = f"{parts[0][:4]}-{parts[0][4:6]}-{parts[0][6:8]} {parts[1][:2]}:{parts[1][2:4]}:{parts[1][4:6]}"
                display = f"[{d_str}] {orig}"
            else:
                display = orig
            choices.append(display)
            choice_map[display] = orig
        choices.append("取消")

        selected = await ctx.reader.choose("選擇要還原的檔案 (標籤為備份當下時間)", choices)
        if not selected or selected == "取消":
            ctx.renderer.message("已取消還原。")
            return CommandResult()

        orig_path_str = choice_map[selected]
        backup_path_str = mapping[orig_path_str]
        try:
            op = Path(orig_path_str)
            bp = Path(backup_path_str)
            orig_lines = op.read_text(encoding="utf-8").splitlines(keepends=True) if op.exists() else []
            bkup_lines = bp.read_text(encoding="utf-8").splitlines(keepends=True) if bp.exists() else []
            diff = list(
                difflib.unified_diff(
                    orig_lines, bkup_lines, fromfile=f"Current: {op.name}", tofile=f"Backup: {bp.name}", n=5
                )
            )
            if not diff:
                ctx.renderer.message("檔案內容無差異，無需還原。", "yellow")
                return CommandResult()
            ctx.renderer.console.print("\n[bold cyan]─── 檔案差異預覽 (還原將套用以下變更) ───[/bold cyan]")
            ctx.renderer.console.print(Syntax("".join(diff), "diff", theme="monokai", background_color="default"))
            ctx.renderer.console.print("[bold cyan]───────────────────────────────────────────────[/bold cyan]\n")
        except UnicodeDecodeError:
            ctx.renderer.message("⚠ 二進制檔案或不支援的編碼，無法預覽差異。", "yellow")
        except Exception as exc:
            ctx.renderer.message(f"⚠ 產生差異預覽時發生錯誤: {exc}", "yellow")

        confirm = await ctx.reader.choose("確定要執行上述還原覆蓋嗎？", ["確認還原", "取消"])
        if confirm != "確認還原":
            ctx.renderer.message("已取消還原。")
            return CommandResult()

        restore_result = sys_registry.execute(f"/undo {orig_path_str}", ctx.agent)
        if restore_result and restore_result.status == "success":
            ctx.renderer.message(restore_result.message, "green")
        else:
            ctx.renderer.message(restore_result.message if restore_result else "還原失敗", "red")
        return CommandResult()


class ConfigCommand(CliCommand):
    """互動式檢視與編輯全域設定（/config [set <key> <value>]）。"""

    @staticmethod
    def _flatten(data: dict, parent_key: str = "", sep: str = ".") -> dict:
        items: dict = {}
        for k, v in data.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            if isinstance(v, dict):
                items.update(ConfigCommand._flatten(v, new_key, sep=sep))
            else:
                items[new_key] = v
        return items

    @staticmethod
    def _is_secret_key(key: str) -> bool:
        k_lower = key.lower()
        return any(word in k_lower for word in ("key", "token", "password", "secret"))

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from pocket_agent.system.config import config
        from pocket_agent.system.sys_commands import sys_registry

        if args.strip():
            res = sys_registry.execute(f"/config {args}", ctx.agent)
            if res is None or res.status == "error":
                raise ValueError(res.message if res else "設定指令失敗")
            ctx.renderer.console.print(res.message)
            return CommandResult()

        while True:
            flat_data = self._flatten(config.json_provider._data)
            choices = []
            keys_list = []
            for k, v in flat_data.items():
                if self._is_secret_key(k):
                    display_v = f"{v[:3]}********{v[-3:]}" if isinstance(v, str) and len(v) > 6 else "********"
                else:
                    display_v = str(v)
                choices.append(f"{k} = {display_v}")
                keys_list.append(k)

            choices.extend(["➕ 新增設定項", "離開"])
            selected = await ctx.reader.choose("【全域設定檔】(選擇項目以編輯)", choices)
            if not selected or selected == "離開":
                ctx.renderer.message("已離開設定選單。")
                return CommandResult()

            if selected == "➕ 新增設定項":
                new_key = await ctx.reader.read("設定鍵名 (Key, 可用點表示法): ")
                if not new_key:
                    continue
                is_secret = self._is_secret_key(new_key)
                new_val = await ctx.reader.read(f"設定值 (Value) [{new_key}]: ", is_password=is_secret)
                if new_val:
                    res = sys_registry.execute(f"/config set {new_key.strip()} {new_val.strip()}", ctx.agent)
                    if res:
                        ctx.renderer.message(res.message, "cyan")
                continue

            idx = choices.index(selected)
            key = keys_list[idx]
            current_val = str(flat_data[key])
            is_secret = self._is_secret_key(key)
            prompt_msg = f"編輯 {key} (機密欄位留空則不修改): " if is_secret else f"編輯 {key}: "
            new_val = await ctx.reader.read(prompt_msg, default="" if is_secret else current_val, is_password=is_secret)

            if new_val is not None and new_val.strip() and new_val != current_val:
                res = sys_registry.execute(f"/config set {key} {new_val.strip()}", ctx.agent)
                if res:
                    ctx.renderer.message(res.message, "cyan")

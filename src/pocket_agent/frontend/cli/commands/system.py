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
    from pocket_agent.system.auth import get_openai_client
    from pocket_agent.system.constants import SUPPORTED_PROVIDERS

    if provider not in SUPPORTED_PROVIDERS or not model or any(c.isspace() for c in model):
        raise ValueError(f"用法：/model <{'|'.join(SUPPORTED_PROVIDERS)}> <模型名稱>")
    new_client = get_openai_client(provider=provider, model=model)
    old_client = agent.client
    # No await between construction and commit: all consumers see one state.
    agent.client, agent.model = new_client, model
    state.provider, state.model = provider, model
    renderer.message(f"當次對話已切換：{provider} / {model}", "green")
    try:
        await old_client.close()
    except Exception:
        renderer.message("已切換，但舊連線清理失敗；目前模型不受影響。", "yellow")


async def do_model_flow(ctx: CommandContext, args: str) -> None:
    from pocket_agent.system.auth import fetch_available_models
    from pocket_agent.system.constants import SUPPORTED_PROVIDERS

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
        provider = await ctx.reader.choose(f"提供者 {' / '.join(SUPPORTED_PROVIDERS)}", SUPPORTED_PROVIDERS)
        if not provider:
            return
        if provider not in SUPPORTED_PROVIDERS:
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


class SysRegistryCommand(CliCommand):
    def __init__(self, name: str):
        self.name = name

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from pocket_agent.system.sys_commands import sys_registry

        result = sys_registry.execute(f"/{self.name} {args}", ctx.agent)

        if result.status == "error":
            raise ValueError(result.message)
        elif result.status == "success":
            if self.name == "config":
                ctx.renderer.console.print(result.message)
            else:
                ctx.renderer.message(result.message, "green" if self.name == "add-dir" else "cyan")
        elif result.status == "needs_auth":
            warn_text = Text()
            warn_text.append("【安全警告】", style="bold red")
            warn_text.append(f"{result.message}是否授權本次會話讀寫該目錄？")
            ctx.renderer.console.print(warn_text)

            confirm = await ctx.reader.choose("請選擇", ["允許", "拒絕"])
            if confirm == "允許" and result.payload.get("target_dir"):
                ctx.agent.add_workspace_dir(result.payload["target_dir"])
                ctx.renderer.message(f"✓ 已授權：{result.payload['target_dir']}，可透過工具讀寫。", "green")
            else:
                ctx.renderer.message("⚠ 已拒絕授權。", "yellow")
        elif result.status == "interactive_undo":
            mapping = result.payload.get("mapping", {})
            if not mapping:
                ctx.renderer.message("目前沒有可用的備份紀錄。")
                return CommandResult()

            choices = []
            choice_map = {}
            for orig, bkup in reversed(mapping.items()):
                bkup_name = Path(bkup).name
                parts = bkup_name.split("_")
                if len(parts) >= 2 and len(parts[0]) == 8 and len(parts[1]) == 6:
                    d_date = f"{parts[0][:4]}-{parts[0][4:6]}-{parts[0][6:8]}"
                    d_time = f"{parts[1][:2]}:{parts[1][2:4]}:{parts[1][4:6]}"
                    d_str = f"{d_date} {d_time}"
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
            except Exception as e:
                ctx.renderer.message(f"⚠ 產生差異預覽時發生錯誤: {e}", "yellow")

            confirm = await ctx.reader.choose("確定要執行上述還原覆蓋嗎？", ["確認還原", "取消"])
            if confirm != "確認還原":
                ctx.renderer.message("已取消還原。")
                return CommandResult()

            restore_result = sys_registry.execute(f"/undo {orig_path_str}", ctx.agent)
            if restore_result.status == "success":
                ctx.renderer.message(restore_result.message, "green")
            else:
                ctx.renderer.message(restore_result.message, "red")
        elif result.status == "interactive_config":
            while True:
                from pocket_agent.system.config import config

                def flatten(d, parent_key="", sep="."):
                    items = []
                    for k, v in d.items():
                        new_key = f"{parent_key}{sep}{k}" if parent_key else k
                        if isinstance(v, dict):
                            items.extend(flatten(v, new_key, sep=sep).items())
                        else:
                            items.append((new_key, v))
                    return dict(items)

                flat_data = flatten(config.json_provider._data)

                choices = []
                keys_list = []
                for k, v in flat_data.items():
                    k_lower = k.lower()
                    if "key" in k_lower or "token" in k_lower or "password" in k_lower or "secret" in k_lower:
                        if isinstance(v, str) and len(v) > 6:
                            display_v = f"{v[:3]}********{v[-3:]}"
                        else:
                            display_v = "********"
                    else:
                        display_v = str(v)
                    choices.append(f"{k} = {display_v}")
                    keys_list.append(k)

                choices.append("➕ 新增設定項")
                choices.append("離開")

                selected = await ctx.reader.choose("【全域設定檔】(選擇項目以編輯)", choices)
                if not selected or selected == "離開":
                    ctx.renderer.message("已離開設定選單。")
                    return CommandResult()

                if selected == "➕ 新增設定項":
                    new_key = await ctx.reader.read("設定鍵名 (Key, 可用點表示法): ")
                    if not new_key:
                        continue
                    is_secret_key = any(x in new_key.lower() for x in ("key", "token", "password", "secret"))
                    new_val = await ctx.reader.read(f"設定值 (Value) [{new_key}]: ", is_password=is_secret_key)
                    if new_val:
                        res = sys_registry.execute(f"/config set {new_key.strip()} {new_val.strip()}", ctx.agent)
                        ctx.renderer.message(res.message, "cyan")
                    continue

                idx = choices.index(selected)
                key = keys_list[idx]
                current_val = str(flat_data[key])
                is_secret = any(x in key.lower() for x in ("key", "token", "password", "secret"))
                default_edit = "" if is_secret else current_val

                prompt_msg = f"編輯 {key} (機密欄位留空則不修改): " if is_secret else f"編輯 {key}: "
                new_val = await ctx.reader.read(prompt_msg, default=default_edit, is_password=is_secret)

                if new_val is not None and new_val.strip() and new_val != current_val:
                    res = sys_registry.execute(f"/config set {key} {new_val.strip()}", ctx.agent)
                    ctx.renderer.message(res.message, "cyan")

        elif result.status == "interactive_menu":
            if self.name == "model":
                await do_model_flow(ctx, args)
        elif result.status == "learn_interactive":
            ctx.renderer.message("正在萃取對話知識，請稍候...", "dim")
            skill_content = await ctx.agent.extract_skill()

            from pocket_agent.system.registry import parse_frontmatter

            meta, body = parse_frontmatter(skill_content)
            suggested_name = meta.get("name", "")
            suggested_desc = meta.get("description", "")

            from pocket_agent.extensions.skills import save_skill

            ctx.renderer.console.print(Markdown(skill_content))

            skill_name = await ctx.reader.read(
                "請輸入要儲存的 Skill 名稱 (英數字中折線，例如 my-skill，留空取消): ", default=suggested_name
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

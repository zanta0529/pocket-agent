import difflib
from pathlib import Path

from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text

from pocket_agent.frontend.cli.commands.core import CliCommand, CommandContext, CommandResult
from pocket_agent.system.i18n import t


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
        raise ValueError(t("system_cmd.model_usage", providers="|".join(SUPPORTED_PROVIDERS)))
    new_client = get_provider_client(provider=provider, model=model)
    old_client = agent.client
    # No await between construction and commit: all consumers see one state.
    agent.client, agent.model, agent.provider = new_client, model, provider
    state.provider, state.model = provider, model
    renderer.message(t("system_cmd.model_switched", provider=provider, model=model), "green")

    from pocket_agent.system.config import config

    if not config.get("system.context_window"):
        try:
            if hasattr(agent, "spawn_background_task"):
                agent.spawn_background_task(agent.engine.fetch_context_window())
        except Exception:
            pass

    try:
        await old_client.close()
    except Exception:
        renderer.message(t("system_cmd.model_close_warn"), "yellow")


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
            raise ValueError(t("system_cmd.model_usage_opt", providers="|".join(SUPPORTED_PROVIDERS)))
    else:
        # 轉換為 display_name 顯示
        display_choices = [PROVIDER_STRATEGIES[p].display_name for p in SUPPORTED_PROVIDERS]
        chosen_display = await ctx.reader.choose(t("system_cmd.model_choose_provider"), display_choices)
        if not chosen_display:
            return

        # 轉回 internal ID
        provider = next((p for p in SUPPORTED_PROVIDERS if PROVIDER_STRATEGIES[p].display_name == chosen_display), None)
        if not provider or provider not in SUPPORTED_PROVIDERS:
            raise ValueError(t("system_cmd.model_unsupported_provider", providers=", ".join(SUPPORTED_PROVIDERS)))

        try:
            models = fetch_available_models(provider)
        except Exception:
            models = []
        if not models:
            ctx.renderer.message(t("system_cmd.model_fetch_failed"), "yellow")
        model = await ctx.reader.choose(t("system_cmd.model_choose_name"), models)
        if not model:
            return
    await switch_model(ctx.agent, ctx.state, provider, model, ctx.renderer)


class ReasoningCommand(CliCommand):
    """在對話中即時切換 LLM 推理強度 (auto / high / medium / low / none)。"""

    _CHOICE_KEYS = (
        ("auto", "system_cmd.reasoning_auto"),
        ("high", "system_cmd.reasoning_high"),
        ("medium", "system_cmd.reasoning_medium"),
        ("low", "system_cmd.reasoning_low"),
        ("none", "system_cmd.reasoning_none"),
    )

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from pocket_agent.system.constants import VALID_REASONING_EFFORTS

        raw = args.strip().lower()
        if not raw:
            current = str(getattr(ctx.agent, "reasoning_effort", "auto") or "auto").lower()
            choices_resolved = [(key, t(i18n_key)) for key, i18n_key in self._CHOICE_KEYS]
            labels = [label for _, label in choices_resolved]
            selected = await ctx.reader.choose(
                t("system_cmd.reasoning_choose", current=current.capitalize()),
                labels,
            )
            if not selected:
                return CommandResult()
            level = next((key for key, label in choices_resolved if label == selected), None)
            if not level:
                return CommandResult()
        else:
            if raw not in VALID_REASONING_EFFORTS:
                raise ValueError(t("system_cmd.reasoning_usage", efforts="|".join(VALID_REASONING_EFFORTS)))
            level = raw

        ctx.agent.reasoning_effort = level
        ctx.state.reasoning_effort = level
        ctx.renderer.message(t("system_cmd.reasoning_set", level=level.capitalize()), "green")
        return CommandResult()


class AddDirCommand(CliCommand):
    """授權額外工作區目錄（/add-dir <path>）。"""

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if not args.strip():
            raise ValueError(t("system_cmd.add_dir_usage"))
        status, msg, target_dir = ctx.agent.check_new_workspace_dir(args.strip())
        if status in ("invalid", "not_found"):
            raise ValueError(msg)
        if status in ("in_default", "already_authorized"):
            ctx.renderer.message(f"✓ {msg}", "green")
            return CommandResult()

        warn_text = Text()
        warn_text.append(t("system_cmd.add_dir_warning"), style="bold red")
        warn_text.append(t("system_cmd.add_dir_confirm_msg", msg=msg))
        ctx.renderer.console.print(warn_text)

        allow_label = t("system_cmd.allow")
        deny_label = t("system_cmd.deny")
        confirm = await ctx.reader.choose(t("system_cmd.choose_prompt"), [allow_label, deny_label])
        if confirm == allow_label and target_dir is not None:
            ctx.agent.add_workspace_dir(target_dir)
            ctx.renderer.message(t("system_cmd.add_dir_authorized", path=target_dir), "green")
        else:
            ctx.renderer.message(t("system_cmd.add_dir_denied"), "yellow")
        return CommandResult()


class RmDirCommand(CliCommand):
    """移除額外授權的工作區目錄（/rm-dir [<path>]）。"""

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        target = args.strip()
        if not target:
            dirs = list(getattr(ctx.agent, "workspace_dirs", []))
            if not dirs:
                ctx.renderer.message(t("system_cmd.rm_dir_empty"))
                return CommandResult()
            cancel_label = t("session_cmd.cancel")
            selected = await ctx.reader.choose(t("system_cmd.rm_dir_choose"), [*dirs, cancel_label])
            if not selected or selected == cancel_label:
                ctx.renderer.message(t("system_cmd.rm_dir_cancelled"))
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
        lines = [t("system_cmd.workspace_default"), f"  {Path.cwd().resolve()}"]
        if ctx.agent.workspace_dirs:
            lines.append(t("system_cmd.workspace_extra"))
            for wd in ctx.agent.workspace_dirs:
                lines.append(f"  {wd}")
        else:
            lines.append(t("system_cmd.workspace_extra_none"))
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

        ctx.renderer.message(t("system_cmd.learn_extracting"), "dim")
        skill_content = await ctx.agent.extract_skill()
        meta, body = parse_frontmatter(skill_content)
        suggested_name = meta.get("name", "")
        suggested_desc = meta.get("description", "")

        ctx.renderer.console.print(Markdown(skill_content))

        skill_name = await ctx.reader.read(
            t("system_cmd.learn_name_prompt"),
            default=suggested_name,
        )
        if not skill_name:
            ctx.renderer.message(t("system_cmd.learn_cancelled"))
            return CommandResult()

        skill_desc = await ctx.reader.read(t("system_cmd.learn_desc_prompt"), default=suggested_desc)
        scope = await ctx.reader.choose(t("system_cmd.learn_scope_choose"), ["workspace", "global"])
        if scope:
            save_path = save_skill(skill_name.strip(), skill_desc.strip(), body.strip(), scope)
            ctx.renderer.message(t("system_cmd.learn_saved", path=save_path), "green")
        else:
            ctx.renderer.message(t("system_cmd.learn_cancelled"))
        return CommandResult()


class UndoCommand(CliCommand):
    """檢視差異並還原最近一次備份之檔案（/undo [<path>]）。"""

    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from pocket_agent.system.sys_commands import sys_registry

        target = args.strip()
        if target:
            res = sys_registry.execute(f"/undo {target}", ctx.agent)
            if res is None or res.status == "error":
                raise ValueError(res.message if res else t("system_cmd.undo_failed"))
            ctx.renderer.message(res.message, "green")
            return CommandResult()

        res = sys_registry.execute("/undo", ctx.agent)
        if res is None or res.status == "error":
            raise ValueError(res.message if res else t("system_cmd.undo_no_backup"))

        mapping = res.payload.get("mapping", {})
        if not mapping:
            ctx.renderer.message(t("system_cmd.undo_no_backup_msg"))
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
        cancel_label = t("session_cmd.cancel")
        choices.append(cancel_label)

        selected = await ctx.reader.choose(t("system_cmd.undo_choose"), choices)
        if not selected or selected == cancel_label:
            ctx.renderer.message(t("system_cmd.undo_cancelled"))
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
                ctx.renderer.message(t("system_cmd.undo_no_backup_msg"), "yellow")
                return CommandResult()
            ctx.renderer.console.print(f"\n[bold cyan]{t('system_cmd.undo_diff_header')}[/bold cyan]")
            ctx.renderer.console.print(Syntax("".join(diff), "diff", theme="monokai", background_color="default"))
            ctx.renderer.console.print("[bold cyan]───────────────────────────────────────────────[/bold cyan]\n")
        except UnicodeDecodeError:
            ctx.renderer.message(t("system_cmd.undo_diff_binary"), "yellow")
        except Exception as exc:
            ctx.renderer.message(t("system_cmd.undo_diff_error", error=exc), "yellow")

        confirm_yes = t("system_cmd.undo_confirm_yes")
        confirm = await ctx.reader.choose(t("system_cmd.undo_confirm_prompt"), [confirm_yes, cancel_label])
        if confirm != confirm_yes:
            ctx.renderer.message(t("system_cmd.undo_cancelled"))
            return CommandResult()

        restore_result = sys_registry.execute(f"/undo {orig_path_str}", ctx.agent)
        if restore_result and restore_result.status == "success":
            ctx.renderer.message(restore_result.message, "green")
        else:
            ctx.renderer.message(
                restore_result.message if restore_result else t("system_cmd.undo_failed"),
                "red",
            )
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
                raise ValueError(res.message if res else t("system_cmd.config_cmd_failed"))
            ctx.renderer.console.print(res.message)
            return CommandResult()

        add_item_label = t("system_cmd.config_add_item")
        exit_label = t("system_cmd.config_exit")

        while True:
            flat_data = self._flatten(config.to_dict())
            choices = []
            keys_list = []
            for k, v in flat_data.items():
                if self._is_secret_key(k):
                    display_v = f"{v[:3]}********{v[-3:]}" if isinstance(v, str) and len(v) > 6 else "********"
                else:
                    display_v = str(v)
                choices.append(f"{k} = {display_v}")
                keys_list.append(k)

            choices.extend([add_item_label, exit_label])
            selected = await ctx.reader.choose(t("system_cmd.config_menu_title"), choices)
            if not selected or selected == exit_label:
                ctx.renderer.message(t("system_cmd.config_exited"))
                return CommandResult()

            if selected == add_item_label:
                new_key = await ctx.reader.read(t("system_cmd.config_new_key_prompt"))
                if not new_key:
                    continue
                is_secret = self._is_secret_key(new_key)
                new_val = await ctx.reader.read(
                    t("system_cmd.config_new_val_prompt", key=new_key),
                    is_password=is_secret,
                )
                if new_val:
                    res = sys_registry.execute(f"/config set {new_key.strip()} {new_val.strip()}", ctx.agent)
                    if res:
                        ctx.renderer.message(res.message, "cyan")
                continue

            idx = choices.index(selected)
            key = keys_list[idx]
            current_val = str(flat_data[key])
            is_secret = self._is_secret_key(key)

            if key == "locale":
                from pocket_agent.system.i18n import get_available_locales

                available_locales = get_available_locales()
                new_val = await ctx.reader.choose(t("system_cmd.config_choose_key", key=key), available_locales)
            else:
                prompt_msg = (
                    t("system_cmd.config_edit_secret", key=key)
                    if is_secret
                    else t("system_cmd.config_edit_normal", key=key)
                )
                new_val = await ctx.reader.read(
                    prompt_msg, default="" if is_secret else current_val, is_password=is_secret
                )

            if new_val is not None and new_val.strip() and new_val != current_val:
                res = sys_registry.execute(f"/config set {key} {new_val.strip()}", ctx.agent)
                if res:
                    ctx.renderer.message(res.message, "cyan")

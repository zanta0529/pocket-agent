import time
import uuid

from pocket_agent.frontend.cli.commands.core import CliCommand, CommandContext, CommandResult


class SessionCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        from pocket_agent.core.sessions import (
            delete_session,
            list_sessions,
            load_session,
            save_session,
        )

        parts = args.split(maxsplit=1)
        sub = parts[0] if parts else ""
        sub_arg = parts[1] if len(parts) > 1 else ""

        if sub in ("", "list"):
            sessions = list_sessions()
            if not sessions:
                ctx.renderer.message("尚無歷史對話。")
                return CommandResult()

            if sub == "list":
                for i, s in enumerate(sessions, 1):
                    dt = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["updated_at"]))
                    ctx.renderer.message(f"{i:2}. [{s['id']}]  {s['title']}  ({dt})")
                return CommandResult()

            page = 0
            page_size = 5
            selected = None

            while True:
                start = page * page_size
                end = start + page_size
                chunk = sessions[start:end]

                choices = []
                for s in chunk:
                    dt = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["updated_at"]))
                    choices.append(f"{dt}  {s['title'][:40]}")

                more_label = "↓ 載入更多歷史紀錄..."
                if end < len(sessions):
                    choices.append(more_label)

                selected_label = await ctx.reader.choose("選擇對話（↑↓ 移動光棒，Enter 確認，Esc 取消）", choices)

                if not selected_label:
                    return CommandResult()

                if selected_label == more_label:
                    page += 1
                    continue

                if selected_label in choices:
                    idx_in_chunk = choices.index(selected_label)
                    selected = chunk[idx_in_chunk]
                    break
                else:
                    return CommandResult()

            action = await ctx.reader.choose(
                f"「{selected['title'][:30]}」→ 選擇動作",
                ["載入", "刪除", "取消"],
            )
            if not action or action == "取消":
                return CommandResult()

            if action == "載入":
                data = load_session(selected["id"])
                if data is None:
                    raise ValueError(f"找不到 session：{selected['id']}")
                if ctx.agent.history:
                    save_session(ctx.state.session_id, ctx.agent.history)
                ctx.agent.history = data["history"]
                ctx.state.session_id = data["id"]
                ctx.renderer.message(
                    f"已載入：{data.get('title', '（無標題）')} · {len(data['history'])} 則訊息",
                    "green",
                )

            elif action == "刪除":
                data = load_session(selected["id"])
                title = data.get("title", selected["id"]) if data else selected["id"]
                delete_session(selected["id"])
                if ctx.state.session_id == selected["id"]:
                    ctx.agent.history = []
                    ctx.state.session_id = str(uuid.uuid4())
                ctx.renderer.message(f"已刪除：{title}")

        elif sub == "load":
            if not sub_arg:
                raise ValueError("用法：/session load <id>")
            data = load_session(sub_arg)
            if data is None:
                raise ValueError(f"找不到 session：{sub_arg}")
            if ctx.agent.history:
                save_session(ctx.state.session_id, ctx.agent.history)
            ctx.agent.history = data["history"]
            ctx.state.session_id = data["id"]
            ctx.renderer.message(
                f"已載入：{data.get('title', '（無標題）')} · {len(data['history'])} 則訊息",
                "green",
            )

        elif sub == "save":
            if not ctx.agent.history:
                ctx.renderer.message("對話歷史為空，略過儲存。")
            else:
                save_session(ctx.state.session_id, ctx.agent.history)
                ctx.renderer.message(f"已存檔：{ctx.state.session_id}", "green")

        elif sub == "delete":
            if not sub_arg:
                raise ValueError("用法：/session delete <id>")
            data = load_session(sub_arg)
            title = data.get("title", sub_arg) if data else sub_arg
            delete_session(sub_arg)
            if ctx.state.session_id == sub_arg:
                ctx.agent.history = []
                ctx.state.session_id = str(uuid.uuid4())
            ctx.renderer.message(f"已刪除：{title}")
        else:
            raise ValueError("用法：/session [list | load <id> | save | delete <id>]")

        return CommandResult()


class CompactCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError("/compact 不接受參數")
        history = ctx.agent.history
        if len(history) < 6:
            ctx.renderer.message("對話歷史不足（需至少 6 則），無需壓縮。")
            return CommandResult()

        ctx.renderer.message("壓縮中…（正在建立高密度對話摘要）", "dim")
        original_len = len(history)
        keep_recent = 2 if len(history) < 8 else 4
        try:
            success, orig_tokens, comp_tokens = await ctx.agent.compact_history(keep_recent=keep_recent)
        except Exception as e:
            ctx.renderer.message(f"壓縮失敗：{e}", "red")
            return CommandResult()

        if not success:
            ctx.renderer.message("目前歷史對話結構過短或無顯著壓縮收益，歷史未變更。", "yellow")
            return CommandResult()

        saved_tokens = max(0, orig_tokens - comp_tokens)
        ctx.renderer.message(
            f"✓ 已壓縮：{original_len} 則 → {len(ctx.agent.history)} 則 "
            f"（原約 {orig_tokens:,} tokens → 壓縮後約 {comp_tokens:,} tokens，"
            f"釋放約 {saved_tokens:,} tokens）",
            "green",
        )
        return CommandResult()


class NewCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        if args:
            raise ValueError("/new 不接受參數")
        from pocket_agent.core.sessions import new_session_id, save_session

        old_session_id = ctx.state.session_id
        # 若原先對話有內容，先自動儲存保護工作成果
        if ctx.agent.history:
            try:
                save_session(old_session_id, ctx.agent.history)
                saved_msg = f"（已自動儲存前次會話：{old_session_id}）"
            except Exception as e:
                saved_msg = f"（前次會話自動存檔失敗：{e}）"
        else:
            saved_msg = ""

        # 徹底清空當前上下文與會話狀態
        ctx.agent.clear_history()
        ctx.state.session_id = new_session_id()
        ctx.state.pending_files.clear()
        ctx.state.last_failed_input = ""
        ctx.state.session_tokens = 0
        ctx.state.session_approved_commands.clear()
        ctx.agent.context_usage = None
        ctx.agent.last_images.clear()
        ctx.agent.last_files.clear()
        if hasattr(ctx.agent, "user_instructions") and isinstance(ctx.agent.user_instructions, list):
            ctx.agent.user_instructions.clear()

        info = f"✓ 已開啟全新對話（Session ID: {ctx.state.session_id}）· 上下文與暫存已完整清空。"
        if saved_msg:
            info += f"\n  {saved_msg}"
        ctx.renderer.message(info, "green")
        return CommandResult()


class RenameCommand(CliCommand):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        new_title = args.strip()
        if not new_title:
            try:
                prompt_input = await ctx.reader.read("請輸入新的對話標題（或按 Ctrl+C 取消）：")
                new_title = prompt_input.strip()
            except KeyboardInterrupt, EOFError:
                ctx.renderer.message("已取消重新命名。")
                return CommandResult()

        if not new_title:
            ctx.renderer.message("標題不得為空，已取消重新命名。", "yellow")
            return CommandResult()

        clean_title = new_title.replace("\n", " ")[:60]
        from pocket_agent.core.sessions import rename_session, save_session

        # 若檔案已存在於磁碟則直接更新；否則以當前 history 儲存帶標題之 session
        renamed = rename_session(ctx.state.session_id, clean_title)
        if not renamed:
            save_session(ctx.state.session_id, ctx.agent.history, title=clean_title)

        ctx.renderer.message(f"✓ 已將目前對話重新命名為：「{clean_title}」", "green")
        return CommandResult()

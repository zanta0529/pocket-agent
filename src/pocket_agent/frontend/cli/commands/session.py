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

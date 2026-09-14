from dataclasses import dataclass
from typing import TYPE_CHECKING

from .cli_input import BUILTINS, CliInput, file_argument
from .cli_render import CliRenderer
from .cli_state import CliState

if TYPE_CHECKING:
    from agent.core import Agent


@dataclass
class CommandResult:
    action: str = "handled"
    prompt: str = ""


async def switch_model(
    agent: "Agent",
    state: CliState,
    provider: str,
    model: str,
    renderer: CliRenderer,
) -> None:
    from agent.auth import get_openai_client
    from agent.config import SUPPORTED_PROVIDERS

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


class CommandHandler:
    def __init__(
        self,
        agent: "Agent",
        state: CliState,
        reader: CliInput,
        renderer: CliRenderer,
        tools: list[str],
    ):
        self.agent = agent
        self.state = state
        self.reader = reader
        self.renderer = renderer
        self.tools = tools

    async def model(self, args: str) -> None:
        from agent.auth import fetch_available_models
        from agent.config import SUPPORTED_PROVIDERS

        if args:
            parts = args.split()
            if len(parts) != 2:
                raise ValueError(f"用法：/model <{'|'.join(SUPPORTED_PROVIDERS)}> <模型名稱>")
            provider, model = parts
        else:
            provider = await self.reader.choose(f"提供者 {' / '.join(SUPPORTED_PROVIDERS)}", SUPPORTED_PROVIDERS)
            if not provider:
                return
            if provider not in SUPPORTED_PROVIDERS:
                raise ValueError(f"僅支援 {', '.join(SUPPORTED_PROVIDERS)}")
            try:
                models = fetch_available_models(provider)
            except Exception:
                models = []
            if not models:
                self.renderer.message("無法取得模型清單，請手動輸入名稱。", "yellow")
            model = await self.reader.choose("模型名稱 · Tab／方向鍵補全", models)
            if not model:
                return
        await switch_model(self.agent, self.state, provider, model, self.renderer)

    def attachments(self, args: str) -> None:
        parts = args.split()
        if not parts:
            if not self.state.pending_files:
                self.renderer.message("沒有待送附件。")
            for i, path in enumerate(self.state.pending_files, 1):
                self.renderer.message(f"{i}. {path}")
        elif parts == ["clear"]:
            self.state.pending_files.clear()
            self.renderer.message("已清空待送佇列，未刪除實體檔案。")
        elif len(parts) == 2 and parts[0] == "remove":
            try:
                number = int(parts[1])
            except ValueError as exc:
                raise ValueError("附件編號必須是整數") from exc
            self.renderer.message(f"已移除待送附件：{self.state.remove(number)}")
        else:
            raise ValueError("用法：/attachments [clear|remove <編號>]")

    async def session(self, args: str) -> None:
        from agent.sessions import (
            delete_session,
            list_sessions,
            load_session,
            save_session,
        )

        parts = args.split(maxsplit=1)
        sub = parts[0] if parts else ""
        sub_arg = parts[1] if len(parts) > 1 else ""

        if sub in ("", "list"):
            import time

            sessions = list_sessions()
            if not sessions:
                self.renderer.message("尚無歷史對話。")
                return

            if sub == "list":
                # 文字清單模式（/session list）：維持原有輸出
                for i, s in enumerate(sessions, 1):
                    dt = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["updated_at"]))
                    self.renderer.message(f"{i:2}. [{s['id']}]  {s['title']}  ({dt})")
                return

            # 互動模式（/session，無參數）：直接以光棒選取對話（分頁顯示）
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

                selected_label = await self.reader.choose("選擇對話（↑↓ 移動光棒，Enter 確認，Esc 取消）", choices)

                if not selected_label:
                    return  # 使用者 Esc 取消

                if selected_label == more_label:
                    page += 1
                    continue

                if selected_label in choices:
                    idx_in_chunk = choices.index(selected_label)
                    selected = chunk[idx_in_chunk]
                    break
                else:
                    return  # 輸入了不匹配的字串

            # ── 第三段：選取動作 ──────────────────────────
            action = await self.reader.choose(
                f"「{selected['title'][:30]}」→ 選擇動作",
                ["載入", "刪除", "取消"],
            )
            if not action or action == "取消":
                return

            if action == "載入":
                data = load_session(selected["id"])
                if data is None:
                    raise ValueError(f"找不到 session：{selected['id']}")
                self.agent.history = data["history"]
                self.state.session_id = data["id"]
                self.renderer.message(
                    f"已載入：{data.get('title', '（無標題）')} · {len(data['history'])} 則訊息",
                    "green",
                )

            elif action == "刪除":
                data = load_session(selected["id"])
                title = data.get("title", selected["id"]) if data else selected["id"]
                delete_session(selected["id"])
                self.renderer.message(f"已刪除：{title}")

        elif sub == "load":
            if not sub_arg:
                raise ValueError("用法：/session load <id>")
            data = load_session(sub_arg)
            if data is None:
                raise ValueError(f"找不到 session：{sub_arg}")
            self.agent.history = data["history"]
            self.state.session_id = data["id"]
            self.renderer.message(
                f"已載入：{data.get('title', '（無標題）')} · {len(data['history'])} 則訊息",
                "green",
            )

        elif sub == "save":
            if not self.agent.history:
                self.renderer.message("對話歷史為空，略過儲存。")
            else:
                save_session(self.state.session_id, self.agent.history)
                self.renderer.message(f"已存檔：{self.state.session_id}", "green")

        elif sub == "delete":
            if not sub_arg:
                raise ValueError("用法：/session delete <id>")
            data = load_session(sub_arg)
            title = data.get("title", sub_arg) if data else sub_arg
            delete_session(sub_arg)
            self.renderer.message(f"已刪除：{title}")

        else:
            raise ValueError("用法：/session [list | load <id> | save | delete <id>]")

    async def compact(self) -> None:
        """摘要並壓縮中間對話歷史，回收 context 空間。"""
        history = self.agent.history
        if len(history) < 4:
            self.renderer.message("對話歷史不足（需至少 4 則），無需壓縮。")
            return

        # 擷取中間歷史（排除首 2 和末 2 則）用於摘要
        middle = history[2:-2]
        middle_text = "\n".join(f"[{m['role']}] {str(m.get('content', ''))[:300]}" for m in middle)
        summary_prompt = "請用繁體中文，以 3–6 句話摘要以下對話的核心要點，讓後續對話能銜接前文脈絡：\n\n" + middle_text

        self.renderer.message("壓縮中…（正在向模型請求摘要）", "dim")
        try:
            from agent.core import Agent

            # 採用 Adapter 精神：建立一個輕量的子代理專門做摘要，
            # 讓底層 EngineFactory 去處理 OpenAI 或 Ollama 的 API 差異。
            summarizer = Agent(
                client=self.agent.client,
                mcp=self.agent.mcp,
                model=self.agent.model,
                system_prompt=(
                    "你是一個精準的對話摘要助手。請直接輸出摘要結果，不要加上任何解釋、問候語或 Markdown 標籤。"
                ),
                is_subagent=True,
                reasoning_effort="none",  # 摘要任務不需深度推理
            )

            # 使用標準的 chat() 介面，它會回傳 (thinking, reply, usage_str)
            _, summary, _ = await summarizer.chat(summary_prompt)

            if not summary or not summary.strip():
                raise ValueError("模型未回傳有效的摘要內容")
        except Exception as e:
            self.renderer.message(f"壓縮失敗：{e}", "red")
            return

        if not summary.strip():
            self.renderer.message("摘要結果為空，歷史未變更。", "yellow")
            return

        original_len = len(history)
        notice = {
            "role": "system",
            "content": f"[System] 以下為對話摘要（已壓縮 {original_len - 4} 則）：\n{summary.strip()}",
        }
        self.agent.history = history[:2] + [notice] + history[-2:]
        self.renderer.message(f"✓ 已壓縮：{original_len} 則 → {len(self.agent.history)} 則", "green")

    async def dispatch(self, raw: str) -> CommandResult:
        text = raw.strip()
        if not text.startswith("/"):
            return CommandResult("chat", raw)
        head, *tail = text.split(maxsplit=1)
        name, args = head[1:], tail[0] if tail else ""
        if name in ("quit", "logout", "clear", "compact", "help", "tools") and args:
            raise ValueError(f"/{name} 不接受參數")
        if name == "quit":
            return CommandResult("quit")
        if name == "logout":
            from agent.auth import logout

            logout()
            return CommandResult("quit")
        if name == "clear":
            self.agent.clear_history()
            self.renderer.message("已清除對話；待送附件保留。")
        elif name == "file":
            for path in self.state.add_files([file_argument(args)]):
                self.renderer.message(f"已加入待送：{path}")
        elif name == "attachments":
            self.attachments(args)
        elif name == "add-dir":
            if not args:
                raise ValueError("用法：/add-dir <目錄路徑>")

            status, msg, target_dir = self.agent.check_new_workspace_dir(args)
            if status in ("invalid", "not_found"):
                raise ValueError(msg)
            if status in ("in_default", "already_authorized"):
                self.renderer.message(f"✓ {msg}")
                return CommandResult()

            from rich.text import Text

            warn_text = Text()
            warn_text.append("【安全警告】", style="bold red")
            warn_text.append(f"{msg}是否授權本次會話讀寫該目錄？")
            self.renderer.console.print(warn_text)

            confirm = await self.reader.choose("請選擇", ["允許", "拒絕"])
            if confirm == "允許" and target_dir:
                self.agent.add_workspace_dir(target_dir)
                self.renderer.message(f"✓ 已授權：{target_dir}，可透過工具讀寫。", "green")
            else:
                self.renderer.message("⚠ 已拒絕授權。", "yellow")
        elif name == "model":
            await self.model(args)
        elif name == "session":
            await self.session(args)
        elif name == "compact":
            await self.compact()
        elif name == "tools":
            self.renderer.message("MCP 工具：" + (", ".join(self.tools) or "無"))
        elif name == "skills":
            from pathlib import Path

            from rich.markdown import Markdown
            from rich.text import Text

            from agent.paths import bundle_dir, user_dir
            from agent.skills import list_skills, load_skill

            if args:
                skill_name = args.strip()
                body = load_skill(skill_name, self.agent.workspace_dirs)
                if body:
                    self.renderer.console.print(f"\n[bold white]Skill: {skill_name}[/bold white]")
                    self.renderer.console.print(Markdown(body))
                else:
                    self.renderer.message(f"無法載入技能 {skill_name}")
                return CommandResult()

            skills = list_skills(self.agent.workspace_dirs)
            if not skills:
                self.renderer.message("目前沒有可用的全域或專案技能。")
                return CommandResult()

            self.renderer.console.print()
            header = Text("Skills\n", style="bold white")
            header.append(f"{len(skills)} skills", style="dim")
            self.renderer.console.print(header)
            self.renderer.console.print()

            create = Text("Create new skills\n", style="bold white")
            create.append("  Workspace: ", style="dim")
            create.append(f"{Path.cwd().resolve().as_posix()}/.agents/skills/{{skill_name}}/SKILL.md\n", style="blue")
            create.append("  Global:    ", style="dim")
            create.append("~/.pocket-agent/skills/{skill_name}/SKILL.md\n", style="blue")
            self.renderer.console.print(create)

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
                t.append(f"{title} ", style="bold white")
                t.append(f"• {subtitle}", style="dim")
                self.renderer.console.print(t)
                for item in items:
                    line = Text()
                    line.append(f"  {item['name']}", style="blue")
                    line.append(f": {item.get('description', '')}")
                    self.renderer.console.print(line)
                self.renderer.console.print()

            print_group("Workspace skills", "Workspace config", workspace_skills)
            print_group("Global skills", "Global config", global_skills)
            print_group("Built-in skills", "Built-in", builtin_skills)

            self.renderer.message("提示：若要檢視某個技能的完整內容，請輸入 /skills <skill_name>", "dim")
        elif name == "workspace":
            from pathlib import Path

            self.renderer.message("【預設工作區】")
            self.renderer.message(f"  {Path.cwd().resolve()}")
            if self.agent.workspace_dirs:
                self.renderer.message("【額外授權目錄】")
                for wd in self.agent.workspace_dirs:
                    self.renderer.message(f"  {wd}")
            else:
                self.renderer.message("【額外授權目錄】無")
        elif name == "help":
            from agent.commands import list_commands

            for command, description in BUILTINS.items():
                self.renderer.message(f"/{command}  {description}")
            if self.state.provider == "openai":
                self.renderer.message("/image <描述>  生圖，待送附件作參考圖")
            self.renderer.message("/attachments remove <編號> · /attachments clear")
            self.renderer.message("Enter 送出 · Esc 後 Enter 換行 · Ctrl+C 清空輸入 · 空輸入 Ctrl+D 離開")
            for command in list_commands():
                self.renderer.message("/" + command["name"] + "  " + command["description"])
        elif name == "image":
            if self.state.provider != "openai":
                raise ValueError("/image 目前僅支援 openai")
            if not args:
                raise ValueError("用法：/image <描述>")
            return CommandResult("image", args)
        else:
            from agent.commands import expand_command, get_command
            from agent.skills import list_skills

            if get_command(name) is not None:
                expanded = expand_command("/" + name + (" " + args if args else ""))
                if expanded is None or not expanded.strip():
                    raise ValueError("指令模板無法展開或內容為空；待送附件保留")
                return CommandResult("chat", expanded)

            # 檢查是否為 Skill (將 /skill-name 轉為呼叫指令)
            skills_dict = {s["name"]: s for s in list_skills(self.agent.workspace_dirs)}
            if name in skills_dict:
                prompt = f"請載入並使用 `{name}` 技能來處理此任務"
                if args:
                    prompt += f"：\n{args}"
                else:
                    prompt += "。"
                return CommandResult("chat", prompt)

            raise ValueError(f"找不到指令或技能 /{name}；輸入 /help 查看說明")
        return CommandResult()

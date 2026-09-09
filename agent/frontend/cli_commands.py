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

    if provider not in ("openai", "ollama") or not model or any(c.isspace() for c in model):
        raise ValueError("用法：/model <openai|ollama> <模型名稱>")
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

        if args:
            parts = args.split()
            if len(parts) != 2:
                raise ValueError("用法：/model <openai|ollama> <模型名稱>")
            provider, model = parts
        else:
            provider = await self.reader.choose("提供者 openai / ollama", ["openai", "ollama"])
            if not provider:
                return
            if provider not in ("openai", "ollama"):
                raise ValueError("僅支援 openai 或 ollama")
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

    def session(self, args: str) -> None:
        from agent.sessions import (
            delete_session,
            list_sessions,
            load_session,
            save_session,
        )

        parts = args.split(maxsplit=1)
        sub = parts[0] if parts else "list"
        sub_arg = parts[1] if len(parts) > 1 else ""

        if sub in ("", "list"):
            sessions = list_sessions()[:10]
            if not sessions:
                self.renderer.message("尚無歷史對話。")
                return
            import time

            for i, s in enumerate(sessions, 1):
                dt = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["updated_at"]))
                self.renderer.message(f"{i:2}. [{s['id']}]  {s['title']}  ({dt})")

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

    async def dispatch(self, raw: str) -> CommandResult:
        text = raw.strip()
        if not text.startswith("/"):
            return CommandResult("chat", raw)
        head, *tail = text.split(maxsplit=1)
        name, args = head[1:], tail[0] if tail else ""
        if name in ("quit", "logout", "clear", "help", "tools") and args:
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
            self.renderer.message("目錄工作範圍尚未接入，不會讀取目錄；請用 /file 或 @ 附加檔案。", "yellow")
        elif name == "model":
            await self.model(args)
        elif name == "session":
            self.session(args)
        elif name == "tools":
            self.renderer.message("MCP 工具：" + (", ".join(self.tools) or "無"))
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

            if get_command(name) is None:
                raise ValueError(f"找不到指令 /{name}；輸入 /help 查看說明")
            expanded = expand_command("/" + name + (" " + args if args else ""))
            if expanded is None or not expanded.strip():
                raise ValueError("指令模板無法展開或內容為空；待送附件保留")
            return CommandResult("chat", expanded)
        return CommandResult()

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from pocket_agent.frontend.base import FrontendStrategy

log = logging.getLogger("pocket-agent")

if TYPE_CHECKING:
    from pocket_agent.core.agent import Agent
    from pocket_agent.frontend.cli.commands import CommandResult
    from pocket_agent.frontend.cli.input import CliInput
    from pocket_agent.frontend.cli.render import CliRenderer
    from pocket_agent.frontend.cli.state import CliState


def _format_usage_limit_hint(exc: BaseException) -> list[str] | None:
    """解析 OpenAI RateLimitError 中的 usage_limit_reached 資訊，回傳人性化說明行列表。

    先嘗試 exc.body（OpenAI SDK >= 1.x 的結構化屬性），
    再從 str(exc) fallback 解析（適用於 body 為 None 的情境）。
    時間以本機時區顯示；若取不到 resets_at 則省略時間資訊。
    回傳 list[str]（每個元素為獨立一行），呼叫端逐行呼叫 renderer.message() 以確保換行。
    """
    import ast
    import re

    def _extract_error_dict(exc: BaseException) -> dict | None:
        # 路徑 1：OpenAI SDK >= 1.x 的 .body 屬性
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                return err

        # 路徑 2：從 str(exc) 解析 "Error code: 429 - {...}" 格式
        text = str(exc)
        # 找到第一個 { 並嘗試 ast.literal_eval 解析整段
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                parsed = ast.literal_eval(match.group())
                if isinstance(parsed, dict):
                    err = parsed.get("error")
                    if isinstance(err, dict):
                        return err
            except (ValueError, SyntaxError):
                pass

        return None

    try:
        error = _extract_error_dict(exc)
        if error is None or error.get("type") != "usage_limit_reached":
            return None

        plan_type: str = error.get("plan_type") or "未知"
        resets_at: object = error.get("resets_at")
        resets_in_seconds: object = error.get("resets_in_seconds")

        lines: list[str] = [
            f"💳 API 用量已達上限（方案：{plan_type}）",
            "   本次請求未被計費，輸入已保留，重置後可直接重試。",
        ]
        if isinstance(resets_at, (int, float)) and resets_at > 0:
            reset_dt = datetime.fromtimestamp(resets_at, tz=timezone.utc).astimezone()
            lines.append(f"   重置時間：{reset_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        if isinstance(resets_in_seconds, (int, float)) and resets_in_seconds > 0:
            secs = int(resets_in_seconds)
            hours, remainder = divmod(secs, 3600)
            minutes, seconds = divmod(remainder, 60)
            if hours:
                countdown = f"{hours} 小時 {minutes} 分 {seconds} 秒"
            elif minutes:
                countdown = f"{minutes} 分 {seconds} 秒"
            else:
                countdown = f"{seconds} 秒"
            lines.append(f"   距離重置還有：{countdown}")
        return lines
    except Exception:
        # 解析失敗不中斷正常錯誤流程
        return None


def _autosave_session(agent: "Agent", state: "CliState", renderer: "CliRenderer") -> None:
    """對話結束時自動存檔（失敗不中斷退出）。"""
    from pocket_agent.core.sessions import save_session

    if not agent.history:
        return
    try:
        save_session(state.session_id, agent.history)
    except Exception:
        pass  # 存檔失敗不中斷退出


async def send_turn(
    agent: "Agent",
    state: "CliState",
    renderer: "CliRenderer",
    raw: str,
    result: "CommandResult",
) -> None:
    from pocket_agent.frontend.cli.input import references

    try:
        state.add_files(references(result.prompt))
        snapshot = state.snapshot()
        renderer.user(raw)
        started = monotonic()

        if result.action == "image":
            # 圖片生成無串流事件，保留 status spinner
            from pocket_agent.tools.image import generate_image

            with renderer.console.status("生圖中…", spinner="dots"):
                reply = await generate_image(
                    result.prompt,
                    output_path=Path("generated_images") / "image.png",
                    input_images=snapshot or None,
                )
        else:
            # 串流模式：啟動思考中 spinner，等第一個 text 事件停止它
            from rich.status import Status

            status = Status("思考中…", spinner="dots", console=renderer.console)
            renderer._active_status = status
            status.start()

            chat_task = asyncio.ensure_future(agent.chat(result.prompt, attachments=snapshot))
            try:
                _, reply, _ = await chat_task
            except (asyncio.CancelledError, KeyboardInterrupt):
                agent.request_stop()
                renderer._stop_status()
                renderer.message("⏹ 已停止", "yellow")
                state.last_failed_input = raw
                return
            finally:
                # 若模型未串流任何 text（如 Ollama buffer 全量）：停止 spinner
                renderer._stop_status()

    except Exception as exc:
        state.last_failed_input = raw
        renderer._ensure_newline()
        renderer._stop_status()

        from rich.markdown import Markdown
        from rich.panel import Panel

        err_text = str(exc)
        hint_lines = _format_usage_limit_hint(exc)
        if hint_lines:
            err_text += "\n\n" + "\n".join(hint_lines)

        panel = Panel(
            Markdown(err_text),
            title="⚠️ 本輪未完成 (錯誤)",
            border_style="bold red",
            expand=False,
        )
        renderer.console.print()
        renderer.console.print(panel)
        renderer.message("待送附件保留；本輪可能部分完成，重送前請檢查結果。未自動重試。", "yellow")
        return

    # Normal return is the boundary; the core may return error text normally.
    state.consume(snapshot)
    if result.action == "image":
        renderer.message(f"已存檔：{reply}", "green")
    else:
        turn_tokens = getattr(agent, "total_tokens", 0)
        state.session_tokens += turn_tokens
        renderer.reply(reply, monotonic() - started, turn_tokens, streamed=True)


async def conversation(
    agent: "Agent",
    state: "CliState",
    reader: "CliInput",
    renderer: "CliRenderer",
    tools: list[str],
) -> None:
    from pocket_agent.frontend.cli.commands import CommandHandler

    handler = CommandHandler(agent, state, reader, renderer, tools)
    agent.on_stream = renderer.event
    while True:
        try:
            raw = await reader.read(default=state.last_failed_input)
        except EOFError:
            break
        except KeyboardInterrupt:
            state.last_failed_input = ""
            renderer.message("輸入 /quit 或在空輸入按 Ctrl+D 離開；待送附件保留。")
            continue
        state.last_failed_input = ""
        if not raw.strip():
            continue
        try:
            result = await handler.dispatch(raw)
        except Exception as exc:
            renderer.message(f"指令未執行：{exc}", "red")
            continue
        if result.action == "quit":
            break
        if result.action in ("chat", "image"):
            try:
                await send_turn(agent, state, renderer, raw, result)
            except KeyboardInterrupt:
                # Ctrl+C 在 send_turn 等待期間：中止當前輪次，不退出
                agent.request_stop()
                state.last_failed_input = raw
                renderer.message("⏹ 已停止", "yellow")

    # 對話結束，自動存檔
    _autosave_session(agent, state, renderer)


class CliFrontend(FrontendStrategy):
    """Scrollback-first CLI with separate input and rendering phases."""

    def run(self, system_prompt: str = "You are a helpful assistant.", resume: bool = False) -> None:
        asyncio.run(self._run_cli(system_prompt, resume=resume))

    async def _run_cli(self, system_prompt: str, resume: bool = False) -> None:
        import pathlib

        from rich.console import Console

        from pocket_agent.core.agent import Agent
        from pocket_agent.extensions.mcp import MCPManager
        from pocket_agent.frontend.cli.input import CliInput
        from pocket_agent.frontend.cli.render import CliRenderer
        from pocket_agent.frontend.cli.state import CliState
        from pocket_agent.system.auth import fetch_available_models, get_model, get_openai_client, load_config
        from pocket_agent.system.paths import user_dir

        renderer = CliRenderer(Console())

        # CLI 模式：抑制 StreamHandler INFO，避免 log 訊息干擾 terminal 輸出
        # 若開發者明確設定 DEBUG 等級則尊重，不抑制
        from pocket_agent.system.config import config

        _lvl = config.get("log_level", "INFO").upper()
        if _lvl == "INFO":
            for h in logging.getLogger("pocket-agent").handlers:
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    h.setLevel(logging.WARNING)

        # F1.5：載入全域規範 (Global Instructions)
        global_ctx_file = user_dir() / "instructions.md"
        loaded_global_ctx = False
        if global_ctx_file.exists() and global_ctx_file.is_file():
            try:
                _g_ctx = global_ctx_file.read_text(encoding="utf-8").strip()
                if _g_ctx:
                    system_prompt = system_prompt + "\n\n---\n## 全域規範\n" + _g_ctx
                    loaded_global_ctx = True
            except Exception as e:
                log.warning("全域規範讀取失敗：%s", e)

        # F2：自動載入工作區規範 (依業界標準)
        candidates = [
            pathlib.Path.cwd() / "AGENTS.md",
            pathlib.Path.cwd() / ".agents" / "AGENTS.md",
        ]

        _ctx_file = next((p for p in candidates if p.exists() and p.is_file()), None)
        if _ctx_file:
            try:
                _ctx = _ctx_file.read_text(encoding="utf-8").strip()
                if _ctx:
                    system_prompt = system_prompt + "\n\n---\n## 工作區規範\n" + _ctx
            except Exception as e:
                log.warning("工作區規範讀取失敗：%s", e)

        client = None
        mcp = None
        agent = None
        try:
            renderer.banner()

            if loaded_global_ctx:
                renderer.message(f"已載入全域規範：{global_ctx_file.name}")
            # 若有工作區規範，在 banner 後提示（保持 banner 整潔）
            if _ctx_file and "工作區規範" in system_prompt:
                renderer.message(f"已載入工作區規範：{_ctx_file.name}")

            client = get_openai_client()
            mcp = MCPManager()
            startup = await mcp.start(on_start=lambda count: renderer.message("正在初始化 MCP 服務…"))
            if startup.configured_servers == 0:
                renderer.message("未設定 MCP 服務，略過初始化。")
            elif startup.failed_servers == 0:
                if startup.tool_count > 0:
                    renderer.message(
                        f"MCP 已就緒：已連線 {startup.ready_servers} 個服務，載入 {startup.tool_count} 個工具。"
                    )
                else:
                    renderer.message(f"MCP 已連線 {startup.ready_servers} 個服務，目前無可用工具。")
            elif startup.ready_servers > 0:
                renderer.message(
                    f"MCP 部分就緒：{startup.ready_servers} 個服務初始化成功，載入 {startup.tool_count} 個工具；"
                    f"{startup.failed_servers} 個服務初始化失敗。",
                    "yellow",
                )
            else:
                renderer.message("MCP 初始化失敗，請檢查設定或上述錯誤訊息。", "red")
            state = CliState(load_config().get("provider", "openai"), get_model())
            try:
                available = fetch_available_models(state.provider)
            except Exception:
                available = []
            if available and state.model not in available:
                renderer.message(f"設定模型 {state.model} 不在清單；改用 {available[0]}。", "yellow")
                state.model = available[0]
            elif not available:
                renderer.message("無法取得模型清單，使用目前設定。", "yellow")
            agent = Agent(client=client, mcp=mcp, model=state.model, system_prompt=system_prompt)

            # F3：讓 renderer 能取得 model 名稱顯示在標頭
            renderer._state = state

            # F1：CLI 工具確認機制（三重授權）
            def _cli_confirm(command: str) -> bool:
                from rich.panel import Panel
                from rich.text import Text

                from pocket_agent.system.auth import load_config, save_config

                # 1. 檢查全域「永遠信任」
                config = load_config()
                if config.get("always_allow_shell") is True:
                    return True

                # 2. 檢查「本次 Session 信任」
                if state.session_approved_commands:
                    return True

                renderer._ensure_newline()

                # 暫停 spinner，避免背景更新覆蓋 input 提示字串
                status = renderer._active_status
                if status:
                    status.stop()

                panel = Panel(
                    Text(f"預計執行：\n{command}", style="yellow"),
                    title="【安全警告】系統要求執行指令",
                    border_style="bold red",
                    expand=False,
                )
                renderer.console.print(panel)

                try:
                    ans = input("允許？[y: 單次 | a: 本次對話 | A: 永遠 | N: 拒絕] ").strip()

                    if status:
                        status.start()

                    if ans == "y" or ans == "Y":
                        return True
                    elif ans == "a":
                        state.session_approved_commands = True
                        renderer.message("✓ 已同意本次對話中自動執行指令", "green")
                        return True
                    elif ans == "A":
                        renderer._ensure_newline()
                        renderer.message(
                            "[警告] 您選擇了「永遠允許」，未來所有指令將自動執行。此設定已存於設定檔。", "red"
                        )
                        config["always_allow_shell"] = True
                        save_config(config)
                        return True
                    return False
                except (EOFError, KeyboardInterrupt):
                    if status:
                        status.start()
                    return False

            agent.on_confirm = _cli_confirm

            tools = list(mcp.list_tools())
            renderer.welcome(state, tools)

            # F7：--resume 自動恢復最近 session
            if resume:
                from pocket_agent.core.sessions import list_sessions, load_session

                sessions = list_sessions()
                if sessions:
                    latest = load_session(sessions[0]["id"])
                    if latest and latest.get("history"):
                        agent.history = latest["history"]
                        state.session_id = latest["id"]
                        renderer.message(
                            f"已恢復：{latest.get('title', '（無標題）')} · {len(agent.history)} 則訊息",
                            "green",
                        )
                else:
                    renderer.message("沒有找到歷史 session，以全新對話啟動。", "dim")

            # F6：Ctrl+L 清屏後重印 banner
            await conversation(agent, state, CliInput(state, on_clear_screen=renderer.banner), renderer, tools)
        except Exception as exc:
            renderer.message(f"CLI 執行失敗：{exc}", "red")
        finally:
            # MCP shutdown failure must not skip closing the active model client.
            try:
                if mcp is not None:
                    await mcp.stop()
            finally:
                active = agent.client if agent is not None else client
                if active is not None:
                    try:
                        await active.close()
                    except Exception:
                        renderer.message("模型連線清理失敗。", "yellow")

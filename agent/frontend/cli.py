import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from .base import FrontendStrategy

if TYPE_CHECKING:
    from agent.core import Agent

    from .cli_commands import CommandResult
    from .cli_input import CliInput
    from .cli_render import CliRenderer
    from .cli_state import CliState


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
    from agent.sessions import save_session

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
    from .cli_input import references

    try:
        state.add_files(references(result.prompt))
        snapshot = state.snapshot()
        renderer.user(raw)
        started = monotonic()

        if result.action == "image":
            # 圖片生成無串流事件，保留 status spinner
            from agent.imagegen import generate_image

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
        renderer.message(f"本輪未完成：{exc}", "red")
        hint_lines = _format_usage_limit_hint(exc)
        if hint_lines:
            for line in hint_lines:
                renderer.message(line, "yellow")
        renderer.message("待送附件保留；本輪可能部分完成，重送前請檢查結果。未自動重試。", "yellow")
        return

    # Normal return is the boundary; the core may return error text normally.
    state.consume(snapshot)
    if result.action == "image":
        renderer.message(f"已存檔：{reply}", "green")
    else:
        renderer.reply(reply, monotonic() - started, getattr(agent, "total_tokens", 0), streamed=True)


async def conversation(
    agent: "Agent",
    state: "CliState",
    reader: "CliInput",
    renderer: "CliRenderer",
    tools: list[str],
) -> None:
    from .cli_commands import CommandHandler

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

    def run(self, system_prompt: str = "You are a helpful assistant.") -> None:
        asyncio.run(self._run_cli(system_prompt))

    async def _run_cli(self, system_prompt: str) -> None:
        from rich.console import Console

        from agent.auth import fetch_available_models, get_model, get_openai_client, load_config
        from agent.core import Agent
        from agent.mcp_manager import MCPManager

        from .cli_input import CliInput
        from .cli_render import CliRenderer
        from .cli_state import CliState

        renderer = CliRenderer(Console())

        # CLI 模式：抑制 StreamHandler INFO，避免 log 訊息干擾 terminal 輸出
        # 若開發者明確設定 DEBUG 等級則尊重，不抑制
        if os.getenv("MY_AGENT_LOG_LEVEL", "INFO").upper() == "INFO":
            for h in logging.getLogger("pocket-agent").handlers:
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    h.setLevel(logging.WARNING)

        client = None
        mcp = None
        agent = None
        try:
            renderer.banner()
            client = get_openai_client()
            mcp = MCPManager()
            renderer.message("啟動 MCP servers…")
            await mcp.start()
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
            tools = list(mcp.list_tools())
            renderer.welcome(state, tools)
            await conversation(agent, state, CliInput(state), renderer, tools)
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

import asyncio
import logging
import os
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

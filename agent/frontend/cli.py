import asyncio
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
        with renderer.console.status("處理中…", spinner="dots"):
            if result.action == "image":
                from agent.imagegen import generate_image

                reply = await generate_image(
                    result.prompt,
                    output_path=Path("generated_images") / "image.png",
                    input_images=snapshot or None,
                )
            else:
                _, reply, _ = await agent.chat(result.prompt, attachments=snapshot)
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
        renderer.reply(reply, monotonic() - started, getattr(agent, "total_tokens", 0))


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
            return
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
            return
        if result.action in ("chat", "image"):
            await send_turn(agent, state, renderer, raw, result)


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

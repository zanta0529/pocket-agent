import asyncio
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from rich.console import Console

from agent.frontend.cli import CliFrontend, conversation, send_turn
from agent.frontend.cli_commands import CommandResult
from agent.frontend.cli_render import CliRenderer
from agent.frontend.cli_state import CliState


@pytest.mark.parametrize("action", ["chat", "image"])
@pytest.mark.parametrize("fails", [False, True])
def test_send_transaction(tmp_path, action, fails):
    path = tmp_path / "a.txt"
    path.write_text("data")
    state = CliState("openai", "model")
    state.add_files([str(path)])
    output = StringIO()
    renderer = CliRenderer(Console(file=output, force_terminal=False))
    chat = AsyncMock(return_value=("", "normal error reply", ""))
    image = AsyncMock(return_value="image.png")
    if fails:
        chat.side_effect = RuntimeError("offline")
        image.side_effect = RuntimeError("offline")
    agent = SimpleNamespace(chat=chat, total_tokens=0)
    with patch("agent.imagegen.generate_image", image):
        asyncio.run(send_turn(agent, state, renderer, "original", CommandResult(action, "prompt")))
    assert bool(state.pending_files) == fails
    assert state.last_failed_input == ("original" if fails else "")
    if action == "chat":
        chat.assert_awaited_once()
        image.assert_not_awaited()
    else:
        image.assert_awaited_once()
        chat.assert_not_awaited()
    if fails:
        assert "可能部分完成" in output.getvalue()


def test_failed_input_prefilled_without_auto_retry():
    reader = SimpleNamespace(read=AsyncMock(side_effect=["hello", "/quit"]))
    agent = SimpleNamespace(chat=AsyncMock(side_effect=RuntimeError("offline")))
    state = CliState("openai", "model")
    renderer = CliRenderer(Console(file=StringIO()))
    asyncio.run(conversation(agent, state, reader, renderer, []))
    assert reader.read.call_args_list[1].kwargs["default"] == "hello"
    agent.chat.assert_awaited_once()


@pytest.mark.parametrize("startup_fails", [False, True])
def test_frontend_cleans_up_clients_and_mcp(startup_fails):
    client = SimpleNamespace(close=AsyncMock())
    mcp = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), list_tools=Mock(return_value=[]))
    if startup_fails:
        mcp.start.side_effect = RuntimeError("startup")
    agent = SimpleNamespace(client=client)
    reader = SimpleNamespace(read=AsyncMock(side_effect=EOFError))
    with (
        patch("agent.auth.get_openai_client", return_value=client),
        patch("agent.auth.get_model", return_value="model"),
        patch("agent.auth.load_config", return_value={"provider": "openai"}),
        patch("agent.auth.fetch_available_models", return_value=["model"]),
        patch("agent.mcp_manager.MCPManager", return_value=mcp),
        patch("agent.core.Agent", return_value=agent),
        patch("agent.frontend.cli_input.CliInput", return_value=reader),
        patch("rich.console.Console", return_value=Console(file=StringIO())),
    ):
        asyncio.run(CliFrontend()._run_cli("system"))
    client.close.assert_awaited_once()
    mcp.stop.assert_awaited_once()

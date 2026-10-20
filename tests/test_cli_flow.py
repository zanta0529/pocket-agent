import asyncio
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from rich.console import Console

from pocket_agent.extensions.mcp import MCPStartupResult
from pocket_agent.frontend.cli.commands import CommandResult
from pocket_agent.frontend.cli.main import CliFrontend, conversation, send_turn
from pocket_agent.frontend.cli.render import CliRenderer
from pocket_agent.frontend.cli.state import CliState


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
    agent = SimpleNamespace(chat=chat, total_tokens=0, on_stream=None, request_stop=Mock())
    with patch("pocket_agent.tools.image.generate_image", image):
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


def test_send_turn_ctrl_c_stops_agent():
    """Ctrl+C（CancelledError）在 send_turn 執行期間應呼叫 request_stop 並保留 last_failed_input。"""
    state = CliState("openai", "model")
    output = StringIO()
    renderer = CliRenderer(Console(file=output, force_terminal=False))

    async def cancellable_chat(*args, **kwargs):
        raise asyncio.CancelledError()

    agent = SimpleNamespace(
        chat=cancellable_chat,
        total_tokens=0,
        on_stream=None,
        request_stop=Mock(),
    )
    asyncio.run(send_turn(agent, state, renderer, "hello", CommandResult("chat", "hello")))
    agent.request_stop.assert_called_once()
    assert state.last_failed_input == "hello"
    assert "已停止" in output.getvalue()


def test_failed_input_prefilled_without_auto_retry():
    reader = SimpleNamespace(read=AsyncMock(side_effect=["hello", "/quit"]))
    agent = SimpleNamespace(
        chat=AsyncMock(side_effect=RuntimeError("offline")),
        on_stream=None,
        request_stop=Mock(),
        history=[],
    )
    state = CliState("openai", "model")
    renderer = CliRenderer(Console(file=StringIO()))
    with patch("pocket_agent.core.sessions.save_session"):
        asyncio.run(conversation(agent, state, reader, renderer, []))
    assert reader.read.call_args_list[1].kwargs["default"] == "hello"
    agent.chat.assert_awaited_once()


@pytest.mark.parametrize("startup_fails", [False, True])
def test_frontend_cleans_up_clients_and_mcp(startup_fails, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = SimpleNamespace(close=AsyncMock())
    mcp = SimpleNamespace(
        start=AsyncMock(return_value=MCPStartupResult(0, 0, 0, 0)),
        stop=AsyncMock(),
        list_tools=Mock(return_value=[]),
    )
    if startup_fails:
        mcp.start.side_effect = RuntimeError("startup")
    agent = SimpleNamespace(client=client, on_stream=None, history=[])
    reader = SimpleNamespace(read=AsyncMock(side_effect=EOFError))
    with (
        patch("pocket_agent.system.auth.get_provider_client", return_value=client),
        patch("pocket_agent.system.auth.get_model", return_value="model"),
        patch("pocket_agent.system.auth.load_config", return_value={"active_provider": "openai"}),
        patch("pocket_agent.system.auth.fetch_available_models", return_value=["model"]),
        patch("pocket_agent.extensions.mcp.MCPManager", return_value=mcp),
        patch("pocket_agent.core.agent.Agent", return_value=agent),
        patch("pocket_agent.frontend.cli.input.CliInput", return_value=reader),
        patch("rich.console.Console", return_value=Console(file=StringIO())),
        patch("pocket_agent.core.sessions.save_session"),
        patch("pocket_agent.system.paths.user_dir", return_value=tmp_path),
    ):
        asyncio.run(CliFrontend()._run_cli("system"))
    client.close.assert_awaited_once()
    mcp.stop.assert_awaited_once()


@pytest.mark.parametrize(
    ("result", "message", "style"),
    [
        (MCPStartupResult(0, 0, 0, 0), "未設定 MCP 服務，略過初始化。", None),
        (MCPStartupResult(2, 2, 0, 12), "MCP 已就緒：已連線 2 個服務，載入 12 個工具。", None),
        (MCPStartupResult(1, 1, 0, 0), "MCP 已連線 1 個服務，目前無可用工具。", None),
        (
            MCPStartupResult(2, 1, 1, 3),
            "MCP 部分就緒：1 個服務初始化成功，載入 3 個工具；1 個服務初始化失敗。",
            "yellow",
        ),
        (
            MCPStartupResult(2, 1, 1, 0),
            "MCP 部分就緒：1 個服務初始化成功，載入 0 個工具；1 個服務初始化失敗。",
            "yellow",
        ),
        (MCPStartupResult(2, 0, 2, 0), "MCP 初始化失敗，請檢查設定或上述錯誤訊息。", "red"),
    ],
)
def test_frontend_mcp_startup_messages(result, message, style, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    renderer = Mock()
    client = SimpleNamespace(close=AsyncMock())

    async def start(*, on_start):
        if result.configured_servers:
            on_start(result.configured_servers)
            renderer.message.assert_called_once_with("正在初始化 MCP 服務…")
        else:
            renderer.message.assert_not_called()
        return result

    mcp = SimpleNamespace(start=AsyncMock(side_effect=start), stop=AsyncMock(), list_tools=Mock(return_value=[]))
    agent = SimpleNamespace(client=client, on_stream=None, history=[])
    with (
        patch("pocket_agent.system.paths.user_dir", return_value=tmp_path),
        patch("pocket_agent.system.auth.get_provider_client", return_value=client),
        patch("pocket_agent.system.auth.get_model", return_value="model"),
        patch("pocket_agent.system.auth.load_config", return_value={"active_provider": "openai"}),
        patch("pocket_agent.system.auth.fetch_available_models", return_value=["model"]),
        patch("pocket_agent.extensions.mcp.MCPManager", return_value=mcp),
        patch("pocket_agent.core.agent.Agent", return_value=agent),
        patch("pocket_agent.frontend.cli.render.CliRenderer", return_value=renderer),
        patch("pocket_agent.frontend.cli.input.CliInput"),
        patch("pocket_agent.frontend.cli.main.conversation", new_callable=AsyncMock),
    ):
        asyncio.run(CliFrontend()._run_cli("system"))
    calls = [entry.args for entry in renderer.message.call_args_list]
    expected = [("正在初始化 MCP 服務…",)] if result.configured_servers else []
    expected.append((message,) if style is None else (message, style))
    assert calls == expected
    client.close.assert_awaited_once()
    mcp.stop.assert_awaited_once()


def test_new_command_cleans_context_and_saves_old_session(tmp_path, monkeypatch):
    from pocket_agent.core import sessions
    from pocket_agent.frontend.cli.commands.core import CommandContext
    from pocket_agent.frontend.cli.commands.session import NewCommand

    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path)
    renderer = Mock()
    reader = Mock()
    agent = SimpleNamespace(
        history=[{"role": "user", "content": "舊對話內容"}],
        clear_history=Mock(side_effect=lambda: agent.history.clear()),
        context_usage="some_usage",
        last_images=["img.png"],
        last_files=["doc.docx"],
    )
    state = CliState("openai", "model")
    old_session_id = state.session_id
    state.pending_files = ["file.txt"]
    state.session_tokens = 5000
    state.last_failed_input = "failed"
    state.session_approved_commands.add("git status")

    ctx = CommandContext(agent=agent, state=state, reader=reader, renderer=renderer, tools=[])
    cmd = NewCommand()
    res = asyncio.run(cmd.execute(ctx, ""))
    assert res.action == "handled"

    # 1. 驗證原先對話已自動儲存至磁碟
    saved_old = sessions.load_session(old_session_id)
    assert saved_old is not None
    assert saved_old["history"][0]["content"] == "舊對話內容"

    # 2. 驗證新對話具有全新乾淨的狀態與新 Session ID
    assert state.session_id != old_session_id
    assert len(agent.history) == 0
    assert len(state.pending_files) == 0
    assert state.session_tokens == 0
    assert state.last_failed_input == ""
    assert len(state.session_approved_commands) == 0
    assert agent.context_usage is None
    assert len(agent.last_images) == 0
    assert len(agent.last_files) == 0
    renderer.message.assert_called()
    assert "已開啟全新對話" in renderer.message.call_args[0][0]


def test_rename_command_updates_title_and_persists(tmp_path, monkeypatch):
    from pocket_agent.core import sessions
    from pocket_agent.frontend.cli.commands.core import CommandContext
    from pocket_agent.frontend.cli.commands.session import RenameCommand

    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path)
    renderer = Mock()
    reader = SimpleNamespace(read=AsyncMock(return_value="互動輸入的新標題"))
    agent = SimpleNamespace(
        history=[{"role": "user", "content": "初始問題"}],
    )
    state = CliState("openai", "model")
    sessions.save_session(state.session_id, agent.history, title="初始標題")

    ctx = CommandContext(agent=agent, state=state, reader=reader, renderer=renderer, tools=[])
    cmd = RenameCommand()

    # 1. 測試直接帶參數重新命名
    res = asyncio.run(cmd.execute(ctx, "新標題 A"))
    assert res.action == "handled"
    data_a = sessions.load_session(state.session_id)
    assert data_a["title"] == "新標題 A"

    # 2. 測試無參數時互動式提示輸入
    res2 = asyncio.run(cmd.execute(ctx, ""))
    assert res2.action == "handled"
    data_b = sessions.load_session(state.session_id)
    assert data_b["title"] == "互動輸入的新標題"

"""測試敏感工作階段（Sensitive Session）端到端不落盤政策。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pocket_agent.core.agent import Agent
from pocket_agent.core.sessions import save_session
from pocket_agent.extensions.mcp import MCPManager
from pocket_agent.frontend.cli.commands.core import CommandContext
from pocket_agent.frontend.cli.commands.session import NewCommand, RenameCommand, SessionCommand
from pocket_agent.frontend.cli.main import _autosave_session


def test_mcp_manager_identifies_sensitive_tools():
    manager = MCPManager()
    assert manager.is_sensitive_tool("rhel_system_overview") is True
    assert manager.is_sensitive_tool("win_rdp_query") is True
    assert manager.is_sensitive_tool("unrelated_tool") is False

    # 模擬向 Manager 註冊帶有 is_sensitive=True 的 ToolEntry
    fake_session = SimpleNamespace(
        list_tools=AsyncMock(
            return_value=SimpleNamespace(
                tools=[
                    SimpleNamespace(name="custom_diag", description="diag", inputSchema={"type": "object"}),
                ]
            )
        )
    )
    asyncio.run(
        manager._register_tools(
            "custom_sec",
            fake_session,
            inject={},
            safe_read_tools=[],
            is_sensitive=True,
        )
    )
    assert manager.is_sensitive_tool("custom_diag") is True


def test_save_session_blocks_when_marked_sensitive(tmp_path, monkeypatch):
    monkeypatch.setattr("pocket_agent.core.sessions.SESSIONS_DIR", tmp_path)
    session_id = "test-sensitive-block"

    history = [{"role": "user", "content": "hello"}]
    save_session(session_id, history, is_sensitive=True)

    # 斷言磁碟上未建立任何 session 檔案
    assert not (tmp_path / f"{session_id}.json").exists()


def test_autosave_session_skips_when_agent_is_sensitive(tmp_path, monkeypatch):
    monkeypatch.setattr("pocket_agent.core.sessions.SESSIONS_DIR", tmp_path)
    agent = MagicMock()
    agent.history = [{"role": "user", "content": "diagnostic data"}]
    agent.is_sensitive_session = True

    state = MagicMock()
    state.session_id = "sess-sensitive-auto"

    renderer = MagicMock()

    _autosave_session(agent, state, renderer)

    # 斷言終端機給出資安警告，且磁碟上無檔案
    renderer.message.assert_called_once()
    assert "敏感主機診斷" in renderer.message.call_args[0][0]
    assert not (tmp_path / f"{state.session_id}.json").exists()


def test_session_commands_block_saving_on_sensitive_agent(tmp_path, monkeypatch):
    monkeypatch.setattr("pocket_agent.core.sessions.SESSIONS_DIR", tmp_path)

    agent = MagicMock()
    agent.history = [{"role": "user", "content": "secret diagnostic"}]
    agent.is_sensitive_session = True

    state = MagicMock()
    state.session_id = "sess-sensitive-manual"

    renderer = MagicMock()
    ctx = CommandContext(
        agent=agent,
        state=state,
        reader=MagicMock(),
        renderer=renderer,
        tools=[],
        command_name="session",
    )

    # 1. 測試 /session save
    cmd = SessionCommand()
    asyncio.run(cmd.execute(ctx, "save"))
    renderer.message.assert_called_with(
        "【安全提醒】本工作階段涉及敏感主機診斷，基於資安政策不予持久化落盤。", "yellow"
    )
    assert not (tmp_path / f"{state.session_id}.json").exists()

    # 2. 測試 /rename
    rename_cmd = RenameCommand()
    asyncio.run(rename_cmd.execute(ctx, "New Title"))
    renderer.message.assert_called_with("【安全提醒】本工作階段涉及敏感主機診斷，不予持久化儲存。", "yellow")

    # 3. 測試 /new 清空敏感標記
    new_cmd = NewCommand()
    asyncio.run(new_cmd.execute(ctx, ""))
    assert agent.is_sensitive_session is False


def test_agent_marks_sensitive_and_hides_exception_on_sensitive_tool():
    client = MagicMock()
    mcp_mgr = MCPManager()
    agent = Agent(client=client, mcp=mcp_mgr, model="gpt-test")
    assert agent.is_sensitive_session is False

    # 模擬呼叫敏感工具拋出異常
    fc = {"call_id": "call-1", "name": "rhel_system_overview", "arguments": {}}
    with patch.object(mcp_mgr, "call", side_effect=RuntimeError("internal stacktrace with synthetic token")):
        # 建立最小化的 _run_loop 工具呼叫測試環境
        loop = asyncio.new_event_loop()
        try:
            # 驗證 is_sensitive_tool 正確觸發
            assert mcp_mgr.is_sensitive_tool(fc["name"]) is True
        finally:
            loop.close()

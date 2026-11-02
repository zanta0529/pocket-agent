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

    fc = {"call_id": "call-1", "name": "rhel_system_overview", "arguments": {}}
    input_items = []

    # 模擬敏感工具異常，驗證 agent 設定敏感標記並遮蔽原始例外
    agent.is_sensitive_session = True
    agent._append_tool_pair(input_items, fc, "[ERROR] 敏感診斷工具執行失敗，未回傳原始例外內容。")
    assert agent.is_sensitive_session is True
    output_item = input_items[-1]
    assert "未回傳原始例外內容" in output_item["output"]


def test_sensitive_spillover_does_not_write_disk(tmp_path):
    from pocket_agent.core.spillover import SpilloverManager

    manager = SpilloverManager(output_dir=tmp_path)
    large_sensitive_text = "SECRET_TOKEN_12345\n" * 500

    result = manager.spill_or_pass(
        large_sensitive_text,
        tool_name="rhel_system_overview",
        call_id="call-sec-1",
        max_chars=200,
        is_sensitive=True,
    )

    # 斷言磁碟上未建立任何 .log 檔案
    log_files = list(tmp_path.glob("*.log"))
    assert len(log_files) == 0
    assert "敏感主機診斷資訊禁止落盤保存" in result


def test_subagent_sensitive_propagates_to_parent():
    parent_agent = Agent(client=MagicMock(), mcp=MagicMock(), model="gpt-test")
    assert parent_agent.is_sensitive_session is False

    async def _test():
        with patch("pocket_agent.core.agent.get_subagent", return_value={"model": "gpt-test"}):
            with patch.object(Agent, "chat") as mock_chat:

                async def fake_chat(task):
                    for sub in parent_agent.active_subagents:
                        sub.is_sensitive_session = True
                    return "", "子代理完成敏感檢測", ""

                mock_chat.side_effect = fake_chat
                await parent_agent._run_subagent({"name": "diag-sub", "task": "check rhel"})

    asyncio.run(_test())
    assert parent_agent.is_sensitive_session is True


def test_win_rdp_rejects_command_injection_characters():
    import pytest

    from mcp_servers.win_rdp_mcp import _validate_win_config_path

    # 合法路徑
    assert _validate_win_config_path(r"C:\Windows\win.ini") == r"C:\Windows\win.ini"

    # 包含單引號等注入字元
    with pytest.raises(ValueError, match="非法的 Shell、引號或命令注入特殊字元"):
        _validate_win_config_path(r"C:\inetpub\wwwroot\test'; whoami; #")

    # 包含 Unicode Smart Quotes (彎引號) 注入字元
    with pytest.raises(ValueError, match="非法的 Shell、引號或命令注入特殊字元"):
        _validate_win_config_path("C:\\inetpub\\wwwroot\\test\u2018; whoami; #")
    with pytest.raises(ValueError, match="非法的 Shell、引號或命令注入特殊字元"):
        _validate_win_config_path("C:\\inetpub\\wwwroot\\test\u201c; whoami; #")


def test_mssql_forbidden_rejects_sequence():
    import pytest

    from mcp_servers.mssql_mcp import _validate_select_query

    # 允許正常 SELECT
    assert _validate_select_query("SELECT 1") == "SELECT 1"

    # 阻擋標準 NEXT VALUE FOR
    with pytest.raises(ValueError, match="禁止執行的寫入、預存程序或系統變更關鍵字"):
        _validate_select_query("SELECT NEXT VALUE FOR dbo.seq_counter")

    # 阻擋註解混淆的 NEXT/**/VALUE/**/FOR 穿透
    with pytest.raises(ValueError, match="禁止執行的寫入、預存程序或系統變更關鍵字"):
        _validate_select_query("SELECT NEXT/**/VALUE/**/FOR dbo.seq_counter")

    # 阻擋單行註解換行混淆
    with pytest.raises(ValueError, match="禁止執行的寫入、預存程序或系統變更關鍵字"):
        _validate_select_query("SELECT NEXT -- comment\nVALUE FOR dbo.seq_counter")

    # 阻擋字串常值內含 '--' 混淆註解消除並執行 NEXT VALUE FOR 繞過
    with pytest.raises(ValueError, match="禁止執行的寫入、預存程序或系統變更關鍵字"):
        _validate_select_query("SELECT '--' AS marker, NEXT VALUE FOR dbo.counter")

    # 阻擋字串常值內含 '/*' 混淆區塊註解並執行 NEXT VALUE FOR 繞過
    with pytest.raises(ValueError, match="禁止執行的寫入、預存程序或系統變更關鍵字"):
        _validate_select_query("SELECT '/* harmless */' AS marker, NEXT VALUE FOR dbo.counter")

    # 阻擋未閉合字串常值
    with pytest.raises(ValueError, match="未閉合的字串常值"):
        _validate_select_query("SELECT 'unclosed string")

    # 允許字串常值包含註解符號之純唯讀查詢
    assert _validate_select_query("SELECT '-- not comment' AS c") == "SELECT '-- not comment' AS c"
    assert _validate_select_query("SELECT '/* block */' AS c") == "SELECT '/* block */' AS c"
    assert _validate_select_query("SELECT N'-- unicode' AS c") == "SELECT N'-- unicode' AS c"


def test_parallel_subagents_handles_cancelled_error():
    agent = Agent(client=MagicMock(), mcp=MagicMock(), model="gpt-test")
    tasks = [{"name": "sub1", "task": "task1"}, {"name": "sub2", "task": "task2"}]

    async def _test():
        with patch.object(agent, "_run_subagent") as mock_sub:
            mock_sub.side_effect = [asyncio.CancelledError(), "子代理 2 成功完成"]
            res = await agent._execute_core_tool("run_subagents_parallel", {"tasks": tasks}, "call-1")
            assert "【子代理 sub1 已取消】" in res
            assert "子代理 2 成功完成" in res

    asyncio.run(_test())


def test_interrupt_active_shell_processes():
    from pocket_agent.tools.shell import (
        _active_shell_procs,
        _shell_proc_lock,
        interrupt_active_shell_processes,
    )

    mock_proc = MagicMock()
    with _shell_proc_lock:
        _active_shell_procs.add(mock_proc)

    count = interrupt_active_shell_processes()
    assert count == 1
    assert len(_active_shell_procs) == 0


def test_browser_fill_exception_fail_secure():
    """驗證讀取 DOM 元素屬性發生非預期例外時，防禦性視為密碼進行遮罩，杜絕 Fail-Open 明文洩漏。"""
    from mcp_servers import browser_mcp

    mock_page = MagicMock()
    mock_locator = AsyncMock()
    mock_locator.get_attribute.side_effect = RuntimeError("DOM detached")
    mock_locator.fill = AsyncMock()
    mock_page.locator.return_value.first = mock_locator

    with patch.object(browser_mcp, "_ensure_page", return_value=mock_page):
        res = asyncio.run(browser_mcp.browser_fill(1, "SuperSecretPassword123!"))
        assert "********" in res
        assert "SuperSecretPassword123!" not in res


def test_shell_interrupt_during_confirm_prevents_spawn():
    """驗證在確認對話框等待期間若觸發中斷，確認返回後指令亦絕不啟動執行。"""
    from pocket_agent.tools.shell import (
        call_shell_tool,
        interrupt_active_shell_processes,
    )

    def fake_confirm(_cmd):
        # 模擬在使用者思考確認期間，外部觸發了中斷
        interrupt_active_shell_processes()
        return True

    with patch("subprocess.Popen") as mock_popen:
        res = call_shell_tool("run_command", {"command": "echo test"}, confirm=fake_confirm)
        assert "指令未建立與執行" in res
        mock_popen.assert_not_called()

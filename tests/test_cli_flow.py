import asyncio
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

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
    state.accept_edits = True
    agent.accept_edits = True

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
    assert state.accept_edits is False
    assert agent.accept_edits is False
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


def test_cli_confirm_with_custom_instruction(tmp_path, monkeypatch):
    """測試在 CLI 確認流程中輸入 c 自訂指示，或直接輸入口語指示時，會記錄至 agent 並回傳指示"""
    monkeypatch.chdir(tmp_path)
    client = SimpleNamespace(close=AsyncMock())
    mcp = SimpleNamespace(
        start=AsyncMock(return_value=MCPStartupResult(0, 0, 0, 0)),
        stop=AsyncMock(),
        list_tools=Mock(return_value=[]),
    )
    agent = SimpleNamespace(client=client, on_stream=None, history=[], user_instructions=[])
    reader = SimpleNamespace(read=AsyncMock(side_effect=EOFError))

    with (
        patch("pocket_agent.system.paths.user_dir", return_value=tmp_path),
        patch("pocket_agent.system.auth.get_provider_client", return_value=client),
        patch("pocket_agent.system.auth.get_model", return_value="model"),
        patch("pocket_agent.system.auth.load_config", return_value={"active_provider": "openai"}),
        patch("pocket_agent.system.auth.fetch_available_models", return_value=["model"]),
        patch("pocket_agent.extensions.mcp.MCPManager", return_value=mcp),
        patch("pocket_agent.core.agent.Agent", return_value=agent),
        patch("pocket_agent.frontend.cli.input.CliInput", return_value=reader),
        patch("rich.console.Console", return_value=Console(file=StringIO())),
        patch("pocket_agent.core.sessions.save_session"),
        patch("pocket_agent.frontend.cli.main.conversation", new_callable=AsyncMock),
    ):
        asyncio.run(CliFrontend()._run_cli("system"))

    confirm_func = agent.on_confirm
    assert confirm_func is not None

    # 1. 測試輸入 c 並提供自訂指示
    custom_text = "請不要自己執行測試指令，改由手動執行"
    with patch("builtins.input", side_effect=["c", custom_text]):
        res = confirm_func("pytest tests/")
        assert res == custom_text
        assert custom_text in agent.user_instructions

    # 2. 測試智慧相容：直接輸入口語指示
    direct_text = "請先不要動資料庫，先列出 SQL 即可"
    with patch("builtins.input", side_effect=[direct_text]):
        res2 = confirm_func("drop table users;")
        assert res2 == direct_text
        assert direct_text in agent.user_instructions

    # 3. 測試在檔案編輯確認提示當下按下 Shift+Tab：直接啟用 Accept-edits 並放行當下與後續檔案
    def fake_pt_prompt(prompt_msg, key_bindings=None, **kwargs):
        from prompt_toolkit.keys import Keys

        assert "Shift+Tab: 自動編輯(Accept-edits)" in prompt_msg
        assert "bottom_toolbar" in kwargs
        handler = key_bindings.get_bindings_for_keys((Keys.BackTab,))[-1].handler
        result_holder = {}
        event = SimpleNamespace(app=SimpleNamespace(exit=lambda result=None: result_holder.setdefault("res", result)))
        handler(event)
        return result_holder["res"]

    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("prompt_toolkit.prompt", side_effect=fake_pt_prompt) as mock_pt,
    ):
        assert confirm_func("局部修改檔案：A.py") is True
        assert getattr(agent, "accept_edits", False) is True
        assert mock_pt.call_count == 1
        # 後續檔案 B 不再彈出詢問，直接放行
        assert confirm_func("建立新檔：B.py") is True
        assert mock_pt.call_count == 1


def test_agent_dynamic_instructions_with_user_instructions():
    """測試 Agent._instructions 會即時注入使用者指示"""
    from pocket_agent.core.agent import Agent

    client = Mock()
    mcp = Mock()
    mcp.openai_tools.return_value = []
    agent = Agent(client=client, mcp=mcp, model="test-model")
    agent.user_instructions.append("請不要自行執行測試，最後列出指令即可")

    instructions = agent._instructions()
    assert "[使用者即時指示，請務必嚴格遵守]" in instructions
    assert "- 請不要自行執行測試，最後列出指令即可" in instructions

    # 驗證 clear_history 會清空指示
    agent.clear_history()
    assert len(agent.user_instructions) == 0


def test_agent_accept_edits_auto_approves_fs_writes_only(tmp_path, monkeypatch):
    """測試開啟 accept_edits 時，跨檔建立與修改自動核准不觸發 on_confirm，但 run_command 仍需 on_confirm。"""
    import json

    from pocket_agent.core.agent import Agent
    from pocket_agent.engines import EventText, EventToolCallDone, EventToolCallStart

    monkeypatch.chdir(tmp_path)
    client = Mock()
    mcp = Mock()
    mcp.openai_tools.return_value = []
    agent = Agent(client=client, mcp=mcp, model="test-model")
    confirm_mock = Mock(return_value=False)
    agent.on_confirm = confirm_mock
    agent.accept_edits = True

    assert "Accept-edits mode" in agent._instructions()

    rounds = [
        [
            EventToolCallStart(call_id="c1", name="write_to_file"),
            EventToolCallDone(
                call_id="c1",
                name="write_to_file",
                full_args=json.dumps({"path": "file_a.py", "content": "x = 1\n"}),
            ),
            EventToolCallStart(call_id="c2", name="write_to_file"),
            EventToolCallDone(
                call_id="c2",
                name="write_to_file",
                full_args=json.dumps({"path": "file_b.py", "content": "y = 2\n"}),
            ),
            EventToolCallStart(call_id="c3", name="replace_file_content"),
            EventToolCallDone(
                call_id="c3",
                name="replace_file_content",
                full_args=json.dumps({"path": "file_a.py", "target": "x = 1", "replacement": "x = 10"}),
            ),
            EventToolCallStart(call_id="c4", name="run_command"),
            EventToolCallDone(
                call_id="c4",
                name="run_command",
                full_args=json.dumps({"command": "echo hi"}),
            ),
        ],
        [EventText(delta="done")],
    ]

    async def fake_stream_chat(**kwargs):
        for ev in rounds.pop(0):
            yield ev

    with (
        patch.object(agent.engine, "stream_chat", side_effect=fake_stream_chat),
        patch("pocket_agent.system.paths.user_dir", return_value=tmp_path / ".pa"),
    ):
        _, reply = asyncio.run(agent._run_loop())

    assert reply == "done"
    assert (tmp_path / "file_a.py").read_text(encoding="utf-8") == "x = 10\n"
    assert (tmp_path / "file_b.py").read_text(encoding="utf-8") == "y = 2\n"
    # 檔案操作均自動放行，僅 run_command 觸發 on_confirm 一次
    confirm_mock.assert_called_once_with("echo hi")


def test_activity_command_completer_and_dispatch():
    """測試 AC-1：/activity 與別名 /changes 註冊、Tab 補全顯示及參數檢查。"""
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from pocket_agent.core.activity import SessionActivityTracker
    from pocket_agent.frontend.cli.commands import CommandHandler
    from pocket_agent.frontend.cli.input import BUILTINS, SlashCommandCompleter

    assert "activity" in BUILTINS
    state = CliState("openai", "model")
    completer = SlashCommandCompleter(state)

    with (
        patch("pocket_agent.system.commands.list_commands", return_value=[]),
        patch("pocket_agent.extensions.skills.list_skills", return_value=[]),
        patch("pocket_agent.core.subagents.list_subagents", return_value=[]),
    ):
        act_Matches = list(completer.get_completions(Document("/act"), CompleteEvent()))
        ch_matches = list(completer.get_completions(Document("/ch"), CompleteEvent()))

    assert any(c.text == "activity" and c.display_text == "activity (changes)" for c in act_Matches)
    assert any(c.text == "activity" and c.display_text == "activity (changes)" for c in ch_matches)

    output = StringIO()
    renderer = CliRenderer(Console(file=output, force_terminal=False, width=80))
    agent = SimpleNamespace(history=[], activity=SessionActivityTracker(), workspace_dirs=[])
    handler = CommandHandler(agent, state, Mock(), renderer, [])

    res1 = asyncio.run(handler.dispatch("/activity"))
    assert res1.action == "handled"
    res2 = asyncio.run(handler.dispatch("/changes"))
    assert res2.action == "handled"

    with pytest.raises(ValueError, match="不接受參數"):
        asyncio.run(handler.dispatch("/activity extra"))


def test_activity_command_files_and_urls_separated_with_status(tmp_path, monkeypatch):
    """測試 AC-2、AC-3、AC-4：檔案（新增／修改／刪除）與網址（完整網址＋成功／失敗＋次數）分區顯示。"""
    from pocket_agent.core.activity import SessionActivityTracker
    from pocket_agent.frontend.cli.commands import CommandHandler

    monkeypatch.chdir(tmp_path)
    existing_file = tmp_path / "existing.py"
    existing_file.write_text("old = 1\n", encoding="utf-8")
    to_delete_file = tmp_path / "obsolete.py"
    to_delete_file.write_text("remove_me = True\n", encoding="utf-8")

    tracker = SessionActivityTracker()

    # 1. 新建檔案（後續再局部修改仍應維持為「新增」）
    new_file = tmp_path / "created.py"
    existed_new = tracker.inspect_file_before_tool(new_file)
    new_file.write_text("a = 1\n", encoding="utf-8")
    tracker.record_tool_result(
        "write_to_file",
        {"path": str(new_file)},
        f"[SUCCESS] 成功寫入檔案 {new_file}",
        existed_before=existed_new,
    )
    tracker.record_tool_result(
        "replace_file_content",
        {"path": str(new_file)},
        f"[SUCCESS] 成功替換檔案 {new_file} 的局部內容",
        existed_before=True,
    )

    # 2. 修改既有檔案
    existed_mod = tracker.inspect_file_before_tool(existing_file)
    existing_file.write_text("old = 2\n", encoding="utf-8")
    tracker.record_tool_result(
        "replace_file_content",
        {"path": str(existing_file)},
        f"[SUCCESS] 成功替換檔案 {existing_file} 的局部內容",
        existed_before=existed_mod,
    )

    # 3. 使用者拒絕寫入的檔案不得列入
    rejected_file = tmp_path / "rejected.py"
    tracker.record_tool_result(
        "write_to_file",
        {"path": str(rejected_file)},
        "[使用者拒絕執行此檔案寫入操作]",
        existed_before=False,
    )

    # 4. 透過工作區快照捕捉刪除檔案
    snap_before = tracker.take_workspace_snapshot()
    to_delete_file.unlink()
    tracker.apply_workspace_diff(snap_before)

    # 5. 記錄網路查詢（含極長完整網址、404 失敗、以及方案 A 重複查詢更新狀態與次數）
    long_url = (
        "https://docs.python.org/3.14/whatsnew/3.14.html"
        "?section=free-threaded-cpython&detail=very-long-query-parameter-that-must-not-be-truncated"
    )
    fail_url = "https://example.com/missing/documentation/page/404"
    retry_url = "https://api.example.org/v1/status?env=prod"

    tracker.record_tool_result(
        "read_url_content",
        {"url": long_url},
        "<external_content>\nPython 3.14 notes\n</external_content>",
    )
    tracker.record_tool_result(
        "read_url_content",
        {"url": fail_url},
        "[ERROR] 網路操作失敗: HTTP 404",
    )
    # retry_url 第一次失敗、第二次成功 -> 方案 A 應保留單筆、狀態更新為 [成功] 且顯示 (2 次)
    tracker.record_tool_result("read_url_content", {"url": retry_url}, "[ERROR] 網路操作失敗: timeout")
    tracker.record_tool_result(
        "read_url_content",
        {"url": retry_url},
        "<external_content>\nOK\n</external_content>",
    )

    output = StringIO()
    # 故意設定較窄的 console width=60，驗證完整網址仍不會被截斷
    renderer = CliRenderer(Console(file=output, force_terminal=False, width=60))
    state = CliState("openai", "model")
    agent = SimpleNamespace(history=[], activity=tracker, workspace_dirs=[])
    handler = CommandHandler(agent, state, Mock(), renderer, [])

    asyncio.run(handler.dispatch("/changes"))
    rendered = output.getvalue()

    file_sec_idx = rendered.index("📁 本次會話檔案異動清單")
    url_sec_idx = rendered.index("🌐 本次會話上網查詢網址清單")
    assert file_sec_idx < url_sec_idx

    file_section = rendered[file_sec_idx:url_sec_idx]
    url_section = rendered[url_sec_idx:]

    # 檔案區塊驗證：區分新增、修改、刪除，且不包含拒絕項目與網址
    assert "[新增] (1)" in file_section
    assert "+ created.py" in file_section
    assert "[修改] (1)" in file_section
    assert "~ existing.py" in file_section
    assert "[刪除] (1)" in file_section
    assert "- obsolete.py" in file_section
    assert "rejected.py" not in rendered
    assert "https://" not in file_section

    # 網址區塊驗證：完整網址不截斷、附帶成功／失敗狀態與重複次數
    assert f"1. [成功] {long_url}" in url_section
    assert f"2. [失敗] {fail_url}" in url_section
    assert f"3. [成功] {retry_url} (2 次)" in url_section
    assert "created.py" not in url_section


def test_activity_command_lifecycle_clear_new_and_session_load(tmp_path, monkeypatch):
    """測試 AC-5：/clear、/new 清空活動紀錄，以及 /session load 從歷史對話重建紀錄。"""
    import json

    from pocket_agent.core import sessions
    from pocket_agent.core.agent import Agent
    from pocket_agent.frontend.cli.commands import CommandHandler

    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path)
    client = Mock()
    mcp = Mock()
    mcp.openai_tools.return_value = []
    agent = Agent(client=client, mcp=mcp, model="test-model")

    agent.activity.record_file_change(tmp_path / "temp.py", "added", existed_before=False)
    agent.activity.record_url("https://example.com/a", success=True)
    assert len(agent.activity.get_url_activities()) == 1

    output = StringIO()
    renderer = CliRenderer(Console(file=output, force_terminal=False, width=100))
    state = CliState("openai", "model")
    handler = CommandHandler(agent, state, Mock(), renderer, [])

    # 1. /clear 應清空 activity
    asyncio.run(handler.dispatch("/clear"))
    assert sum(len(v) for v in agent.activity.get_file_changes().values()) == 0
    assert len(agent.activity.get_url_activities()) == 0

    # 2. 儲存一份含工具呼叫的歷史對話，再透過 /session load 載入並重建 activity
    saved_id = "20261109-120000-abcdef"
    saved_history = [
        {"role": "user", "content": "請查資料並寫檔"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "read_url_content",
            "arguments": json.dumps({"url": "https://python.org/doc"}),
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "<external_content>\nDoc\n</external_content>",
        },
        {
            "type": "function_call",
            "call_id": "call_2",
            "name": "write_to_file",
            "arguments": json.dumps({"path": "notes.md", "content": "hello", "overwrite": False}),
        },
        {
            "type": "function_call_output",
            "call_id": "call_2",
            "output": "[SUCCESS] 成功寫入檔案 notes.md",
        },
    ]
    sessions.save_session(saved_id, saved_history, title="測試載入重建")

    asyncio.run(handler.dispatch(f"/session load {saved_id}"))
    changes = agent.activity.get_file_changes(sync_disk=False)
    urls = agent.activity.get_url_activities()
    assert "notes.md" in changes["added"]
    assert len(urls) == 1
    assert urls[0].url == "https://python.org/doc"
    assert urls[0].status == "success"


def test_mcp_streamable_http_and_oauth_keyring_storage(tmp_path, monkeypatch):
    """測試 MCPManager 支援 Streamable HTTP 傳輸協定、自訂 Headers、OAuth Provider 及 Keyring 儲存。"""
    from contextlib import asynccontextmanager

    from pocket_agent.extensions.mcp import KeyringTokenStorage, MCPManager, OAuthClientInformationFull, OAuthToken

    cfg_file = tmp_path / "mcp_config.yaml"
    cfg_file.write_text(
        "servers:\n"
        "  agensi:\n"
        "    transport: Streamable HTTP\n"
        "    url: https://mcp.agensi.io/mcp/auth\n"
        "  custom_http:\n"
        "    transport: streamable-http\n"
        "    url: https://example.com/mcp\n"
        "    headers:\n"
        "      Authorization: 'Bearer ${MY_TOKEN}'\n"
        "    env:\n"
        "      MY_TOKEN: 'secret-token-123'\n",
        encoding="utf-8",
    )

    created_clients: list[dict] = []
    entered_urls: list[str] = []

    class DummyHttpClient:
        def __init__(self, headers=None, auth=None):
            self.headers = headers
            self.auth = auth
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self.closed = True

    def fake_create_http_client(headers=None, timeout=None, auth=None):
        client = DummyHttpClient(headers=headers, auth=auth)
        created_clients.append({"headers": headers, "auth": auth, "client": client})
        return client

    @asynccontextmanager
    async def fake_streamable_http_client(url, *, http_client=None, **kwargs):
        entered_urls.append(url)
        idx = len(entered_urls)
        yield (f"read_stream_{idx}", f"write_stream_{idx}")

    class DummySession:
        def __init__(self, read, write):
            self.read = read
            self.write = write

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def initialize(self):
            return None

        async def list_tools(self):
            idx = self.read.removeprefix("read_stream_")
            tool = SimpleNamespace(
                name=f"tool_{idx}",
                description="test tool",
                inputSchema={"type": "object", "properties": {}},
            )
            return SimpleNamespace(tools=[tool])

    with (
        patch("pocket_agent.extensions.mcp.create_mcp_http_client", side_effect=fake_create_http_client),
        patch("pocket_agent.extensions.mcp.streamable_http_client", side_effect=fake_streamable_http_client),
        patch("pocket_agent.extensions.mcp.ClientSession", side_effect=DummySession),
    ):
        mgr = MCPManager(config_path=cfg_file)
        res = asyncio.run(mgr.start())
        assert res.configured_servers == 2
        assert res.ready_servers == 2
        assert res.failed_servers == 0
        assert res.tool_count == 2
        assert entered_urls == ["https://mcp.agensi.io/mcp/auth", "https://example.com/mcp"]
        # agensi 未提供靜態 Authorization header，應自動掛載 OAuthClientProvider
        assert created_clients[0]["headers"] is None
        assert created_clients[0]["auth"] is not None
        # custom_http 已提供 Authorization header，應解析環境變數並略過 OAuth
        assert created_clients[1]["headers"] == {"Authorization": "Bearer secret-token-123"}
        assert created_clients[1]["auth"] is None

        asyncio.run(mgr.stop())
        assert all(item["client"].closed for item in created_clients)

    # 驗證 KeyringTokenStorage 讀寫 OAuthToken 與 OAuthClientInformationFull
    store: dict[str, dict] = {}
    with (
        patch("pocket_agent.system.auth._save_token", side_effect=lambda k, v: store.__setitem__(k, dict(v))),
        patch("pocket_agent.system.auth._load_token", side_effect=lambda k: store.get(k)),
    ):
        storage = KeyringTokenStorage("agensi")
        tok = OAuthToken(access_token="acc-1", token_type="Bearer", expires_in=3600, refresh_token="ref-1")
        asyncio.run(storage.set_tokens(tok))
        loaded_tok = asyncio.run(storage.get_tokens())
        assert loaded_tok is not None
        assert loaded_tok.access_token == "acc-1"
        assert loaded_tok.refresh_token == "ref-1"

        info = OAuthClientInformationFull(client_id="cid-123", client_secret="csec-456")
        asyncio.run(storage.set_client_info(info))
        loaded_info = asyncio.run(storage.get_client_info())
        assert loaded_info is not None
        assert loaded_info.client_id == "cid-123"
        assert loaded_info.client_secret == "csec-456"


def test_builtin_search_skills_tool():
    """驗證內建 search_skills 工具之註冊、判定與格式化回傳。"""
    from pocket_agent.extensions.skills import SEARCH_SKILLS_TOOL, call_skill_tool, is_skill_tool

    assert is_skill_tool("search_skills") is True
    assert is_skill_tool("use_skill") is True
    assert is_skill_tool("other_tool") is False
    assert SEARCH_SKILLS_TOOL["_is_safe_read"] is True

    # 查無結果情境
    with patch("pocket_agent.extensions.skills.search_skills", return_value=[]):
        with patch("pocket_agent.extensions.skills.list_skills", return_value=[{"name": "weekly-report"}]):
            res_empty = call_skill_tool("search_skills", {"query": "nonexistent"})
            assert "找不到與「nonexistent」相關的技能" in res_empty
            assert "weekly-report" in res_empty

    # 搜尋成功情境
    fake_hits = [{"name": "pbv", "description": "plan build verify workflow"}]
    with patch("pocket_agent.extensions.skills.search_skills", return_value=fake_hits):
        res_hit = call_skill_tool("search_skills", {"query": "plan", "top_k": 3})
        assert "pbv" in res_hit
        assert "plan build verify workflow" in res_hit
        assert "use_skill" in res_hit


def test_mcp_claude_code_namespacing_and_dispatch(tmp_path):
    """驗證 MCPManager 對齊 Claude Code 雙底線命名空間（mcp__<server>__<tool>）與雙向轉發。"""
    from pocket_agent.extensions.mcp import MCPManager

    fake_tool = SimpleNamespace(
        name="search_skills",
        description="Search Agensi remote skills registry",
        inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    fake_session = SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=[fake_tool])),
        call_tool=AsyncMock(return_value=MagicMock(content=[MagicMock(text='{"skills": ["remote-skill"]} blend')])),
    )

    manager = MCPManager(config_path=tmp_path / "mcp_config.yaml")
    asyncio.run(
        manager._register_tools(
            "agensi",
            fake_session,
            inject={},
            safe_read_tools=["search_skills"],
        )
    )

    # 1. 驗證對外工具 schema 冠上 mcp__agensi__ 前綴且 safe_read 正確判定
    tools = manager.openai_tools()
    assert len(tools) == 1
    assert tools[0]["name"] == "mcp__agensi__search_skills"
    assert tools[0]["_is_safe_read"] is True

    # 2. 驗證透過完整前綴呼叫時，轉發給 session 執行的是原始名稱 "search_skills"
    out1 = asyncio.run(manager.call("mcp__agensi__search_skills", {"query": "python"}))
    assert "remote-skill" in out1
    fake_session.call_tool.assert_awaited_with("search_skills", {"query": "python"})

    # 3. 驗證向下相容：透過原始名稱呼叫亦能正確轉發
    out2 = asyncio.run(manager.call("search_skills", {"query": "asyncio"}))
    assert "remote-skill" in out2
    fake_session.call_tool.assert_awaited_with("search_skills", {"query": "asyncio"})


def test_cli_renderer_mcp_tag_formatting():
    """驗證 CliRenderer 針對 mcp__ 前綴工具自動拆分 [server] 標籤與工具名稱。"""
    from pocket_agent.frontend.cli.render import CliRenderer

    renderer = CliRenderer(Console(file=StringIO()))
    info = {
        "action": "Read",
        "action_color": "bold yellow",
        "name_color": "bold cyan",
        "real_name": "search_skills",
        "target": "python asyncio",
        "server_tag": "agensi",
    }
    lines = renderer._format_tool_lines(("● ", "bold green"), info)
    plain_text = lines[0].plain
    assert "Read" in plain_text
    assert "[agensi]" in plain_text
    assert "(search_skills)" in plain_text


def test_run_operation_drain_timeout_and_force_abort():
    """驗證 run_operation 在背景任務阻塞時能在收尾逾時或二次中斷下安全結束。"""
    from pocket_agent.frontend.cli.interrupts import OperationInterrupted, run_operation

    async def stuck_operation():
        await asyncio.sleep(10.0)

    stop_called = False

    def on_stop():
        nonlocal stop_called
        stop_called = True

    async def _test():
        task = asyncio.create_task(run_operation(stuck_operation(), on_stop))
        await asyncio.sleep(0.05)
        # 模擬第一次 Ctrl+C 觸發中斷
        import signal

        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)
        await asyncio.sleep(0.05)
        # 模擬第二次 Ctrl+C 觸發強制中止
        handler(signal.SIGINT, None)

        with pytest.raises(OperationInterrupted):
            await task

    asyncio.run(_test())
    assert stop_called is True


def test_setup_win_sigint_workaround_retains_strong_ref(monkeypatch):
    """驗證 Windows 下 setup_win_sigint_workaround 能將 Task 存入強引用集合避免 GC。"""
    import sys

    from pocket_agent.system import compat

    monkeypatch.setattr(sys, "platform", "win32")

    async def _test():
        task = compat.setup_win_sigint_workaround()
        assert task is not None
        assert task in compat._GLOBAL_BACKGROUND_TASKS
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task not in compat._GLOBAL_BACKGROUND_TASKS

    asyncio.run(_test())


def test_agent_sleep_with_cancel_quick_response():
    """驗證 Agent._sleep_with_cancel 在 cancel_requested 為 True 時立即返回，且支援 Task 取消。"""
    from types import SimpleNamespace

    from pocket_agent.core.agent import Agent

    agent_inst = SimpleNamespace(cancel_requested=True)
    sleep_func = Agent._sleep_with_cancel.__get__(agent_inst)

    async def _test():
        # 當已要求取消時，直接返回不 sleep
        await sleep_func(10.0)

        # 支援 asyncio.CancelledError 取消
        agent_normal = SimpleNamespace(cancel_requested=False)
        sleep_normal = Agent._sleep_with_cancel.__get__(agent_normal)
        task = asyncio.create_task(sleep_normal(10.0))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_test())

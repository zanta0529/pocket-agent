import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from pocket_agent.frontend.cli.commands import CommandHandler, switch_model
from pocket_agent.frontend.cli.state import CliState


def test_model_switch_commits_state_and_closes_old_client():
    old, new = SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=AsyncMock())
    agent = SimpleNamespace(client=old, model="old", history=["preserve"])
    state = CliState("openai", "old", ["pending"])
    with patch("pocket_agent.system.auth.get_provider_client", return_value=new):
        asyncio.run(switch_model(agent, state, "ollama", "new", Mock()))
    assert agent.client is new
    assert (state.provider, state.model, agent.model) == ("ollama", "new", "new")
    assert state.pending_files == ["pending"]
    assert agent.history == ["preserve"]
    old.close.assert_awaited_once()


def test_failed_switch_preserves_state():
    old = SimpleNamespace(close=AsyncMock())
    agent = SimpleNamespace(client=old, model="old")
    state = CliState("openai", "old")
    with patch("pocket_agent.system.auth.get_provider_client", side_effect=RuntimeError("offline")):
        with pytest.raises(RuntimeError):
            asyncio.run(switch_model(agent, state, "ollama", "new", Mock()))
    assert agent.client is old
    assert (state.provider, state.model) == ("openai", "old")
    old.close.assert_not_awaited()


@pytest.mark.parametrize("raw", ["/modelxxx", "/quit extra", "/image", "/attachments remove 0"])
def test_invalid_commands_preserve_attachments(raw):
    state = CliState("openai", "model", ["pending"])
    agent = Mock()
    agent.workspace_dirs = []
    handler = CommandHandler(agent, state, Mock(), Mock(), [])
    with patch("pocket_agent.system.commands.get_command", return_value=None):
        with pytest.raises(ValueError):
            asyncio.run(handler.dispatch(raw))
    assert state.pending_files == ["pending"]


def test_clear_and_add_dir_do_not_consume_attachments():
    agent, renderer = Mock(), Mock()
    agent.workspace_dirs = []
    agent.check_new_workspace_dir.return_value = ("in_default", "加入成功，但不會讀取目錄", "dir")
    state = CliState("openai", "model", ["pending"])
    handler = CommandHandler(agent, state, Mock(), renderer, [])
    asyncio.run(handler.dispatch("/clear"))
    asyncio.run(handler.dispatch("/add-dir any-directory"))
    agent.clear_history.assert_called_once()
    assert state.pending_files == ["pending"]
    assert "不會讀取目錄" in renderer.message.call_args.args[0]


def test_model_selection_unavailable_allows_manual_entry():
    state = CliState("openai", "old")
    from pocket_agent.system.providers import PROVIDER_STRATEGIES

    ollama_display = PROVIDER_STRATEGIES["ollama"].display_name
    reader = SimpleNamespace(choose=AsyncMock(side_effect=[ollama_display, "manual"]))
    handler = CommandHandler(Mock(), state, reader, Mock(), [])
    with (
        patch("pocket_agent.system.auth.fetch_available_models", return_value=[]),
        patch("pocket_agent.frontend.cli.commands.system.switch_model", new_callable=AsyncMock) as switch,
    ):
        asyncio.run(handler.dispatch("/model"))
        switch.assert_awaited_once()
        assert switch.call_args.args[2:4] == ("ollama", "manual")
    assert reader.choose.call_args.args[1] == []


def test_model_selection_cancel_keeps_state():
    state = CliState("openai", "old")
    reader = SimpleNamespace(choose=AsyncMock(return_value=None))
    handler = CommandHandler(Mock(), state, reader, Mock(), [])
    with patch("pocket_agent.system.auth.get_provider_client") as create:
        asyncio.run(handler.dispatch("/model"))
        create.assert_not_called()
    assert state.model == "old"


def test_old_client_cleanup_failure_does_not_rollback():
    old = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("close")))
    new = SimpleNamespace(close=AsyncMock())
    agent = SimpleNamespace(client=old, model="old")
    state = CliState("openai", "old")
    with patch("pocket_agent.system.auth.get_provider_client", return_value=new):
        asyncio.run(switch_model(agent, state, "ollama", "new", Mock()))
    assert agent.client is new and state.model == "new"


def test_custom_expansion_failure_retains_queue():
    state = CliState("openai", "model", ["pending"])
    handler = CommandHandler(Mock(), state, Mock(), Mock(), [])
    with (
        patch("pocket_agent.system.commands.get_command", return_value={"name": "custom"}),
        patch("pocket_agent.system.commands.expand_command", return_value=None),
    ):
        with pytest.raises(ValueError):
            asyncio.run(handler.dispatch("/custom"))
    assert state.pending_files == ["pending"]


# ── F5: /session 指令 ─────────────────────────────────


def _make_handler(agent=None, state=None, renderer=None):
    agent = agent or Mock()
    state = state or CliState("openai", "model")
    renderer = renderer or Mock()
    return CommandHandler(agent, state, Mock(), renderer, [])


def test_session_list_empty():
    """空白時顯示「尚無歷史對話」。"""
    renderer = Mock()
    handler = _make_handler(renderer=renderer)
    with patch("pocket_agent.core.sessions.list_sessions", return_value=[]):
        asyncio.run(handler.dispatch("/session list"))
    renderer.message.assert_called_once()
    assert "尚無歷史對話" in renderer.message.call_args.args[0]


def test_session_list_shows_recent():
    """list_sessions 回傳 2 筆時，應印出 2 行摘要。"""
    import time

    renderer = Mock()
    handler = _make_handler(renderer=renderer)
    sessions = [
        {"id": "20260909-120000-aabbcc", "title": "第一輪對話", "updated_at": time.time()},
        {"id": "20260909-130000-ddeeff", "title": "第二輪對話", "updated_at": time.time()},
    ]
    with patch("pocket_agent.core.sessions.list_sessions", return_value=sessions):
        asyncio.run(handler.dispatch("/session list"))
    assert renderer.message.call_count == 2
    first_call = renderer.message.call_args_list[0].args[0]
    assert "20260909-120000-aabbcc" in first_call
    assert "第一輪對話" in first_call


def test_session_load_ok():
    """/session load <id> 成功時更新 agent.history 與 state.session_id。"""
    agent = Mock()
    agent.history = []
    state = CliState("openai", "model")
    renderer = Mock()
    handler = _make_handler(agent=agent, state=state, renderer=renderer)
    mock_data = {
        "id": "20260909-120000-aabbcc",
        "title": "測試對話",
        "history": [{"role": "user", "content": "hi"}],
    }
    with patch("pocket_agent.core.sessions.load_session", return_value=mock_data):
        asyncio.run(handler.dispatch("/session load 20260909-120000-aabbcc"))
    assert agent.history == mock_data["history"]
    assert state.session_id == "20260909-120000-aabbcc"
    assert "已載入" in renderer.message.call_args.args[0]


def test_session_load_not_found():
    """/session load 不存在的 id 應拋 ValueError，不改 history。"""
    agent = Mock()
    agent.history = ["original"]
    handler = _make_handler(agent=agent)
    with patch("pocket_agent.core.sessions.load_session", return_value=None):
        with pytest.raises(ValueError, match="找不到 session"):
            asyncio.run(handler.dispatch("/session load nonexistent"))
    assert agent.history == ["original"]


def test_session_save_ok():
    """有 history 時 /session save 呼叫 save_session。"""
    agent = Mock()
    agent.history = [{"role": "user", "content": "test"}]
    state = CliState("openai", "model")
    handler = _make_handler(agent=agent, state=state)
    with patch("pocket_agent.core.sessions.save_session") as mock_save:
        asyncio.run(handler.dispatch("/session save"))
    mock_save.assert_called_once_with(state.session_id, agent.history)


def test_session_delete_ok():
    """/session delete <id> 呼叫 delete_session。"""
    renderer = Mock()
    handler = _make_handler(renderer=renderer)
    mock_data = {"id": "abc", "title": "要刪除的對話"}
    with (
        patch("pocket_agent.core.sessions.load_session", return_value=mock_data),
        patch("pocket_agent.core.sessions.delete_session") as mock_del,
    ):
        asyncio.run(handler.dispatch("/session delete abc"))
    mock_del.assert_called_once_with("abc")
    assert "已刪除" in renderer.message.call_args.args[0]
    assert "要刪除的對話" in renderer.message.call_args.args[0]


def test_config_set_numeric_and_bool_types():
    from pocket_agent.system.config import config
    from pocket_agent.system.sys_commands import sys_registry

    res_int = sys_registry.execute("/config set test_int 42", None)
    assert res_int.status == "success"
    assert config.get("test_int") == 42

    res_float = sys_registry.execute("/config set test_float 0.75", None)
    assert res_float.status == "success"
    assert config.get("test_float") == 0.75

    res_bool = sys_registry.execute("/config set test_bool false", None)
    assert res_bool.status == "success"
    assert config.get("test_bool") is False

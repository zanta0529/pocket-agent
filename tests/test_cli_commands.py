import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from agent.frontend.cli_commands import CommandHandler, switch_model
from agent.frontend.cli_state import CliState


def test_model_switch_commits_state_and_closes_old_client():
    old, new = SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=AsyncMock())
    agent = SimpleNamespace(client=old, model="old", history=["preserve"])
    state = CliState("openai", "old", ["pending"])
    with patch("agent.auth.get_openai_client", return_value=new):
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
    with patch("agent.auth.get_openai_client", side_effect=RuntimeError("offline")):
        with pytest.raises(RuntimeError):
            asyncio.run(switch_model(agent, state, "ollama", "new", Mock()))
    assert agent.client is old
    assert (state.provider, state.model) == ("openai", "old")
    old.close.assert_not_awaited()


@pytest.mark.parametrize("raw", ["/modelxxx", "/quit extra", "/image", "/attachments remove 0"])
def test_invalid_commands_preserve_attachments(raw):
    state = CliState("openai", "model", ["pending"])
    handler = CommandHandler(Mock(), state, Mock(), Mock(), [])
    with patch("agent.commands.get_command", return_value=None):
        with pytest.raises(ValueError):
            asyncio.run(handler.dispatch(raw))
    assert state.pending_files == ["pending"]


def test_clear_and_add_dir_do_not_consume_attachments():
    agent, renderer = Mock(), Mock()
    state = CliState("openai", "model", ["pending"])
    handler = CommandHandler(agent, state, Mock(), renderer, [])
    asyncio.run(handler.dispatch("/clear"))
    asyncio.run(handler.dispatch("/add-dir any-directory"))
    agent.clear_history.assert_called_once()
    assert state.pending_files == ["pending"]
    assert "不會讀取目錄" in renderer.message.call_args.args[0]


def test_model_selection_unavailable_allows_manual_entry():
    state = CliState("openai", "old")
    reader = SimpleNamespace(choose=AsyncMock(side_effect=["ollama", "manual"]))
    handler = CommandHandler(Mock(), state, reader, Mock(), [])
    with (
        patch("agent.auth.fetch_available_models", return_value=[]),
        patch("agent.frontend.cli_commands.switch_model", new_callable=AsyncMock) as switch,
    ):
        asyncio.run(handler.dispatch("/model"))
        switch.assert_awaited_once()
        assert switch.call_args.args[2:4] == ("ollama", "manual")
    assert reader.choose.call_args.args[1] == []


def test_model_selection_cancel_keeps_state():
    state = CliState("openai", "old")
    reader = SimpleNamespace(choose=AsyncMock(return_value=None))
    handler = CommandHandler(Mock(), state, reader, Mock(), [])
    with patch("agent.auth.get_openai_client") as create:
        asyncio.run(handler.dispatch("/model"))
        create.assert_not_called()
    assert state.model == "old"


def test_old_client_cleanup_failure_does_not_rollback():
    old = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("close")))
    new = SimpleNamespace(close=AsyncMock())
    agent = SimpleNamespace(client=old, model="old")
    state = CliState("openai", "old")
    with patch("agent.auth.get_openai_client", return_value=new):
        asyncio.run(switch_model(agent, state, "ollama", "new", Mock()))
    assert agent.client is new and state.model == "new"


def test_custom_expansion_failure_retains_queue():
    state = CliState("openai", "model", ["pending"])
    handler = CommandHandler(Mock(), state, Mock(), Mock(), [])
    with (
        patch("agent.commands.get_command", return_value={"name": "custom"}),
        patch("agent.commands.expand_command", return_value=None),
    ):
        with pytest.raises(ValueError):
            asyncio.run(handler.dispatch("/custom"))
    assert state.pending_files == ["pending"]

"""Researcher 沙盒筆記、子代理檢查點與 Housekeeping 整合測試。"""

import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pocket_agent.core.agent import Agent
from pocket_agent.core.subagents import get_subagent
from pocket_agent.core.tool_registry import ScratchToolHandler, default_tool_registry
from pocket_agent.system.housekeeping import Housekeeper
from pocket_agent.tools.fs import call_scratch_tool


def test_researcher_definition_allows_scratch_note_only():
    sa = get_subagent("researcher")
    assert sa is not None
    allowed = sa["allowed_tools"]
    assert "write_scratch_note" in allowed
    assert "write_to_file" not in allowed
    assert "append_to_file" not in allowed


def test_write_scratch_note_write_append_and_isolation(tmp_path, monkeypatch):
    fake_scratch = tmp_path / "scratch"
    monkeypatch.setattr("pocket_agent.system.paths.scratch_dir", lambda cwd=None: fake_scratch)

    res1 = call_scratch_tool(
        "write_scratch_note",
        {"filename": "research_1.md", "content": "# 標題\n第一段"},
    )
    assert res1.startswith("[SUCCESS]")
    note_file = fake_scratch / "notes" / "research_1.md"
    assert note_file.is_file()
    assert note_file.read_text(encoding="utf-8") == "# 標題\n第一段"

    res2 = call_scratch_tool(
        "write_scratch_note",
        {"filename": "research_1.md", "content": "\n第二段", "append": True},
    )
    assert res2.startswith("[SUCCESS]")
    assert note_file.read_text(encoding="utf-8") == "# 標題\n第一段\n第二段"

    # 阻擋目錄穿越與絕對路徑
    err_traversal = call_scratch_tool(
        "write_scratch_note",
        {"filename": "../escape.md", "content": "bad"},
    )
    assert err_traversal.startswith("[ERROR]")

    err_abs = call_scratch_tool(
        "write_scratch_note",
        {"filename": str(tmp_path / "abs.md"), "content": "bad"},
    )
    assert err_abs.startswith("[ERROR]")

    err_env = call_scratch_tool(
        "write_scratch_note",
        {"filename": ".env.secret", "content": "bad"},
    )
    assert err_env.startswith("[ERROR]")


def test_scratch_tool_handler_blocks_sensitive_and_not_parallel_safe(tmp_path, monkeypatch):
    fake_scratch = tmp_path / "scratch"
    monkeypatch.setattr("pocket_agent.system.paths.scratch_dir", lambda cwd=None: fake_scratch)

    handler = default_tool_registry.resolve("write_scratch_note")
    assert isinstance(handler, ScratchToolHandler)
    assert handler.is_parallel_safe("write_scratch_note", {}, Mock()) is False

    sensitive_agent = SimpleNamespace(is_sensitive_session=True, scratch_note_paths=[])
    res = asyncio.run(
        handler.execute("write_scratch_note", {"filename": "a.md", "content": "secret"}, "c1", sensitive_agent)
    )
    assert res.output.startswith("[ERROR]")
    assert not (fake_scratch / "notes" / "a.md").exists()

    normal_agent = SimpleNamespace(is_sensitive_session=False, scratch_note_paths=[])
    res_ok = asyncio.run(
        handler.execute("write_scratch_note", {"filename": "a.md", "content": "ok"}, "c2", normal_agent)
    )
    assert res_ok.output.startswith("[SUCCESS]")
    assert len(normal_agent.scratch_note_paths) == 1


def test_subagent_instructions_prefer_scratch_note_when_write_to_file_disallowed(tmp_path, monkeypatch):
    fake_scratch = tmp_path / "scratch"
    monkeypatch.setattr("pocket_agent.system.paths.scratch_dir", lambda cwd=None: fake_scratch)

    agent = Agent(
        client=Mock(),
        mcp=Mock(),
        is_subagent=True,
        allowed_tools=["read_file", "search_web", "write_scratch_note"],
    )
    agent.scratch_note_filename = "researcher_999.md"
    inst = agent._instructions(repo_map_str="", skills_prompt="")
    assert "write_scratch_note" in inst
    assert "researcher_999.md" in inst
    assert "write_to_file" not in inst


def test_run_subagent_writes_checkpoint_on_exception_and_max_rounds(tmp_path, monkeypatch):
    fake_scratch = tmp_path / "scratch"
    monkeypatch.setattr("pocket_agent.system.paths.scratch_dir", lambda cwd=None: fake_scratch)

    parent = Agent(client=Mock(), mcp=Mock())

    # 1. 異常中斷：應保留檢查點與筆記路徑於例外訊息
    sub_err = Agent(client=Mock(), mcp=Mock(), is_subagent=True)
    sub_err.history = [
        {"type": "function_call", "call_id": "c1", "name": "search_web", "arguments": '{"query": "test"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "found result"},
    ]
    sub_err.scratch_note_paths = [str(fake_scratch / "notes" / "n1.md")]
    sub_err.chat = AsyncMock(side_effect=RuntimeError("Quota exceeded"))

    monkeypatch.setattr("pocket_agent.core.agent.Agent", lambda **kwargs: sub_err)
    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(parent._run_subagent({"name": "researcher", "task": "研究測試"}))

    msg = str(exc_info.value)
    assert "Quota exceeded" in msg
    assert "research_checkpoint_researcher_" in msg
    assert "n1.md" in msg
    ckpts = list(fake_scratch.glob("research_checkpoint_researcher_*.md"))
    assert len(ckpts) == 1
    assert "Quota exceeded" in ckpts[0].read_text(encoding="utf-8")

    # 2. 達工具輪次上限：應於回傳文字附帶檢查點提示
    sub_max = Agent(client=Mock(), mcp=Mock(), is_subagent=True)
    sub_max.history = [
        {"type": "function_call", "call_id": "c2", "name": "read_file", "arguments": '{"path": "a.py"}'},
        {"type": "function_call_output", "call_id": "c2", "output": "code"},
    ]
    sub_max.chat = AsyncMock(return_value=("", "（子代理已達工具呼叫上限，且未產出額外分析內容）", ""))

    monkeypatch.setattr("pocket_agent.core.agent.Agent", lambda **kwargs: sub_max)
    reply = asyncio.run(parent._run_subagent({"name": "researcher", "task": "輪次測試"}))
    assert "已達工具呼叫上限" in reply
    assert "research_checkpoint_researcher_" in reply

    # 3. 敏感會話：不得寫入檢查點檔案
    sub_sens = Agent(client=Mock(), mcp=Mock(), is_subagent=True)
    sub_sens.is_sensitive_session = True
    sub_sens.history = [
        {"type": "function_call", "call_id": "c3", "name": "rhel_run", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c3", "output": "sensitive"},
    ]
    sub_sens.chat = AsyncMock(side_effect=RuntimeError("Network error"))

    before_count = len(list(fake_scratch.glob("research_checkpoint_researcher_*.md")))
    monkeypatch.setattr("pocket_agent.core.agent.Agent", lambda **kwargs: sub_sens)
    with pytest.raises(RuntimeError):
        asyncio.run(parent._run_subagent({"name": "researcher", "task": "敏感測試"}))
    after_count = len(list(fake_scratch.glob("research_checkpoint_researcher_*.md")))
    assert after_count == before_count


def test_housekeeper_cleans_scratch_notes_and_dynamic_checkpoints(tmp_path):
    scratch_dir = tmp_path / "scratch"
    notes_dir = scratch_dir / "notes"
    notes_dir.mkdir(parents=True)

    now = time.time()
    ten_days_ago = now - (10 * 86400)

    old_note = notes_dir / "researcher_old.md"
    old_note.write_text("old note", encoding="utf-8")
    os.utime(old_note, (ten_days_ago, ten_days_ago))

    protected_note = notes_dir / "notes.md"
    protected_note.write_text("keep me", encoding="utf-8")
    os.utime(protected_note, (ten_days_ago, ten_days_ago))

    fresh_note = notes_dir / "researcher_new.md"
    fresh_note.write_text("fresh", encoding="utf-8")

    old_ckpt = scratch_dir / "research_checkpoint_researcher_123.md"
    old_ckpt.write_text("old ckpt", encoding="utf-8")
    os.utime(old_ckpt, (ten_days_ago, ten_days_ago))

    fresh_ckpt = scratch_dir / "research_checkpoint_researcher_456.md"
    fresh_ckpt.write_text("fresh ckpt", encoding="utf-8")

    hk = Housekeeper(
        target_log_dir=tmp_path / "logs",
        target_scratch_dir=scratch_dir,
        cwd=tmp_path,
    )
    cleaned_notes, _ = hk.clean_scratch_notes(ttl_days=7)
    cleaned_ckpts, _ = hk.clean_scratch_checkpoints(ttl_days=7)

    assert cleaned_notes == 1
    assert not old_note.exists()
    assert protected_note.exists()
    assert fresh_note.exists()

    assert cleaned_ckpts == 1
    assert not old_ckpt.exists()
    assert fresh_ckpt.exists()

"""單元測試：非同步並發安全、MultiStatus 渲染、同輪平行子代理與唯讀背景子代理。"""

import asyncio
import concurrent.futures
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prompt_toolkit.formatted_text import fragment_list_to_text
from rich.console import Console

from pocket_agent.core.agent import Agent
from pocket_agent.core.cancellation import current_scope
from pocket_agent.core.subagents import build_subagent_labels
from pocket_agent.core.tool_registry import (
    AskQuestionToolHandler,
    BackgroundSubagentToolHandler,
    ShellToolHandler,
    SkillToolHandler,
)
from pocket_agent.engines.base import BaseEngine, EventText, EventToolCallDone, EventToolCallStart
from pocket_agent.engines.factory import EngineFactory
from pocket_agent.extensions import skills as skills_mod
from pocket_agent.frontend.cli.commands import CommandHandler
from pocket_agent.frontend.cli.render import CliRenderer, MultiStatus, toolbar
from pocket_agent.frontend.cli.state import CliState
from pocket_agent.system import constants
from pocket_agent.tools.repo_map import RepoMapGenerator


def test_skills_and_repo_map_thread_safe_concurrency(tmp_path: Path, monkeypatch) -> None:
    """AC-1: 多執行緒同時同步技能索引與建置 RepoMap，不得發生鎖定或競態錯誤。"""
    db_dir = tmp_path / "db"
    db_dir.mkdir(parents=True)
    skill_dir = tmp_path / "repo" / ".agents" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\ndescription: concurrent test\n---\nBody\n", "utf-8")

    py_dir = tmp_path / "repo" / "pkg"
    py_dir.mkdir(parents=True)
    (py_dir / "mod.py").write_text("def hello(x: int) -> int:\n    return x + 1\n", encoding="utf-8")

    monkeypatch.setattr(skills_mod, "_get_db_path", lambda: db_dir / "skills_index.db")
    monkeypatch.setattr(skills_mod, "_last_sync_state", None)
    monkeypatch.chdir(tmp_path / "repo")

    repo_gen = RepoMapGenerator(db_path=db_dir / "repo_map.db")

    def _worker(idx: int) -> tuple[int, str]:
        skills_mod.sync_index_if_needed([str(tmp_path / "repo")])
        hits = skills_mod.search_skills("concurrent", [str(tmp_path / "repo")])
        rm = repo_gen.build_repo_map([py_dir], budget_tokens=500)
        return len(hits), rm

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_worker, range(12)))

    assert all(hit_count >= 1 and "hello" in rm for hit_count, rm in results)


def test_multistatus_preserves_across_tools_and_deduplicates_labels() -> None:
    """AC-2: MultiStatus 在 tool_start/thinking 不被停止，且同名子代理具備獨立標籤。"""
    assert build_subagent_labels([{"name": "researcher"}, {"name": "coder"}, {"name": "researcher"}]) == [
        "researcher#1",
        "coder",
        "researcher#2",
    ]

    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=100)
    renderer = CliRenderer(console)
    multi = MultiStatus("Running", ["researcher", "researcher"], console=console)
    multi.is_running = True
    renderer._active_status = multi

    renderer.event("thinking", "[researcher#1] 正在分析結構")
    renderer.event(
        "tool_start",
        {"call_id": "c1", "name": "researcher#2::read_file", "arguments": '{"path": "a.py"}', "is_safe_read": True},
    )
    renderer.event("tool_done", {"call_id": "c1", "name": "researcher#2::read_file", "ok": True})

    assert renderer._active_status is multi
    assert "正在分析結構" in multi._states["researcher#1"]
    assert "Read (read_file)" in multi._states["researcher#2"]
    assert renderer._thinking_buffer == ""


def test_auto_multistatus_events_and_non_terminal_noop() -> None:
    """AC-2: parallel_subagents_start/done 正確管理自動 MultiStatus 生命週期。"""
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=100)
    renderer = CliRenderer(console)
    renderer.event("parallel_subagents_start", {"labels": ["researcher#1", "researcher#2"]})
    assert renderer._auto_multi_status is False
    renderer.event("parallel_subagents_done", {})
    assert renderer._auto_multi_status is False


def test_same_turn_multiple_readonly_run_subagent_execute_concurrently(monkeypatch) -> None:
    """AC-3: 同輪發出多筆唯讀 run_subagent 會並發執行並受 Semaphore 上限節流。"""

    async def _scenario() -> None:
        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []

        active_subs = 0
        max_seen = 0
        round_no = 0

        class MockOrchestratorEngine(BaseEngine):
            async def stream_chat(
                self, input_items, instructions, tools, reasoning_param=None, cancel_check=None, **kwargs
            ):
                nonlocal round_no, active_subs, max_seen
                # 子代理透過 system_prompt 區分
                if "SUB_PROMPT" in instructions:
                    active_subs += 1
                    max_seen = max(max_seen, active_subs)
                    await asyncio.sleep(0.08)
                    active_subs -= 1
                    yield EventText("subagent done")
                    return

                round_no += 1
                if round_no == 1:
                    for i in range(4):
                        cid = f"call_sub_{i}"
                        yield EventToolCallStart(cid, "run_subagent")
                        yield EventToolCallDone(cid, "run_subagent", f'{{"name": "researcher", "task": "t{i}"}}')
                else:
                    yield EventText("all done")

        monkeypatch.setattr(EngineFactory, "create", lambda *a, **kw: MockOrchestratorEngine(mock_client, "m"))
        sub_def = {
            "name": "researcher",
            "description": "ro",
            "allowed_tools": ["read_file", "search_files"],
            "system_prompt": "SUB_PROMPT",
        }
        monkeypatch.setattr("pocket_agent.core.agent.get_subagent", lambda n: sub_def if n == "researcher" else None)
        monkeypatch.setattr(
            "pocket_agent.core.tool_registry.get_subagent", lambda n: sub_def if n == "researcher" else None
        )
        monkeypatch.setattr("pocket_agent.core.subagents.list_subagents", lambda: [sub_def])

        agent = Agent(client=mock_client, mcp=mock_mcp, model="m", provider="openai")
        events: list[str] = []
        agent.on_stream = lambda kind, _data: events.append(kind)

        _t, reply, _u = await agent.chat("run 4 researchers")
        assert reply == "all done"
        assert 2 <= max_seen <= constants.DEFAULT_MAX_PARALLEL_SUBAGENTS
        assert "parallel_subagents_start" in events
        assert "parallel_subagents_done" in events

    asyncio.run(_scenario())


def test_sync_offload_and_non_interactive_ask_question_guard() -> None:
    """AC-4: Skill/Shell 快照經 run_sync 卸載；非互動子代理呼叫 ask_question 直接拒絕。"""

    async def _scenario() -> None:
        fake_agent = SimpleNamespace(
            _non_interactive=True,
            workspace_dirs=[],
            on_confirm=None,
            activity=MagicMock(),
        )
        ask_res = await AskQuestionToolHandler().execute("ask_question", {"questions": []}, "c1", fake_agent)
        assert ask_res.output.startswith("[ERROR]")

        with patch("pocket_agent.core.tool_registry.run_sync", new_callable=AsyncMock, return_value="ok") as mock_sync:
            skill_res = await SkillToolHandler().execute("use_skill", {"name": "x"}, "c2", fake_agent)
            assert skill_res.output == "ok"
            mock_sync.assert_awaited_once()

        with patch(
            "pocket_agent.core.tool_registry.run_sync",
            new_callable=AsyncMock,
            side_effect=[{"snap": 1}, "exit code: 0\nok", None],
        ) as mock_shell_sync:
            sh_res = await ShellToolHandler().execute("run_command", {"command": "echo 1"}, "c3", fake_agent)
            assert "exit code: 0" in sh_res.output
            assert mock_shell_sync.await_count == 3

    asyncio.run(_scenario())


def test_background_subagent_readonly_filtering_and_rejection(monkeypatch) -> None:
    """AC-5: 背景子代理嚴格過濾唯讀工具，強制 on_confirm=None 且純寫入子代理被拒絕。"""

    async def _scenario() -> None:
        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = [
            {"type": "function", "name": "mcp__sqlite__query", "_is_safe_read": True},
            {"type": "function", "name": "mcp__browser__navigate", "_is_safe_read": True},
            {"type": "function", "name": "mcp__custom__win_rdp_screenshot", "_is_safe_read": True},
            {"type": "function", "name": "mcp__sqlite__sqlite_switch_database", "_is_safe_read": True},
        ]

        agent = Agent(client=mock_client, mcp=mock_mcp, model="m", provider="openai")
        agent.on_confirm = MagicMock(return_value=True)
        agent.accept_edits = True

        filtered = agent.resolve_readonly_subagent_tools(
            [
                "read_file",
                "write_to_file",
                "run_command",
                "ask_question",
                "mcp__sqlite__query",
                "mcp__browser__navigate",
                "mcp__custom__win_rdp_screenshot",
                "mcp__sqlite__sqlite_switch_database",
            ]
        )
        assert filtered == ["read_file", "mcp__sqlite__query"]

        write_only_def = {"name": "writer", "description": "w", "allowed_tools": ["write_to_file", "run_command"]}
        monkeypatch.setattr(
            "pocket_agent.core.subagents.get_subagent", lambda n: write_only_def if n == "writer" else None
        )

        with pytest.raises(ValueError, match="唯讀"):
            agent.start_background_subagent("writer", "modify files")

    asyncio.run(_scenario())


def test_background_subagent_isolation_ctrl_c_and_shutdown(monkeypatch) -> None:
    """AC-6: 背景子代理具備獨立 CancellationScope，前景 request_stop 不中斷它，shutdown 會回收。"""

    async def _scenario() -> None:
        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []

        started = asyncio.Event()
        observed_scopes: list[object] = []

        async def _bg_chat(self_sub, task: str) -> tuple[str, str, str]:
            observed_scopes.append(current_scope.get())
            started.set()
            await asyncio.sleep(5.0)
            return "", "finished", "0s"

        sub_def = {"name": "researcher", "description": "r", "allowed_tools": ["read_file"], "system_prompt": "SP"}
        monkeypatch.setattr(
            "pocket_agent.core.subagents.get_subagent", lambda n: sub_def if n == "researcher" else None
        )
        monkeypatch.setattr(Agent, "chat", _bg_chat)

        agent = Agent(client=mock_client, mcp=mock_mcp, model="m", provider="openai")
        fg_scope = object()
        token = current_scope.set(fg_scope)  # type: ignore[arg-type]
        try:
            job = agent.start_background_subagent("researcher", "long background research")
            await asyncio.wait_for(started.wait(), timeout=1.0)

            assert observed_scopes and observed_scopes[0] is not fg_scope
            assert job.status == "running"

            # 前景 Ctrl+C / request_stop 不得中斷背景工作
            agent.request_stop()
            await asyncio.sleep(0.05)
            assert job.status == "running"

            await agent.shutdown(timeout=1.0)
            assert job.status == "cancelled"
        finally:
            current_scope.reset(token)

    asyncio.run(_scenario())


def test_background_subagent_llm_tools_and_sensitivity_propagation(monkeypatch) -> None:
    """AC-7: LLM 派發／查詢／取回／取消工具、上限檢查、敏感旗標傳染與子代理隱藏。"""

    async def _scenario() -> None:
        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []

        async def _fast_chat(self_sub, task: str) -> tuple[str, str, str]:
            self_sub.is_sensitive_session = True
            self_sub.activity.record_file_change("notes.md", "added", existed_before=False)
            return "", f"report for {task}", "0s"

        sub_def = {
            "name": "researcher",
            "description": "r",
            "allowed_tools": ["read_file"],
            "system_prompt": "SP",
            "post_actions": {"require_save_prompt": True},
        }
        monkeypatch.setattr(
            "pocket_agent.core.subagents.get_subagent", lambda n: sub_def if n == "researcher" else None
        )
        monkeypatch.setattr("pocket_agent.core.subagents.list_subagents", lambda: [sub_def])
        monkeypatch.setattr(Agent, "chat", _fast_chat)

        main_agent = Agent(client=mock_client, mcp=mock_mcp, model="m", provider="openai")
        main_tools = {t.get("name") for t in main_agent._build_tools() if t.get("name")}
        assert {"start_background_subagent", "check_background_subagent", "cancel_background_subagent"} <= main_tools

        sub_agent = main_agent.create_ephemeral_subagent("SP", allowed_tools=None)
        sub_tools = {t.get("name") for t in sub_agent._build_tools() if t.get("name")}
        assert "start_background_subagent" not in sub_tools

        handler = BackgroundSubagentToolHandler()
        start_res = await handler.execute(
            "start_background_subagent",
            {"name": "researcher", "task": "audit"},
            "c1",
            main_agent,
        )
        assert "job_id:" in start_res.output
        job = main_agent.list_background_jobs()[0]

        check_res = await handler.execute(
            "check_background_subagent",
            {"job_id": job.job_id, "wait_seconds": 2},
            "c2",
            main_agent,
        )
        assert "<external_content>" in check_res.output
        assert "report for audit" in check_res.output
        assert "ask_question" in check_res.output
        assert main_agent.is_sensitive_session is True
        assert job.collected is True

        # 驗證並發上限拒絕
        main_agent.background_jobs._max_concurrent = 0
        with pytest.raises(RuntimeError, match="上限"):
            main_agent.start_background_subagent("researcher", "over limit")

    asyncio.run(_scenario())


def test_cli_bg_jobs_commands_and_toolbar(monkeypatch) -> None:
    """AC-8: /bg、/jobs 清單／取回／取消與 toolbar 背景計數顯示。"""

    async def _scenario() -> None:
        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []

        async def _done_chat(self_sub, task: str) -> tuple[str, str, str]:
            return "", f"done: {task}", "0s"

        sub_def = {"name": "researcher", "description": "r", "allowed_tools": ["read_file"], "system_prompt": "SP"}
        monkeypatch.setattr(
            "pocket_agent.core.subagents.get_subagent", lambda n: sub_def if n == "researcher" else None
        )
        monkeypatch.setattr(Agent, "chat", _done_chat)

        agent = Agent(client=mock_client, mcp=mock_mcp, model="m", provider="openai")
        state = CliState("openai", "m")
        output = StringIO()
        renderer = CliRenderer(Console(file=output, force_terminal=False, no_color=True, width=100))
        handler = CommandHandler(agent, state, MagicMock(), renderer, [])

        await handler.dispatch("/bg researcher scan docs")
        jobs = agent.list_background_jobs()
        assert len(jobs) == 1
        await agent.wait_background_job(jobs[0].job_id, timeout=1.0)
        state.bg_jobs_running, state.bg_jobs_done = agent.background_job_counts()

        bar_text = fragment_list_to_text(toolbar(state, 160))
        assert "待取回" in bar_text

        await handler.dispatch("/jobs")
        assert jobs[0].job_id in output.getvalue()

        await handler.dispatch(f"/jobs collect {jobs[0].job_id}")
        assert state.bg_jobs_done == 0
        assert any("done: scan docs" in str(m.get("content", "")) for m in agent.history)

        # Toolbar 必須相容簡易／Mock state
        mock_state = SimpleNamespace(provider="openai", model="m", pending_files=[], session_tokens=0)
        assert "openai" in fragment_list_to_text(toolbar(mock_state, 100))  # type: ignore[arg-type]

    asyncio.run(_scenario())

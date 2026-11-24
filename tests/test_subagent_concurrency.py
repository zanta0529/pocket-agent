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


def test_background_visibility_two_line_toolbar_footer_and_toast(monkeypatch) -> None:
    """方案 A + B + C: 兩行動態背景狀態列、回覆尾註徽章與非侵入式完成通知。"""
    import time as _time

    from rich.cells import cell_len

    from pocket_agent.frontend.cli.main import conversation

    # 1. 方案 A: 閒置單行 vs 執行中兩行 vs 完成後提示
    state = CliState("openai", "nvidia/nemotron-3.5-super-vl:free")
    idle_text = fragment_list_to_text(toolbar(state, 100))
    assert "\n" not in idle_text

    state.bg_jobs_running = 1
    state.bg_job_summaries = [
        {
            "job_id": "bg-111111",
            "name": "researcher",
            "status": "running",
            "progress": "tool: search_web",
            "created_at": _time.time() - 5,
            "finished_at": None,
        }
    ]
    running_text = fragment_list_to_text(toolbar(state, 100))
    lines = running_text.split("\n")
    assert len(lines) == 2
    assert "bg-111111 [researcher] tool: search_web" in lines[0]
    assert all(cell_len(line) <= 100 for line in lines)

    state.bg_jobs_running = 0
    state.bg_jobs_done = 1
    state.bg_job_summaries = [
        {
            "job_id": "bg-111111",
            "name": "researcher",
            "status": "completed",
            "progress": "completed",
            "created_at": _time.time() - 5,
            "finished_at": _time.time(),
        }
    ]
    done_text = fragment_list_to_text(toolbar(state, 120))
    assert "待取回" in done_text and "/jobs collect" in done_text

    # 2. 方案 C: 回覆尾註附帶背景作業徽章
    stream = StringIO()
    renderer = CliRenderer(Console(file=stream, force_terminal=False, no_color=True, width=120))
    renderer._state = state
    renderer.reply("ok", 1.2, 100, streamed=False)
    assert "背景 1 待取回" in stream.getvalue()

    # 3. 方案 B: conversation 中背景完成通知（提示列作用中即時通知 + 忙碌時佇列補印）
    async def _toast_scenario() -> None:
        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []

        async def _quick_chat(self_sub, task: str) -> tuple[str, str, str]:
            await asyncio.sleep(0.03)
            return "", "bg report", "0s"

        sub_def = {"name": "researcher", "description": "r", "allowed_tools": ["read_file"], "system_prompt": "SP"}
        monkeypatch.setattr(
            "pocket_agent.core.subagents.get_subagent", lambda n: sub_def if n == "researcher" else None
        )
        monkeypatch.setattr(Agent, "chat", _quick_chat)

        agent = Agent(client=mock_client, mcp=mock_mcp, model="m", provider="openai")
        cli_state = CliState("openai", "m")
        out = StringIO()
        cli_renderer = CliRenderer(Console(file=out, force_terminal=False, no_color=True, width=120))

        # 啟動背景工作時 reader.notify_in_terminal 回傳 False -> 應排入佇列並於下次 read 前印出
        job = agent.start_background_subagent("researcher", "news")
        await agent.wait_background_job(job.job_id, timeout=1.0)
        # 重新註冊 on_job_finished 於 conversation() 並觸發一次新工作測試即時通知
        notified_live: list[bool] = []
        read_calls = 0

        async def _fake_read(default: str = "") -> str:
            nonlocal read_calls
            read_calls += 1
            if read_calls == 1:
                j2 = agent.start_background_subagent("researcher", "ai")
                await agent.wait_background_job(j2.job_id, timeout=1.0)
                return "/quit"
            return "/quit"

        def _fake_notify(fn) -> bool:
            notified_live.append(True)
            fn()
            return True

        reader = SimpleNamespace(read=_fake_read, invalidate=MagicMock(), notify_in_terminal=_fake_notify)
        with patch("pocket_agent.core.sessions.save_session"):
            await conversation(agent, cli_state, reader, cli_renderer, [])

        assert notified_live == [True]
        assert "[背景完成]" in out.getvalue()

    asyncio.run(_toast_scenario())


def test_jobs_active_view_all_clear_formatting_and_new_session_cleanup(monkeypatch) -> None:
    """方案 A: /jobs 預設只看活躍、/jobs all、/jobs clear、清單去重與摘要、/new 聯動清理。"""

    async def _scenario() -> None:
        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []

        running_started = asyncio.Event()
        release_running = asyncio.Event()

        async def _chat_mock(self_sub, task: str) -> tuple[str, str, str]:
            if task.startswith("slow"):
                running_started.set()
                await release_running.wait()
                return "", "slow done", "0s"
            return "", f"done: {task}", "0s"

        sub_def = {"name": "researcher", "description": "r", "allowed_tools": ["read_file"], "system_prompt": "SP"}
        monkeypatch.setattr(
            "pocket_agent.core.subagents.get_subagent", lambda n: sub_def if n == "researcher" else None
        )
        monkeypatch.setattr(Agent, "chat", _chat_mock)
        monkeypatch.setattr("pocket_agent.core.sessions.save_session", lambda *a, **kw: None)

        agent = Agent(client=mock_client, mcp=mock_mcp, model="m", provider="openai")
        state = CliState("openai", "m")
        output = StringIO()
        renderer = CliRenderer(Console(file=output, force_terminal=False, no_color=True, width=140))
        handler = CommandHandler(agent, state, MagicMock(), renderer, [])

        # 1. 啟動兩個短任務並等它們完成
        await handler.dispatch("/bg researcher 上網研究 2026 年資安熱門新聞與防禦趨勢")
        await handler.dispatch("/bg researcher 分析 AI 對企業資安治理的影響")
        jobs = agent.list_background_jobs()
        for j in jobs:
            await agent.wait_background_job(j.job_id, timeout=1.0)

        # 未取回前：預設 /jobs 應列出活躍工作，包含任務摘要且不含重複 "· completed"
        output.truncate(0)
        output.seek(0)
        await handler.dispatch("/jobs")
        before_collect = output.getvalue()
        assert jobs[0].job_id in before_collect
        assert "上網研究 2026 年資安熱門新聞" in before_collect
        assert "· completed" not in before_collect

        # check_background_subagent 不帶 job_id 也應去重並含任務摘要
        bg_tool = BackgroundSubagentToolHandler()
        llm_list = (await bg_tool.execute("check_background_subagent", {}, "c0", agent)).output
        assert "| 進度: completed" not in llm_list
        assert "上網研究 2026 年資安熱門新聞" in llm_list

        # 2. 全部取回後：預設 /jobs 不再展開舊清單，且提示切換為 /jobs all 與 /jobs clear
        await handler.dispatch("/jobs collect all")
        output.truncate(0)
        output.seek(0)
        await handler.dispatch("/jobs")
        after_collect = output.getvalue()
        assert jobs[0].job_id not in after_collect
        assert "已取回歸檔紀錄" in after_collect
        assert "/jobs all" in after_collect
        assert "/jobs clear" in after_collect
        assert "/jobs cancel" not in after_collect

        # 3. /jobs all 可回查完整歷史含 [已取回]
        output.truncate(0)
        output.seek(0)
        await handler.dispatch("/jobs all")
        all_out = output.getvalue()
        assert jobs[0].job_id in all_out
        assert "[已取回]" in all_out

        # check_background_subagent 亦應標示 [已取回]
        llm_list_after = (await bg_tool.execute("check_background_subagent", {}, "c1", agent)).output
        assert "[已取回]" in llm_list_after

        # 4. 啟動一個仍在執行中的背景工作後，測試 /jobs clear 只清已完成，保留執行中
        await handler.dispatch("/bg researcher slow background monitor")
        await asyncio.wait_for(running_started.wait(), timeout=1.0)
        slow_job = [j for j in agent.list_background_jobs() if j.status == "running"][0]

        output.truncate(0)
        output.seek(0)
        await handler.dispatch("/jobs clear")
        clear_out = output.getvalue()
        assert "已清除 2 筆" in clear_out
        remaining_ids = [j.job_id for j in agent.list_background_jobs()]
        assert remaining_ids == [slow_job.job_id]

        # 5. 再完成一筆並取回，驗證 /new 會清除已結束工作但保留仍在 running 的工作
        await handler.dispatch("/bg researcher quick summary")
        quick_job = [j for j in agent.list_background_jobs() if j.job_id != slow_job.job_id][0]
        await agent.wait_background_job(quick_job.job_id, timeout=1.0)
        await handler.dispatch(f"/jobs collect {quick_job.job_id}")

        await handler.dispatch("/new")
        after_new_ids = [j.job_id for j in agent.list_background_jobs()]
        assert after_new_ids == [slow_job.job_id]
        assert state.bg_jobs_running == 1
        assert state.bg_jobs_done == 0

        release_running.set()
        await agent.shutdown(timeout=1.0)

    asyncio.run(_scenario())


def test_jobs_show_and_collect_interactive_popup_selection(monkeypatch) -> None:
    """省略 ID 時，/jobs show 與 /jobs collect 開啟互動式光棒選單選擇工作。"""

    async def _scenario() -> None:
        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []

        async def _done_chat(self_sub, task: str) -> tuple[str, str, str]:
            return "", f"report: {task}", "0s"

        sub_def = {"name": "researcher", "description": "r", "allowed_tools": ["read_file"], "system_prompt": "SP"}
        monkeypatch.setattr(
            "pocket_agent.core.subagents.get_subagent", lambda n: sub_def if n == "researcher" else None
        )
        monkeypatch.setattr(Agent, "chat", _done_chat)

        agent = Agent(client=mock_client, mcp=mock_mcp, model="m", provider="openai")
        state = CliState("openai", "m")
        output = StringIO()
        renderer = CliRenderer(Console(file=output, force_terminal=False, no_color=True, width=140))
        reader = MagicMock()
        handler = CommandHandler(agent, state, reader, renderer, [])

        await handler.dispatch("/bg researcher 研究文字生圖模型")
        await handler.dispatch("/bg researcher 研究零知識證明")
        jobs = agent.list_background_jobs()
        for j in jobs:
            await agent.wait_background_job(j.job_id, timeout=1.0)

        # 1. /jobs show 不帶 ID -> 彈出選單並選取第一筆，不寫入 history
        reader.choose = AsyncMock(side_effect=lambda _label, choices: choices[0])
        await handler.dispatch("/jobs show")
        show_choices = reader.choose.call_args[0][1]
        assert len(show_choices) == 2
        assert jobs[0].job_id in show_choices[0]
        assert "研究文字生圖模型" in show_choices[0]
        assert "report: 研究文字生圖模型" in output.getvalue()
        assert agent.history == []

        # 2. /jobs show 按 Esc 取消 (choose 回傳 None) -> 安全返回
        reader.choose = AsyncMock(return_value=None)
        await handler.dispatch("/jobs show")

        # 3. /jobs collect 不帶 ID -> 含兩筆待取回與「全部取回」選項；先選第一筆
        reader.choose = AsyncMock(side_effect=lambda _label, choices: choices[0])
        await handler.dispatch("/jobs collect")
        collect_choices_1 = reader.choose.call_args[0][1]
        assert len(collect_choices_1) == 3
        assert "全部取回" in collect_choices_1[-1]
        assert jobs[0].collected is True
        assert jobs[1].collected is False
        assert state.bg_jobs_done == 1

        # 4. 僅剩 1 筆時再次 /jobs collect -> 不再出現「全部取回」，選中後清空待取回計數
        reader.choose = AsyncMock(side_effect=lambda _label, choices: choices[0])
        await handler.dispatch("/jobs collect")
        collect_choices_2 = reader.choose.call_args[0][1]
        assert len(collect_choices_2) == 1
        assert jobs[1].collected is True
        assert state.bg_jobs_done == 0

    asyncio.run(_scenario())


def test_slash_command_arg_completions_jobs_and_other_commands(monkeypatch) -> None:
    """/jobs 兩階段空白補全、其他斜線指令參數補全，以及 Enter 未高亮直接送出行為。"""
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from pocket_agent.frontend.cli.input import SlashCommandCompleter, key_bindings

    async def _scenario() -> None:
        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []
        mock_mcp.get_grouped_tools.return_value = {"sqlite": [{"name": "query"}], "github": [{"name": "get_issue"}]}

        started = asyncio.Event()
        release = asyncio.Event()

        async def _chat_mock(self_sub, task: str) -> tuple[str, str, str]:
            if task.startswith("slow"):
                started.set()
                await release.wait()
            return "", f"done: {task}", "0s"

        ro_def = {"name": "researcher", "description": "ro", "allowed_tools": ["read_file"], "system_prompt": "SP"}
        rw_def = {"name": "coder", "description": "rw", "allowed_tools": ["write_to_file"], "system_prompt": "SP"}
        monkeypatch.setattr(
            "pocket_agent.core.subagents.get_subagent",
            lambda n: {"researcher": ro_def, "coder": rw_def}.get(n),
        )
        monkeypatch.setattr("pocket_agent.core.subagents.list_subagents", lambda: [ro_def, rw_def])
        monkeypatch.setattr(Agent, "chat", _chat_mock)

        agent = Agent(client=mock_client, mcp=mock_mcp, model="m", provider="openai")
        state = CliState("openai", "m")
        state.pending_files = ["/tmp/report.pdf", "/tmp/notes.md"]
        completer = SlashCommandCompleter(state, agent=agent)
        ev = CompleteEvent()

        def _complete(text: str):
            return list(completer.get_completions(Document(text, len(text)), ev))

        # 1. /jobs 第一階段
        stage1 = [c.text for c in _complete("/jobs ")]
        assert stage1 == ["show", "collect", "all", "clear", "cancel", "list"]
        assert [c.text for c in _complete("/jobs sh")] == ["show"]
        assert _complete("/jobs xyz") == []

        # 建立 2 筆已完成 + 1 筆執行中工作
        j1 = agent.start_background_subagent("researcher", "研究文字生圖")
        j2 = agent.start_background_subagent("researcher", "研究零知識證明")
        await agent.wait_background_job(j1.job_id, timeout=1.0)
        await agent.wait_background_job(j2.job_id, timeout=1.0)
        j3 = agent.start_background_subagent("researcher", "slow monitor")
        await asyncio.wait_for(started.wait(), timeout=1.0)

        # 2. /jobs show / collect / cancel 第二階段
        show_items = _complete("/jobs show ")
        assert [c.text for c in show_items] == [j1.job_id, j2.job_id, j3.job_id]
        assert "研究文字生圖" in str(show_items[0].display_meta)

        collect_items = [c.text for c in _complete("/jobs collect ")]
        assert collect_items == [j1.job_id, j2.job_id, "all"]

        cancel_items = [c.text for c in _complete("/jobs cancel ")]
        assert cancel_items == [j3.job_id]

        # 3. 其他斜線指令補全
        assert [c.text for c in _complete("/reasoning ")] == ["auto", "high", "medium", "low", "none"]
        assert [c.text for c in _complete("/effort h")] == ["high"]
        assert [c.text for c in _complete("/session ")] == ["list", "load", "save", "delete", "prune"]
        assert [c.text for c in _complete("/bg ")] == ["researcher"]
        assert [c.text for c in _complete("/subagent ")] == ["researcher", "coder"]
        assert [c.text for c in _complete("/attachments ")] == ["clear", "remove"]
        assert [c.text for c in _complete("/attachments remove ")] == ["1", "2"]
        assert [c.text for c in _complete("/rm-file ")] == ["1", "2"]
        assert [c.text for c in _complete("/mcp ")] == ["sqlite", "github"]
        assert [c.text for c in _complete("/tools ")] == ["sqlite", "github"]
        assert _complete("/model ") == []

        from pocket_agent.frontend.cli.arg_completers import _DEFAULT_REGISTRY, ArgCompleterStrategy

        assert isinstance(_DEFAULT_REGISTRY.get("jobs"), ArgCompleterStrategy)
        assert _DEFAULT_REGISTRY.get("reasoning") is _DEFAULT_REGISTRY.get("effort")
        assert _DEFAULT_REGISTRY.get("mcp") is _DEFAULT_REGISTRY.get("tools")

        # 4. Enter 鍵在參數補全未高亮時直接送出；高亮時套用補全不送出
        kb = key_bindings(lambda: False, state=state, agent=agent, completer=completer)
        enter_handler = next(b.handler for b in kb.get_bindings_for_keys(("c-m",)))

        first_comp = _complete("/jobs ")[0]
        buf_unselected = SimpleNamespace(
            document=Document("/jobs ", 6),
            complete_state=SimpleNamespace(current_completion=None, completions=[first_comp]),
            apply_completion=MagicMock(),
            validate_and_handle=MagicMock(),
        )
        enter_handler(SimpleNamespace(current_buffer=buf_unselected))
        buf_unselected.apply_completion.assert_not_called()
        buf_unselected.validate_and_handle.assert_called_once()

        buf_selected = SimpleNamespace(
            document=Document("/jobs ", 6),
            complete_state=SimpleNamespace(current_completion=first_comp, completions=[first_comp]),
            apply_completion=MagicMock(),
            validate_and_handle=MagicMock(),
        )
        enter_handler(SimpleNamespace(current_buffer=buf_selected))
        buf_selected.apply_completion.assert_called_once_with(first_comp)
        buf_selected.validate_and_handle.assert_not_called()

        release.set()
        await agent.shutdown(timeout=1.0)

    asyncio.run(_scenario())


def test_model_command_provider_only_opens_model_picker(monkeypatch) -> None:
    """/model <provider> 應開啟第二段模型光棒選單而非誤將 provider 當成模型名稱。"""

    async def _scenario() -> None:
        mock_client = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.openai_tools.return_value = []

        agent = Agent(client=mock_client, mcp=mock_mcp, model="m", provider="openai")
        state = CliState("openai", "m")
        output = StringIO()
        renderer = CliRenderer(Console(file=output, force_terminal=False, no_color=True, width=120))
        reader = MagicMock()
        reader.choose = AsyncMock(return_value="openai/gpt-5")
        handler = CommandHandler(agent, state, reader, renderer, [])

        fetched: list[str] = []

        def _fake_fetch(prov: str) -> list[str]:
            fetched.append(prov)
            return ["openai/gpt-5", "anthropic/claude-sonnet-4"]

        switched: list[tuple[str, str]] = []

        async def _fake_switch(_agent, _state, prov: str, model: str, _renderer) -> None:
            switched.append((prov, model))

        monkeypatch.setattr("pocket_agent.system.auth.fetch_available_models", _fake_fetch)
        monkeypatch.setattr("pocket_agent.frontend.cli.commands.system.switch_model", _fake_switch)

        await handler.dispatch("/model openrouter")
        assert fetched == ["openrouter"]
        assert reader.choose.call_args[0][1] == ["openai/gpt-5", "anthropic/claude-sonnet-4"]
        assert switched == [("openrouter", "openai/gpt-5")]

    asyncio.run(_scenario())

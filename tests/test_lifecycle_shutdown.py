import asyncio
import subprocess
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

from pocket_agent.core.agent import Agent
from pocket_agent.extensions.mcp import MCPManager, _ServerRunner
from pocket_agent.tools.shell import (
    _active_shell_procs,
    _kill_proc_tree,
    _shell_proc_lock,
    interrupt_active_shell_processes,
)

# ── CR-003: 子代理 Task 追蹤與中斷 ──────────────────────────────────────────


def test_subagent_task_cancelled_on_request_stop() -> None:
    """測試主代理呼叫 request_stop 時，子代理 Task 會被即刻 cancel 並返回中斷狀態。"""

    async def _test() -> None:
        client = AsyncMock()
        mcp = MagicMock()
        main_agent = Agent(client=client, mcp=mcp, model="main-model")

        started_event = asyncio.Event()

        async def hanging_chat(task: str) -> tuple[str, str, str]:
            started_event.set()
            await asyncio.sleep(10.0)
            return "", "done", "0s"

        sub_config = {
            "name": "tester",
            "model": "sub-model",
            "system_prompt": "You are a test subagent.",
        }

        with (
            patch("pocket_agent.core.agent.get_subagent", return_value=sub_config),
            patch.object(Agent, "chat", side_effect=hanging_chat),
        ):
            run_task = asyncio.create_task(main_agent._run_subagent({"name": "tester", "task": "long work"}))
            await asyncio.wait_for(started_event.wait(), timeout=1.0)

            assert len(main_agent.active_subagents) == 1
            assert len(main_agent._subagent_tasks) == 1

            # 觸發中斷：應回傳 False（表示中斷了子代理而非主代理）
            res = main_agent.request_stop()
            assert res is False

            reply = await asyncio.wait_for(run_task, timeout=1.0)
            assert "已被使用者停止" in reply
            assert len(main_agent.active_subagents) == 0
            assert len(main_agent._subagent_tasks) == 0

    asyncio.run(_test())


# ── CR-005: MCP Task Ownership 與逾時隔離 ───────────────────────────────────


def test_mcp_runner_lifecycle_same_task_and_stop_timeout() -> None:
    """測試 MCP runner 的 context manager 在同一 Task 內進出，且單一服務卡住不影響其他服務收尾。"""

    async def _test() -> None:
        mgr = MCPManager("dummy.yaml")

        entered_task_id = None
        exited_task_id = None

        class DummyContextManager:
            async def __aenter__(self):
                nonlocal entered_task_id
                entered_task_id = id(asyncio.current_task())
                return ("read", "write")

            async def __aexit__(self, exc_type, exc, tb):
                nonlocal exited_task_id
                exited_task_id = id(asyncio.current_task())

        class DummySession:
            def __init__(self, read, write):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                pass

            async def initialize(self):
                pass

        loop = asyncio.get_running_loop()
        ready_fut = loop.create_future()
        stop_evt = asyncio.Event()

        spec = {"transport": "stdio", "command": "echo"}
        with (
            patch("pocket_agent.extensions.mcp.stdio_client", return_value=DummyContextManager()),
            patch("pocket_agent.extensions.mcp.ClientSession", side_effect=DummySession),
        ):
            t = asyncio.create_task(mgr._run_server_lifecycle("dummy", spec, ready_fut, stop_evt))
            session = await asyncio.wait_for(ready_fut, timeout=1.0)
            assert session is not None

            # 驗證 entered_task_id 為 runner task
            assert entered_task_id == id(t)

            mgr._runners["dummy"] = _ServerRunner("dummy", t, stop_evt, session)

            # 加入一個模擬永遠卡住的 runner
            async def stuck_runner():
                await asyncio.sleep(100.0)

            stuck_task = asyncio.create_task(stuck_runner())
            mgr._runners["stuck"] = _ServerRunner("stuck", stuck_task, asyncio.Event())

            # 呼叫 stop：應能於 3 秒內整體收尾，stuck 被 cancel，dummy 正確退出
            t0 = time.monotonic()
            await mgr.stop(timeout=2.0)
            elapsed = time.monotonic() - t0

            assert elapsed < 3.0
            assert exited_task_id == entered_task_id
            assert stuck_task.done()

    asyncio.run(_test())


# ── CR-006: Shell 行程樹終止升級與 Reader 清理 ──────────────────────────────


def test_shell_kill_proc_tree_and_interrupt() -> None:
    """測試 _kill_proc_tree 與 interrupt_active_shell_processes 能正確處理行程終止。"""
    if sys.platform == "win32":
        proc = subprocess.Popen(["cmd.exe", "/c", "pause"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    else:
        proc = subprocess.Popen(["sleep", "30"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    with _shell_proc_lock:
        _active_shell_procs.add(proc)

    try:
        assert proc.poll() is None
        killed = interrupt_active_shell_processes()
        assert killed >= 1
        time.sleep(0.2)
        assert proc.poll() is not None
    finally:
        try:
            _kill_proc_tree(proc)
        except Exception:
            pass
        with _shell_proc_lock:
            _active_shell_procs.discard(proc)


# ── CR-007: Agent 優雅關閉協定（收斂背景 Task）──────────────────────────────


def test_agent_shutdown_cancels_bg_tasks_and_subagents() -> None:
    """測試 agent.shutdown() 會同時取消進行中的背景任務與子代理。"""

    async def _test() -> None:
        client = AsyncMock()
        mcp = MagicMock()
        agent = Agent(client=client, mcp=mcp, model="test-model")

        # 模擬背景學習任務
        bg_completed = False

        async def bg_worker():
            nonlocal bg_completed
            try:
                await asyncio.sleep(10.0)
                bg_completed = True
            except asyncio.CancelledError:
                pass

        bg_task = asyncio.create_task(bg_worker())
        agent._bg_tasks.add(bg_task)

        # 模擬子代理 Task
        sub_agent = Agent(client=client, mcp=mcp, model="sub-model")
        sub_task = asyncio.create_task(asyncio.sleep(10.0))
        agent.active_subagents.append(sub_agent)
        agent._subagent_tasks[sub_agent] = sub_task

        # 執行優雅關閉
        await agent.shutdown(timeout=1.0)

        assert agent.cancel_requested is True
        assert len(agent.active_subagents) == 1  # 關閉協定標記中斷
        assert sub_task.cancelled() or sub_task.done()
        assert bg_task.cancelled() or bg_task.done()
        assert len(agent._bg_tasks) == 0
        assert bg_completed is False

    asyncio.run(_test())

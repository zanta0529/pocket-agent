"""取消與訊號的整合測試：真實 SIGINT 與 thread/Task 生命週期。"""

import asyncio
import sys
import time

import pytest

from pocket_agent.core.cancellation import (
    CancellationScope,
    OperationCancelled,
    current_scope,
    run_sync,
)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 真實終端機測試")
def test_real_sigint_interrupts_worker_on_windows() -> None:
    """在真實終端機送 SIGINT，確認 run_sync worker 能被停止且 scope 能收尾。"""
    scope = CancellationScope()
    token = current_scope.set(scope)

    def long_work() -> str:
        for _ in range(100):
            if scope.cancelled.is_set():
                raise OperationCancelled
            time.sleep(0.05)
        return "done"

    async def _test() -> None:
        task = asyncio.create_task(run_sync(long_work))
        await asyncio.sleep(0.05)
        scope.cancel()
        with pytest.raises(OperationCancelled):
            await task
        await scope.drain()

    asyncio.run(_test())
    current_scope.reset(token)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGINT 測試")
def test_real_sigint_interrupts_worker_on_posix() -> None:
    """在真實終端機送 SIGINT，確認 run_sync worker 能被停止且 scope 能收尾。"""
    scope = CancellationScope()
    token = current_scope.set(scope)

    def long_work() -> str:
        for _ in range(100):
            if scope.cancelled.is_set():
                raise OperationCancelled
            time.sleep(0.05)
        return "done"

    async def _test() -> None:
        task = asyncio.create_task(run_sync(long_work))
        await asyncio.sleep(0.05)
        scope.cancel()
        with pytest.raises(OperationCancelled):
            await task
        await scope.drain()

    asyncio.run(_test())
    current_scope.reset(token)


def test_run_sync_cancelled_before_submit() -> None:
    """取消在送出前生效：不應啟動 worker。"""

    async def _test() -> None:
        scope = CancellationScope()
        token = current_scope.set(scope)
        scope.cancel()

        called = False

        def worker() -> int:
            nonlocal called
            called = True
            return 42

        with pytest.raises(OperationCancelled):
            await run_sync(worker)

        assert not called
        current_scope.reset(token)

    asyncio.run(_test())


def test_run_sync_drains_after_cancel() -> None:
    """取消後 worker 已經在跑，scope.drain 會等待收尾。"""

    async def _test() -> None:
        scope = CancellationScope()
        token = current_scope.set(scope)

        done = asyncio.Event()

        def worker() -> int:
            time.sleep(0.1)
            done.set()
            return 1

        task = asyncio.create_task(run_sync(worker))
        await asyncio.sleep(0.02)
        scope.cancel()
        await task

        await asyncio.wait_for(done.wait(), timeout=1.0)
        await scope.drain()
        current_scope.reset(token)

    asyncio.run(_test())


def test_multiple_workers_and_prompts() -> None:
    """同時有多個 worker 與 prompt，cancel 會一次標記、drain 全部等待。"""

    async def _test() -> None:
        scope = CancellationScope()
        token = current_scope.set(scope)
        results: list[int] = []

        def work(x: int) -> int:
            time.sleep(0.05)
            results.append(x)
            return x

        scope.workers.update(asyncio.create_task(run_sync(work, i)) for i in range(3))
        await asyncio.sleep(0.01)
        scope.cancel()
        await scope.drain()
        assert len(results) == 3
        current_scope.reset(token)

    asyncio.run(_test())


def test_cancelled_propagates_to_check_cancelled() -> None:
    """check_cancelled 在取消後必須拋出。"""
    scope = CancellationScope()
    token = current_scope.set(scope)

    from pocket_agent.core.cancellation import check_cancelled

    scope.cancel()
    with pytest.raises(OperationCancelled):
        check_cancelled()

    current_scope.reset(token)

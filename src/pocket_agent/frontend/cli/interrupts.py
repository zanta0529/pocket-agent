"""CLI 訊號 Adapter；共用取消狀態由 core 擁有。"""

import asyncio
import signal
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pocket_agent.core.cancellation import CancellationScope, current_scope
from pocket_agent.system.constants import DEFAULT_OPERATION_DRAIN_TIMEOUT_SEC

T = TypeVar("T")


class OperationInterrupted(Exception):
    """使用者要求停止當前操作。"""


async def run_operation(operation: Awaitable[T], stopping: Callable[[], None]) -> T:
    loop = asyncio.get_running_loop()
    scope = CancellationScope()
    token = current_scope.set(scope)
    task = asyncio.ensure_future(operation)
    interrupted = False
    force_abort = False
    cleanup_task: asyncio.Task | None = None
    previous = signal.getsignal(signal.SIGINT)

    def interrupt() -> None:
        nonlocal interrupted, force_abort
        if not interrupted:
            interrupted = True
            scope.cancel()
            stopping()
            task.cancel()
            return

        # 若已處於中斷或收尾階段，使用者再次按下 Ctrl+C 觸發強制中止
        if not force_abort:
            force_abort = True
            scope.cancel()
            task.cancel()
            if cleanup_task is not None and not cleanup_task.done():
                cleanup_task.cancel()

    def handler(signum: int, frame: object) -> None:
        loop.call_soon_threadsafe(interrupt)

    installed = False
    try:
        signal.signal(signal.SIGINT, handler)
        installed = True
        try:
            res = await asyncio.shield(task)
            if interrupted:
                raise OperationInterrupted from None
            return res
        except asyncio.CancelledError:
            scope.cancel()
            task.cancel()
            if interrupted:
                raise OperationInterrupted from None
            raise
    finally:
        scope.cancel()

        async def cleanup() -> None:
            await asyncio.gather(task, return_exceptions=True)
            await scope.drain()

        cleanup_task = asyncio.create_task(cleanup())
        try:
            # 加入逾時保護，若工作在保底時間內未退出或遭遇強制中止則立即放棄等待
            try:
                await asyncio.wait_for(
                    asyncio.shield(cleanup_task),
                    timeout=DEFAULT_OPERATION_DRAIN_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError, asyncio.CancelledError:
                cleanup_task.cancel()
                try:
                    await asyncio.wait_for(cleanup_task, timeout=0.3)
                except Exception:
                    pass
        finally:
            current_scope.reset(token)
            if installed:
                signal.signal(signal.SIGINT, previous)

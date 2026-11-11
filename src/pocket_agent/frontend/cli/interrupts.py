"""CLI 訊號 Adapter；共用取消狀態由 core 擁有。"""

import asyncio
import signal
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pocket_agent.core.cancellation import CancellationScope, current_scope

T = TypeVar("T")


class OperationInterrupted(Exception):
    """使用者要求停止當前操作。"""


async def run_operation(operation: Awaitable[T], stopping: Callable[[], None]) -> T:
    loop = asyncio.get_running_loop()
    scope = CancellationScope()
    token = current_scope.set(scope)
    task = asyncio.ensure_future(operation)
    interrupted = False
    draining = False
    previous = signal.getsignal(signal.SIGINT)

    def interrupt() -> None:
        nonlocal interrupted
        if draining or interrupted or task.done():
            return
        interrupted = True
        scope.cancel()
        stopping()
        task.cancel()

    def handler(signum: int, frame: object) -> None:
        loop.call_soon_threadsafe(interrupt)

    installed = False
    try:
        signal.signal(signal.SIGINT, handler)
        installed = True
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            scope.cancel()
            task.cancel()
            if interrupted:
                raise OperationInterrupted from None
            raise
    finally:
        draining = True
        scope.cancel()

        async def cleanup() -> None:
            await asyncio.gather(task, return_exceptions=True)
            await scope.drain()

        cleanup_task = asyncio.create_task(cleanup())
        try:
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    # 必須完成清理；外部取消仍在下方重新傳遞。
                    if not interrupted:
                        task.cancel()
            cleanup_task.result()
        finally:
            current_scope.reset(token)
            if installed:
                signal.signal(signal.SIGINT, previous)

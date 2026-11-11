"""每次操作獨立的取消訊號與同步工作收尾協定。"""

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event
from typing import Any


class OperationCancelled(Exception):
    """同步工作已取消，不得開始下一個副作用。"""


@dataclass(eq=False)
class CancellationScope:
    cancelled: Event = field(default_factory=Event)
    workers: set[asyncio.Task[Any]] = field(default_factory=set)
    prompts: set[asyncio.Task[Any]] = field(default_factory=set)

    def cancel(self) -> None:
        """由 event loop 呼叫；執行緒僅讀取 cancelled。"""
        self.cancelled.set()
        for prompt in tuple(self.prompts):
            prompt.cancel()

    async def drain(self) -> None:
        """不可撤銷的同步操作收尾前，不交還互動控制權。"""
        while self.workers or self.prompts:
            tasks = tuple(self.workers | self.prompts)
            await asyncio.gather(*tasks, return_exceptions=True)
            self.workers.difference_update(task for task in tasks if task.done())
            self.prompts.difference_update(task for task in tasks if task.done())


current_scope: ContextVar[CancellationScope | None] = ContextVar("operation_scope", default=None)


def check_cancelled() -> None:
    scope = current_scope.get()
    if scope is not None and scope.cancelled.is_set():
        raise OperationCancelled("操作已取消，不執行後續動作")


async def run_sync(function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """取消等待者不取消 worker；scope 負責等候實際執行緒結束。"""
    check_cancelled()
    scope = current_scope.get()

    def invoke() -> Any:
        check_cancelled()
        return function(*args, **kwargs)

    worker = asyncio.create_task(asyncio.to_thread(invoke))
    if scope is not None:
        scope.workers.add(worker)
    try:
        return await asyncio.shield(worker)
    finally:
        # 未完成的工作保留給 drain；取走已完成例外，避免遺失錯誤通知。
        if worker.done() and scope is not None:
            scope.workers.discard(worker)

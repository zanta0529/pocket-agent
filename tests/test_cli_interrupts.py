"""CLI 訊號 Adapter 整合測試：真實 SIGINT 與操作收尾。"""

import asyncio
import signal
import sys
import time

import pytest

from pocket_agent.frontend.cli.interrupts import OperationInterrupted, run_operation


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 真實終端機測試")
def test_operation_interrupted_by_real_sigint_on_windows() -> None:
    """真實 SIGINT 應被轉為 OperationInterrupted，不吞下、不跳過 cleanup。"""
    interrupted = False
    cleaned = False

    async def slow_operation() -> str:
        nonlocal interrupted, cleaned
        try:
            loop = asyncio.get_running_loop()
            handler = signal.getsignal(signal.SIGINT)
            if callable(handler):
                loop.call_later(0.05, handler, signal.SIGINT, None)
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            interrupted = True
            raise
        finally:
            await asyncio.sleep(0.05)
            cleaned = True

    async def run() -> str:
        def stopping() -> None:
            pass

        return await run_operation(slow_operation(), stopping)

    with pytest.raises(OperationInterrupted):
        asyncio.run(run())

    assert interrupted
    assert cleaned


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGINT 測試")
def test_operation_interrupted_by_real_sigint_on_posix() -> None:
    """真實 SIGINT 應被轉為 OperationInterrupted，不吞下、不跳過 cleanup。"""
    interrupted = False
    cleaned = False

    async def slow_operation() -> str:
        nonlocal interrupted, cleaned
        try:
            loop = asyncio.get_running_loop()
            handler = signal.getsignal(signal.SIGINT)
            if callable(handler):
                loop.call_later(0.05, handler, signal.SIGINT, None)
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            interrupted = True
            raise
        finally:
            await asyncio.sleep(0.05)
            cleaned = True

    async def run() -> str:
        def stopping() -> None:
            pass

        return await run_operation(slow_operation(), stopping)

    with pytest.raises(OperationInterrupted):
        asyncio.run(run())

    assert interrupted
    assert cleaned


def test_operation_completes_normally_without_signal() -> None:
    """無訊號時正常返回結果，stopping 未被呼叫。"""
    stopping_called = False

    async def quick_operation() -> str:
        await asyncio.sleep(0.01)
        return "ok"

    async def run() -> str:
        def stopping() -> None:
            nonlocal stopping_called
            stopping_called = True

        return await run_operation(quick_operation(), stopping)

    result = asyncio.run(run())
    assert result == "ok"
    assert not stopping_called


def test_external_cancel_propagates() -> None:
    """外部取消（非 SIGINT）仍正常傳遞，不被吞下。"""

    async def _test() -> None:
        async def run() -> str:
            def stopping() -> None:
                pass

            return await run_operation(asyncio.sleep(5), stopping)

        task = asyncio.create_task(run())
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_test())


def test_stopping_called_on_interrupt() -> None:
    """SIGINT 觸發時 stopping callback 應被呼叫一次。"""
    stopping_called = 0

    async def _test() -> None:
        nonlocal stopping_called

        async def slow() -> str:
            loop = asyncio.get_running_loop()
            handler = signal.getsignal(signal.SIGINT)
            if callable(handler):
                loop.call_later(0.02, handler, signal.SIGINT, None)
            await asyncio.sleep(5)
            return "done"

        def stopping() -> None:
            nonlocal stopping_called
            stopping_called += 1

        with pytest.raises(OperationInterrupted):
            await run_operation(slow(), stopping)

    asyncio.run(_test())
    assert stopping_called == 1


def test_draining_on_interrupt() -> None:
    """interrupt 後必須等待正在進行的同步工作收尾。"""
    worker_done = asyncio.Event()

    async def _test() -> None:
        def blocking() -> str:
            time.sleep(0.2)
            worker_done.set()
            return "done"

        from pocket_agent.core.cancellation import run_sync

        async def op() -> str:
            loop = asyncio.get_running_loop()
            handler = signal.getsignal(signal.SIGINT)
            if callable(handler):
                loop.call_later(0.02, handler, signal.SIGINT, None)
            return await run_sync(blocking)

        def stopping() -> None:
            pass

        with pytest.raises(OperationInterrupted):
            await run_operation(op(), stopping)

        await worker_done.wait()
        assert worker_done.is_set()

    asyncio.run(_test())


def test_handler_restored_on_exit() -> None:
    """退出時還原原始 SIGINT handler（不需依賴 asyncio.run）。"""

    original = signal.getsignal(signal.SIGINT)
    custom_called = False

    def custom(signum: int, frame: object) -> None:
        nonlocal custom_called
        custom_called = True

    signal.signal(signal.SIGINT, custom)

    try:

        async def run() -> str:
            def stopping() -> None:
                pass

            return await run_operation(asyncio.sleep(0.01), stopping)

        asyncio.run(run())
        assert signal.getsignal(signal.SIGINT) == custom
    finally:
        signal.signal(signal.SIGINT, original)

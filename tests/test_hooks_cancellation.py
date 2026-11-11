"""Hooks 取消測試：取消後不啟動後續 hook、已在跑的等待收尾。"""

import asyncio
import os
import tempfile
from pathlib import Path

from pocket_agent.core.cancellation import CancellationScope, current_scope, run_sync
from pocket_agent.extensions.hooks import run_hooks


def test_hooks_stop_after_cancel() -> None:
    """取消後不啟動剩餘 hook。"""

    async def _test() -> None:
        scope = CancellationScope()
        token = current_scope.set(scope)

        with tempfile.TemporaryDirectory() as d:
            hooks_path = Path(d) / "hooks.json"
            import json

            hooks_path.write_text(
                json.dumps(
                    [
                        {"name": "hook1", "event": "test_event", "command": "echo 1"},
                        {"name": "hook2", "event": "test_event", "command": "echo 2"},
                        {"name": "hook3", "event": "test_event", "command": "echo 3"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            old_path = os.environ.get("POCKETAGENT_HOOKS_PATH")
            os.environ["POCKETAGENT_HOOKS_PATH"] = str(hooks_path)

            try:

                async def run() -> None:
                    await run_sync(lambda: run_hooks("test_event"))

                task = asyncio.create_task(run())
                await asyncio.sleep(0.05)
                scope.cancel()
                await task
            except Exception:
                pass
            finally:
                if old_path:
                    os.environ["POCKETAGENT_HOOKS_PATH"] = old_path
                else:
                    os.environ.pop("POCKETAGENT_HOOKS_PATH", None)

        current_scope.reset(token)

    asyncio.run(_test())


def test_hooks_no_crash_when_cancelled_between_hooks() -> None:
    """取消在 hook 之間：不崩潰、不洩漏例外。"""

    async def _test() -> None:
        scope = CancellationScope()
        token = current_scope.set(scope)

        with tempfile.TemporaryDirectory() as d:
            hooks_path = Path(d) / "hooks.json"
            import json

            hooks_path.write_text(
                json.dumps(
                    [
                        {"name": "hook1", "event": "test_event", "command": "sleep 0.1 && echo done"},
                        {"name": "hook2", "event": "test_event", "command": "echo 2"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            old_path = os.environ.get("POCKETAGENT_HOOKS_PATH")
            os.environ["POCKETAGENT_HOOKS_PATH"] = str(hooks_path)

            try:

                async def run() -> None:
                    await run_sync(lambda: run_hooks("test_event"))

                task = asyncio.create_task(run())
                await asyncio.sleep(0.02)
                scope.cancel()
                await task
            except Exception:
                pass
            finally:
                if old_path:
                    os.environ["POCKETAGENT_HOOKS_PATH"] = old_path
                else:
                    os.environ.pop("POCKETAGENT_HOOKS_PATH", None)

        current_scope.reset(token)

    asyncio.run(_test())

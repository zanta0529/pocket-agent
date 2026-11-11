"""檔案工具取消測試：確認寫入前檢查取消、不允許舊確認授權。"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from pocket_agent.core.cancellation import CancellationScope, current_scope, run_sync
from pocket_agent.tools.fs import _evaluate_fs_confirmation


@pytest.fixture
def temp_dir() -> Path:
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def test_write_cancelled_before_confirm(temp_dir: Path) -> None:
    """取消在確認前生效：不呼叫確認、不寫入。"""

    async def _test() -> None:
        scope = CancellationScope()
        token = current_scope.set(scope)
        target = temp_dir / "new.txt"

        called = False

        async def fake_confirm(*args: object) -> bool:
            nonlocal called
            called = True
            return True

        scope.cancel()
        try:
            await run_sync(lambda: _evaluate_fs_confirmation(fake_confirm, "建立新檔：new.txt", "寫入"))
        except Exception:
            pass

        assert not called
        assert not target.exists()
        current_scope.reset(token)

    asyncio.run(_test())


def test_write_cancelled_after_confirm_rejected(temp_dir: Path) -> None:
    """確認回傳 False（取消後）：不寫入、不備份。"""

    async def _test() -> None:
        scope = CancellationScope()
        token = current_scope.set(scope)
        target = temp_dir / "new.txt"

        def fake_confirm(*args: object) -> bool:
            return False

        try:
            await run_sync(lambda: _evaluate_fs_confirmation(fake_confirm, "建立新檔：new.txt", "寫入"))
        except Exception:
            pass

        assert not target.exists()
        current_scope.reset(token)

    asyncio.run(_test())


def test_confirm_bridge_rejects_stale_decision(temp_dir: Path) -> None:
    """確認是舊輪決策（取消已經生效）：拒絕授權、不寫入。"""

    async def _test() -> None:
        scope = CancellationScope()
        token = current_scope.set(scope)
        target = temp_dir / "new.txt"

        def fake_confirm(*args: object) -> bool:
            return True

        scope.cancel()
        try:
            await run_sync(lambda: _evaluate_fs_confirmation(fake_confirm, "建立新檔：new.txt", "寫入"))
        except Exception:
            pass

        assert not target.exists()
        current_scope.reset(token)

    asyncio.run(_test())


def test_replace_cancelled_before_backup(temp_dir: Path) -> None:
    """取消在備份前：不建立備份。"""

    async def _test() -> None:
        scope = CancellationScope()
        token = current_scope.set(scope)
        target = temp_dir / "existing.txt"
        target.write_text("original")

        async def fake_confirm(*args: object) -> bool:
            return True

        scope.cancel()
        try:
            await run_sync(lambda: _evaluate_fs_confirmation(fake_confirm, "修改檔案：existing.txt", "寫入"))
        except Exception:
            pass

        # 備份目錄不應有新檔
        from pocket_agent.system.paths import user_dir

        backup_dir = user_dir() / "backups"
        backups = list(backup_dir.glob("*existing.txt*"))
        assert len(backups) == 0
        current_scope.reset(token)

    asyncio.run(_test())


def test_append_cancelled_after_confirm_rejected(temp_dir: Path) -> None:
    """確認回傳 False 後不附加。"""

    async def _test() -> None:
        scope = CancellationScope()
        token = current_scope.set(scope)
        target = temp_dir / "existing.txt"
        target.write_text("original")

        def fake_confirm(*args: object) -> bool:
            return False

        try:
            await run_sync(lambda: _evaluate_fs_confirmation(fake_confirm, "附加內容至檔案：existing.txt", "寫入"))
        except Exception:
            pass

        assert target.read_text() == "original"
        current_scope.reset(token)

    asyncio.run(_test())

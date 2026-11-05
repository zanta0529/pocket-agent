"""作業系統與執行期相容性輔助模組 (Windows / macOS / Linux)。"""

import asyncio
import sys
import warnings


def setup_asyncio_policy() -> None:
    """初始化平台專屬的 asyncio 事件迴圈策略。

    Windows 下 uvicorn OAuth 回調伺服器與特定底層通訊需要 SelectorEventLoop。
    """
    if sys.platform == "win32":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            try:
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            except Exception:
                pass


def setup_win_sigint_workaround() -> asyncio.Task | None:
    """Windows 下 asyncio 在等待標準輸入時可能延遲處理 SIGINT (Ctrl+C)。

    透過低開銷的背景睡眠定時喚醒事件迴圈。
    """
    if sys.platform == "win32":

        async def _workaround():
            while True:
                await asyncio.sleep(0.5)

        return asyncio.create_task(_workaround())
    return None

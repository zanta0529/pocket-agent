"""跨平台人工驗證提示策略（Strategy Pattern：Windows / macOS / TTY 降級）。"""

import ctypes
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod

from mcp_servers._shared import t, tty_prompt_text


class UserPromptStrategy(ABC):
    """等待使用者完成瀏覽器人工操作之提示介面。"""

    @abstractmethod
    def prompt_continue(self, title: str, body: str) -> bool:
        """顯示提示並等待確認；回傳 True 表示繼續，False 表示中止。"""


class Win32MessageBoxStrategy(UserPromptStrategy):
    """Windows 原生置頂訊息框提示策略。"""

    _MB_OKCANCEL = 0x1
    _MB_ICONINFO = 0x40
    _MB_SETFOREGROUND = 0x10000
    _MB_TOPMOST = 0x40000
    _IDOK = 1

    def prompt_continue(self, title: str, body: str) -> bool:
        flags = self._MB_OKCANCEL | self._MB_ICONINFO | self._MB_SETFOREGROUND | self._MB_TOPMOST
        result = ctypes.windll.user32.MessageBoxW(0, body, title, flags)
        return result == self._IDOK


class MacOsDialogStrategy(UserPromptStrategy):
    """macOS osascript 原生對話框提示策略（透過 argv 傳參防範腳本注入）。"""

    _APPLESCRIPT = (
        "on run argv\n"
        "display dialog (item 2 of argv) with title (item 1 of argv) "
        'buttons {"Cancel", "OK"} default button "OK"\n'
        "end run"
    )

    def prompt_continue(self, title: str, body: str) -> bool:
        proc = subprocess.run(
            ["osascript", "-e", self._APPLESCRIPT, title, body],
            check=False,
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0


class TtyFallbackStrategy(UserPromptStrategy):
    """Linux 或無圖形視窗環境之底層 TTY 互動提示降級策略。"""

    def prompt_continue(self, title: str, body: str) -> bool:
        _ = title
        prompt_msg = t("browser_mcp.tty_wait_prompt", message=body)
        answer = tty_prompt_text(prompt_msg, default="y")
        return answer.strip().lower() in ("", "y", "yes", "ok", "1")


def get_user_prompt_strategy() -> UserPromptStrategy:
    """依目前作業系統環境挑選最適合的人工確認提示策略。"""
    if sys.platform == "win32" and hasattr(ctypes, "windll"):
        return Win32MessageBoxStrategy()
    if sys.platform == "darwin" and shutil.which("osascript"):
        return MacOsDialogStrategy()
    return TtyFallbackStrategy()

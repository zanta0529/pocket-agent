"""跨平台安全終端機互動詢問模組（絕不讀取 MCP stdin 協定管線）。"""

import getpass
import os
import sys
import warnings
from typing import Protocol

from mcp_servers._shared.bootstrap import t


class TtyPrompter(Protocol):
    """終端機互動詢問抽象介面（便於依賴注入與單元測試隔離）。"""

    def prompt(self, prompt_text: str, *, is_password: bool = False, hint: str = "") -> str:
        """向終端機詢問一般文字或隱藏密碼。"""
        ...


class SystemTtyPrompter:
    """使用作業系統終端機設備（CONIN$/CONOUT$ 或 /dev/tty）與 getpass 安全讀取輸入。"""

    def prompt(self, prompt_text: str, *, is_password: bool = False, hint: str = "") -> str:
        if is_password:
            return self.prompt_secret(prompt_text)
        return self.prompt_text(prompt_text, hint=hint)

    def prompt_secret(self, prompt_text: str) -> str:
        if os.name == "nt" and sys.stdin is not sys.__stdin__:
            raise ValueError(t("mcp_shared.tty_password_unavailable"))
        is_patched_getpass = getattr(getpass.getpass, "__module__", "") != "getpass"
        if not is_patched_getpass:
            stdin_tty = bool(sys.stdin is not None and getattr(sys.stdin, "isatty", lambda: False)())
            stderr_tty = bool(sys.stderr is not None and getattr(sys.stderr, "isatty", lambda: False)())
            if not (stdin_tty and stderr_tty):
                raise ValueError(t("mcp_shared.tty_password_unavailable"))
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                return getpass.getpass(prompt_text)
            except (getpass.GetPassWarning, EOFError, OSError) as exc:
                raise ValueError(t("mcp_shared.tty_password_read_failed")) from exc

    def prompt_text(self, prompt_text: str, *, hint: str = "") -> str:
        tty_in_name = "CONIN$" if os.name == "nt" else "/dev/tty"
        tty_out_name = "CONOUT$" if os.name == "nt" else "/dev/tty"
        try:
            with (
                open(tty_out_name, "w", encoding="utf-8") as tty_out,
                open(tty_in_name, encoding="utf-8") as tty_in,
            ):
                tty_out.write(prompt_text)
                tty_out.flush()
                return tty_in.readline().strip()
        except OSError as exc:
            raise ValueError(t("mcp_shared.tty_unavailable", hint=hint or "env")) from exc


DEFAULT_TTY_PROMPTER = SystemTtyPrompter()


def tty_prompt_text(prompt_text: str, *, hint: str = "") -> str:
    """透過預設系統終端機安全詢問非機密文字輸入（不含密碼）。"""
    return DEFAULT_TTY_PROMPTER.prompt_text(prompt_text, hint=hint)


def tty_prompt_secret(prompt_text: str) -> str:
    """透過預設系統終端機安全詢問隱藏密碼或 Token。"""
    return DEFAULT_TTY_PROMPTER.prompt_secret(prompt_text)


def tty_prompt(prompt_text: str, *, is_password: bool = False, hint: str = "") -> str:
    """透過預設系統終端機安全詢問使用者輸入。"""
    return DEFAULT_TTY_PROMPTER.prompt(prompt_text, is_password=is_password, hint=hint)

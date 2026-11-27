#!/usr/bin/env python3
"""Browser MCP 伺服器薄入口（純 Python，不需要 Node.js）。

用 Python Playwright 驅動電腦上已安裝的 Edge / Chrome，
不需要 `playwright install` 下載瀏覽器（適合離線或受限網路環境）。
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_servers._shared import CONSTANTS  # noqa: E402
from mcp_servers.browser import (  # noqa: E402
    browser_back,
    browser_click,
    browser_close,
    browser_close_tab,
    browser_fill,
    browser_get_text,
    browser_list_tabs,
    browser_navigate,
    browser_press,
    browser_screenshot,
    browser_scroll,
    browser_select_option,
    browser_session,
    browser_snapshot,
    browser_switch_tab,
    browser_wait,
    browser_wait_for_user,
    check_url_allowed,
    mcp,
)

MAX_TEXT = CONSTANTS.max_output_chars
MAX_OUTPUT_CHARS = CONSTANTS.max_output_chars

__all__ = [
    "MAX_OUTPUT_CHARS",
    "MAX_TEXT",
    "browser_back",
    "browser_click",
    "browser_close",
    "browser_close_tab",
    "browser_fill",
    "browser_get_text",
    "browser_list_tabs",
    "browser_navigate",
    "browser_press",
    "browser_screenshot",
    "browser_scroll",
    "browser_select_option",
    "browser_session",
    "browser_snapshot",
    "browser_switch_tab",
    "browser_wait",
    "browser_wait_for_user",
    "check_url_allowed",
    "mcp",
]

if __name__ == "__main__":
    mcp.run()

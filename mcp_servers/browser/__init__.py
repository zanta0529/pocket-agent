"""Playwright 瀏覽器自動化 MCP 子套件。"""

from mcp_servers.browser.security import check_url_allowed
from mcp_servers.browser.server import mcp
from mcp_servers.browser.session import browser_session
from mcp_servers.browser.tools import (
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
    browser_snapshot,
    browser_switch_tab,
    browser_wait,
    browser_wait_for_user,
)

__all__ = [
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

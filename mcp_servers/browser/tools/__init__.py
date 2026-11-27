"""瀏覽器自動化工具群組註冊清單。"""

from mcp_servers.browser.tools import actions, inspection, navigation, tabs
from mcp_servers.browser.tools.actions import (
    browser_click,
    browser_fill,
    browser_press,
    browser_scroll,
    browser_select_option,
    browser_wait_for_user,
)
from mcp_servers.browser.tools.inspection import browser_get_text, browser_screenshot, browser_snapshot
from mcp_servers.browser.tools.navigation import browser_back, browser_navigate, browser_wait
from mcp_servers.browser.tools.tabs import browser_close, browser_close_tab, browser_list_tabs, browser_switch_tab

REGISTRARS = (
    navigation.register,
    inspection.register,
    actions.register,
    tabs.register,
)

__all__ = [
    "REGISTRARS",
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
    "browser_snapshot",
    "browser_switch_tab",
    "browser_wait",
    "browser_wait_for_user",
]

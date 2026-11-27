"""瀏覽器分頁管理與關閉工具群組（Command / Registrar Pattern）。"""

from typing import Any

from mcp_servers._shared import t
from mcp_servers.browser.security import AUDIT
from mcp_servers.browser.session import browser_session
from mcp_servers.browser.tools._common import format_tool_error


async def browser_list_tabs() -> str:
    """列出目前瀏覽器所有開啟中的分頁編號、標題與網址。"""
    try:
        tabs = await browser_session.list_tabs()
        if not tabs:
            return t("browser_mcp.tabs_empty")
        lines = [t("browser_mcp.tabs_header", count=len(tabs))]
        for tab in tabs:
            marker = "*" if tab["active"] else " "
            title = tab["title"] or "(untitled)"
            url = tab["url"] or "about:blank"
            lines.append(f"{marker}[{tab['index']}] {title} — {url}")
        AUDIT.log("browser_list_tabs", f"count={len(tabs)}", status="OK")
        return "\n".join(lines)
    except Exception as exc:
        return format_tool_error("browser_list_tabs", exc)


async def browser_switch_tab(index: int) -> str:
    """切換至指定編號（1-based，由 browser_list_tabs 取得）的瀏覽器分頁。"""
    try:
        target = await browser_session.switch_tab(index)
        state = await browser_session.page_state(target, tool_name="browser_switch_tab")
        AUDIT.log("browser_switch_tab", f"index={index}", target=str(getattr(target, "url", "")), status="OK")
        return t("browser_mcp.tab_switched", index=index, state=state)
    except Exception as exc:
        return format_tool_error("browser_switch_tab", exc)


async def browser_close_tab(index: int = 0) -> str:
    """關閉指定編號的分頁（index=0 代表關閉目前作用中分頁）。"""
    try:
        closed_idx, next_page = await browser_session.close_tab(index)
        AUDIT.log("browser_close_tab", f"index={closed_idx}", status="OK")
        if next_page is None:
            return t("browser_mcp.tab_closed_none_left", index=closed_idx)
        state = await browser_session.page_state(next_page, tool_name="browser_close_tab")
        return t("browser_mcp.tab_closed", index=closed_idx, state=state)
    except Exception as exc:
        return format_tool_error("browser_close_tab", exc)


async def browser_close() -> str:
    """關閉瀏覽器並釋放所有執行期資源。"""
    try:
        closed = await browser_session.close()
        AUDIT.log("browser_close", f"closed={closed}", status="OK")
        return t("browser_mcp.closed") if closed else t("browser_mcp.already_closed")
    except Exception as exc:
        return format_tool_error("browser_close", exc)


def register(mcp: Any) -> None:
    """註冊瀏覽器分頁管理與關閉工具至 MCPServer。"""
    for fn in (browser_list_tabs, browser_switch_tab, browser_close_tab, browser_close):
        mcp.tool()(fn)

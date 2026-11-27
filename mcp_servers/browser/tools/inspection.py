"""瀏覽器頁面檢視與截圖工具群組（Command / Registrar Pattern）。"""

import time
from typing import Any

from mcp_servers._shared import CONSTANTS, t
from mcp_servers.browser.security import AUDIT, redact_and_truncate
from mcp_servers.browser.session import browser_session, resolve_screenshot_dir
from mcp_servers.browser.snapshot import DEFAULT_SNAPSHOT_JS
from mcp_servers.browser.tools._common import format_tool_error


async def browser_snapshot(selector: str = "") -> str:
    """列出頁面上所有可互動元素（連結、按鈕、輸入框），每個有 [ref] 編號。
    用編號呼叫 browser_click / browser_fill / browser_select_option 來操作；
    可選傳入 CSS selector 聚焦特定區域。"""
    clean_selector = (selector or "").strip()
    try:
        page = await browser_session.ensure_allowed_page("browser_snapshot")
        state = await browser_session.page_state(page, tool_name="browser_snapshot")
        raw_result = await page.evaluate(DEFAULT_SNAPSHOT_JS, clean_selector or None)

        if isinstance(raw_result, dict):
            if not raw_result.get("found", True):
                return f"[ERROR] {t('browser_mcp.selector_not_found', selector=clean_selector)}"
            elements = str(raw_result.get("text") or "")
        else:
            elements = str(raw_result or "")

        if not elements:
            AUDIT.log(
                "browser_snapshot", clean_selector or "document", target=str(getattr(page, "url", "")), status="OK"
            )
            return f"{state}\n\n{t('browser_mcp.no_interactive_elements')}"

        safe_elements, truncated = redact_and_truncate(
            elements,
            max_chars=CONSTANTS.max_output_chars,
            hint=clean_selector,
        )
        if truncated and len(elements) > CONSTANTS.max_output_chars:
            # 保留原有元素截斷統計提示，但以安全裁切長度為準
            safe_elements = safe_elements.split("\n\n", 1)[0] + t(
                "browser_mcp.elements_truncated",
                max_chars=CONSTANTS.max_output_chars,
                total=len(elements),
            )
        AUDIT.log("browser_snapshot", clean_selector or "document", target=str(getattr(page, "url", "")), status="OK")
        return t("browser_mcp.interactive_elements", state=state, elements=safe_elements)
    except Exception as exc:
        return format_tool_error("browser_snapshot", exc)


async def browser_get_text() -> str:
    """取得頁面的可見文字內容（閱讀網頁用，自動遮罩機密並套用字元上限）。"""
    try:
        page = await browser_session.ensure_allowed_page("browser_get_text")
        state = await browser_session.page_state(page, tool_name="browser_get_text")
        raw_text = str(await page.evaluate("() => document.body ? document.body.innerText : ''") or "").strip()

        safe_text, truncated = redact_and_truncate(raw_text, max_chars=CONSTANTS.max_output_chars)
        if truncated and len(raw_text) > CONSTANTS.max_output_chars:
            safe_text = safe_text.split("\n\n", 1)[0] + t(
                "browser_mcp.text_truncated",
                max_chars=CONSTANTS.max_output_chars,
                total=len(raw_text),
            )
        AUDIT.log("browser_get_text", "innerText", target=str(getattr(page, "url", "")), status="OK")
        return f"{state}\n\n{safe_text}"
    except Exception as exc:
        return format_tool_error("browser_get_text", exc)


async def browser_screenshot(full_page: bool = False) -> str:
    """擷取目前瀏覽器頁面 PNG 截圖並儲存於 ~/.pocket-agent/artifacts/browser_shots/。"""
    try:
        page = await browser_session.ensure_allowed_page("browser_screenshot")
        state = await browser_session.page_state(page, tool_name="browser_screenshot")
        shots_dir = resolve_screenshot_dir()
        shots_dir.mkdir(parents=True, exist_ok=True)
        out_path = shots_dir / f"browser_{int(time.time() * 1000)}.png"
        await page.screenshot(path=str(out_path), full_page=bool(full_page))

        mode_label = t("browser_mcp.screenshot_mode_full") if full_page else t("browser_mcp.screenshot_mode_viewport")
        AUDIT.log(
            "browser_screenshot",
            f"full_page={bool(full_page)}",
            target=str(getattr(page, "url", "")),
            status="OK",
            detail=str(out_path),
        )
        return t("browser_mcp.screenshot_saved", path=str(out_path), mode=mode_label, state=state)
    except Exception as exc:
        return format_tool_error("browser_screenshot", exc)


def register(mcp: Any) -> None:
    """註冊瀏覽器頁面檢視與截圖工具至 MCPServer。"""
    for fn in (browser_snapshot, browser_get_text, browser_screenshot):
        mcp.tool()(fn)

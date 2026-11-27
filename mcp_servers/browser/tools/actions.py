"""瀏覽器頁面互動與人工介入工具群組（Command / Registrar Pattern）。"""

import asyncio
from typing import Any

from mcp_servers._shared import CONSTANTS, t
from mcp_servers.browser.prompt import get_user_prompt_strategy
from mcp_servers.browser.security import (
    AUDIT,
    BROWSER_REDACTION,
    is_sensitive_field,
    load_browser_config,
)
from mcp_servers.browser.session import browser_session
from mcp_servers.browser.tools._common import format_tool_error, require_ref_locator


async def browser_click(ref: int) -> str:
    """點擊指定編號的元素（編號來自 browser_snapshot）。"""
    try:
        page = await browser_session.ensure_allowed_page("browser_click")
        before_count = len(await browser_session.open_pages())
        locator = await require_ref_locator(page, ref)
        await locator.click()
        await page.wait_for_load_state("domcontentloaded")
        state = await browser_session.page_state(page, tool_name="browser_click")
        after_count = len(await browser_session.open_pages())
        AUDIT.log("browser_click", f"ref={ref}", target=str(getattr(page, "url", "")), status="OK")
        msg = t("browser_mcp.clicked", ref=ref, state=state)
        if after_count > before_count:
            msg += t("browser_mcp.new_tab_opened_hint", count=after_count)
        return msg
    except Exception as exc:
        return format_tool_error("browser_click", exc)


async def browser_fill(ref: int, text: str) -> str:
    """在指定編號的輸入框填入文字（會先清空；密碼與敏感欄位自動遮罩）。"""
    try:
        page = await browser_session.ensure_allowed_page("browser_fill")
        locator = await require_ref_locator(page, ref)

        is_sensitive = False
        try:
            el_type = (await locator.get_attribute("type") or "").lower()
            el_name = await locator.get_attribute("name") or ""
            el_id = await locator.get_attribute("id") or ""
            el_auto = await locator.get_attribute("autocomplete") or ""
            is_sensitive = is_sensitive_field(el_type, el_name, el_id, el_auto)
        except Exception:
            # 讀取欄位屬性失敗時防禦性視為敏感欄位進行遮罩，杜絕 Fail-Open 明文洩漏
            is_sensitive = True

        await locator.fill(text)
        display_text = "********" if is_sensitive else BROWSER_REDACTION.redact(text)
        AUDIT.log(
            "browser_fill",
            f"ref={ref} len={len(text)} masked={is_sensitive}",
            target=str(getattr(page, "url", "")),
            status="OK",
        )
        return t("browser_mcp.filled", ref=ref, text=display_text)
    except Exception as exc:
        return format_tool_error("browser_fill", exc)


async def browser_select_option(ref: int, value: str = "", label: str = "") -> str:
    """在指定編號的 <select> 下拉選單依 value 或 label 選擇項目。"""
    clean_val = (value or "").strip()
    clean_label = (label or "").strip()
    if not clean_val and not clean_label:
        return f"[ERROR] {t('browser_mcp.select_missing_option')}"

    try:
        page = await browser_session.ensure_allowed_page("browser_select_option")
        locator = await require_ref_locator(page, ref)
        if clean_val:
            selected = await locator.select_option(value=clean_val)
            chosen_desc = clean_val
        else:
            selected = await locator.select_option(label=clean_label)
            chosen_desc = clean_label

        if isinstance(selected, list) and selected:
            chosen_desc = ", ".join(str(item) for item in selected)
        state = await browser_session.page_state(page, tool_name="browser_select_option")
        AUDIT.log(
            "browser_select_option",
            f"ref={ref} selected={chosen_desc}",
            target=str(getattr(page, "url", "")),
            status="OK",
        )
        return t("browser_mcp.selected_option", ref=ref, selected=BROWSER_REDACTION.redact(chosen_desc), state=state)
    except Exception as exc:
        return format_tool_error("browser_select_option", exc)


async def browser_press(key: str) -> str:
    """按下鍵盤按鍵，例如 Enter、Tab、Escape、ArrowDown。"""
    clean_key = (key or "").strip()
    if not clean_key:
        return f"[ERROR] {t('browser_mcp.invalid_key')}"
    try:
        page = await browser_session.ensure_allowed_page("browser_press")
        await page.keyboard.press(clean_key)
        await page.wait_for_load_state("domcontentloaded")
        state = await browser_session.page_state(page, tool_name="browser_press")
        AUDIT.log("browser_press", clean_key, target=str(getattr(page, "url", "")), status="OK")
        return t("browser_mcp.pressed", key=clean_key, state=state)
    except Exception as exc:
        return format_tool_error("browser_press", exc)


async def browser_scroll(direction: str = "down", amount: int = CONSTANTS.browser_scroll_default_px) -> str:
    """捲動頁面，direction 為 up 或 down，amount 為捲動像素（100~3000）。"""
    clean_dir = (direction or "").strip().lower()
    if clean_dir not in ("up", "down"):
        return f"[ERROR] {t('browser_mcp.invalid_direction', direction=direction)}"
    try:
        px = int(amount)
    except TypeError, ValueError:
        px = CONSTANTS.browser_scroll_default_px
    clamped_px = max(CONSTANTS.browser_scroll_min_px, min(px, CONSTANTS.browser_scroll_max_px))
    delta = -clamped_px if clean_dir == "up" else clamped_px

    try:
        page = await browser_session.ensure_allowed_page("browser_scroll")
        await page.mouse.wheel(0, delta)
        AUDIT.log("browser_scroll", f"{clean_dir}:{clamped_px}", target=str(getattr(page, "url", "")), status="OK")
        return t("browser_mcp.scrolled_up") if delta < 0 else t("browser_mcp.scrolled_down")
    except Exception as exc:
        return format_tool_error("browser_scroll", exc)


async def browser_wait_for_user(message: str = "") -> str:
    """遇到 CAPTCHA 驗證、登入頁、簡訊／兩步驟驗證等需要使用者親自操作的畫面時，
    必須呼叫此工具，不要嘗試自己完成。會暫停並提示使用者完成操作，
    確認後才回傳，接著用 browser_snapshot 確認新狀態。"""
    cfg = load_browser_config()
    if cfg.headless:
        return t("browser_mcp.headless_wait_error")

    clean_message = (message or "").strip() or t("browser_mcp.wait_default_message")
    try:
        page = await browser_session.ensure_allowed_page("browser_wait_for_user")
        bring_to_front = getattr(page, "bring_to_front", None)
        if callable(bring_to_front):
            try:
                await bring_to_front()
            except Exception:
                pass

        strategy = get_user_prompt_strategy()
        title = t("browser_mcp.wait_dialog_title")
        body = t("browser_mcp.wait_dialog_body", message=clean_message)
        continued = await asyncio.to_thread(strategy.prompt_continue, title, body)
        AUDIT.log(
            "browser_wait_for_user",
            f"continued={continued}",
            target=str(getattr(page, "url", "")),
            status="OK" if continued else "CANCELLED",
        )
        return t("browser_mcp.wait_completed") if continued else t("browser_mcp.wait_cancelled")
    except Exception as exc:
        return format_tool_error("browser_wait_for_user", exc)


def register(mcp: Any) -> None:
    """註冊瀏覽器互動與人工介入工具至 MCPServer。"""
    for fn in (
        browser_click,
        browser_fill,
        browser_select_option,
        browser_press,
        browser_scroll,
        browser_wait_for_user,
    ):
        mcp.tool()(fn)

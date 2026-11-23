#!/usr/bin/env python3
"""Browser MCP server（純 Python，不需要 Node.js）

用 Python playwright 驅動電腦上已安裝的 Edge / Chrome，
不需要 `playwright install` 下載瀏覽器（適合離線或受限網路環境）。

掛載方式（mcp_config.yaml）：
    servers:
      browser:
        transport: stdio
        command: python
        args: ["mcp_servers/browser_mcp.py"]

環境變數（可選）：
    BROWSER_HEADLESS=1        無頭模式（預設開視窗）
    BROWSER_CDP_URL=http://localhost:9222
        連接既有的瀏覽器（保留登入狀態）。先手動啟動：
        msedge.exe --remote-debugging-port=9222
"""

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_servers._shared import CONSTANTS, create_mcp_server, t  # noqa: E402

try:
    from playwright.async_api import async_playwright  # pyright: ignore[reportMissingImports]
except ImportError:
    async_playwright = None  # type: ignore

mcp = create_mcp_server("browser")

_pw = None
_browser = None
_page = None

MAX_TEXT = CONSTANTS.max_output_chars

# 可互動元素的標記 JS：加上 data-mcp-ref 屬性並回傳清單
_SNAPSHOT_JS = """
() => {
  const sels = 'a[href], button, input, textarea, select, summary, ' +
    '[role="button"], [role="link"], [role="tab"], [role="checkbox"], ' +
    '[role="menuitem"], [role="combobox"], [role="option"], [contenteditable="true"]';
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll(sels)) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    i += 1;
    el.setAttribute('data-mcp-ref', String(i));
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    let rawLabel = '';
    if (tag === 'input' && type === 'password') {
      rawLabel = '[PROTECTED PASSWORD FIELD]';
    } else {
      rawLabel = (el.innerText || el.value || el.placeholder ||
        el.getAttribute('aria-label') || el.title || '');
    }
    const label = rawLabel
      .trim().replace(/\\s+/g, ' ').slice(0, 80);
    out.push(`[${i}] <${tag}${type ? ' type=' + type : ''}> ${label}`);
  }
  return out.join('\\n');
}
"""


async def _ensure_page():
    """啟動或重用瀏覽器頁面。優先用系統 Edge，其次 Chrome。"""
    global _pw, _browser, _page
    if _page is not None and not _page.is_closed():
        return _page

    if async_playwright is None:
        raise RuntimeError(t("browser_mcp.playwright_missing"))

    if _pw is None:
        _pw = await async_playwright().start()

    cdp = os.getenv("BROWSER_CDP_URL")
    if cdp:
        _browser = await _pw.chromium.connect_over_cdp(cdp)
        ctx = _browser.contexts[0] if _browser.contexts else await _browser.new_context()
        _page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    else:
        headless = os.getenv("BROWSER_HEADLESS", "") in ("1", "true", "yes")
        last_err = None
        for channel in ("msedge", "chrome", None):
            try:
                _browser = await _pw.chromium.launch(headless=headless, channel=channel)
                break
            except Exception as e:
                last_err = e
        else:
            raise RuntimeError(t("browser_mcp.browser_not_found", error=last_err))
        _page = await _browser.new_page()

    _page.set_default_timeout(15000)
    allowed = os.getenv("BROWSER_ALLOWED_DOMAINS")
    if allowed:

        async def _intercept_request(route):
            req_url = route.request.url
            if req_url.startswith(("data:", "about:", "javascript:")):
                await route.continue_()
                return
            try:
                check_url_allowed(req_url)
                await route.continue_()
            except ValueError:
                await route.abort("blockedbyclient")

        # 於 Context 層級安裝攔截器，使所有現存頁面、新開分頁與 Popup 均受網域白名單約束
        ctx = _page.context
        if not getattr(ctx, "_pocket_route_installed", False):
            await ctx.route("**/*", _intercept_request)
            ctx._pocket_route_installed = True
            ctx.on("page", lambda p: p.set_default_timeout(15000))
    return _page


def _ref_locator(page, ref: int):
    return page.locator(f'[data-mcp-ref="{ref}"]')


async def _page_state(page) -> str:
    curr_url = page.url
    if curr_url and curr_url != "about:blank":
        try:
            check_url_allowed(curr_url)
        except ValueError:
            await page.goto("about:blank")
            raise
    return t("browser_mcp.page_state", title=await page.title(), url=page.url)


def check_url_allowed(url: str):
    allowed = os.getenv("BROWSER_ALLOWED_DOMAINS")
    if not allowed:
        return
    import urllib.parse

    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password:
        raise ValueError(t("browser_mcp.userinfo_blocked"))

    hostname = (parsed.hostname or "").lower().strip()
    if not hostname:
        raise ValueError(t("browser_mcp.domain_parse_failed", url=url))

    allowed_list = [d.strip().lower() for d in allowed.split(",") if d.strip()]
    matched = False
    for allowed_domain in allowed_list:
        if hostname == allowed_domain or hostname.endswith("." + allowed_domain):
            matched = True
            break
    if not matched:
        raise ValueError(t("browser_mcp.domain_not_allowed", host=hostname))


@mcp.tool()
async def browser_navigate(url: str) -> str:
    """開啟指定網址。之後用 browser_snapshot 查看頁面內容。"""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    check_url_allowed(url)
    page = await _ensure_page()
    await page.goto(url, wait_until="domcontentloaded")
    return await _page_state(page)


@mcp.tool()
async def browser_snapshot() -> str:
    """列出頁面上所有可互動元素（連結、按鈕、輸入框），每個有 [ref] 編號。
    用編號呼叫 browser_click / browser_fill 來操作。"""
    page = await _ensure_page()
    elements = await page.evaluate(_SNAPSHOT_JS)
    state = await _page_state(page)
    if not elements:
        return f"{state}\n\n{t('browser_mcp.no_interactive_elements')}"
    total_el_len = len(elements)
    if total_el_len > MAX_TEXT:
        elements = elements[:MAX_TEXT] + t(
            "browser_mcp.elements_truncated",
            max_chars=MAX_TEXT,
            total=total_el_len,
        )
    return t("browser_mcp.interactive_elements", state=state, elements=elements)


@mcp.tool()
async def browser_get_text() -> str:
    """取得頁面的可見文字內容（閱讀網頁用）。"""
    page = await _ensure_page()
    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    text = text.strip()
    total_text_len = len(text)
    if total_text_len > MAX_TEXT:
        text = text[:MAX_TEXT] + t(
            "browser_mcp.text_truncated",
            max_chars=MAX_TEXT,
            total=total_text_len,
        )
    return f"{await _page_state(page)}\n\n{text}"


@mcp.tool()
async def browser_click(ref: int) -> str:
    """點擊指定編號的元素（編號來自 browser_snapshot）。"""
    page = await _ensure_page()
    await _ref_locator(page, ref).first.click()
    await page.wait_for_load_state("domcontentloaded")
    return t("browser_mcp.clicked", ref=ref, state=await _page_state(page))


@mcp.tool()
async def browser_fill(ref: int, text: str) -> str:
    """在指定編號的輸入框填入文字（會先清空）。"""
    page = await _ensure_page()
    locator = _ref_locator(page, ref).first
    is_password = False
    try:
        el_type = (await locator.get_attribute("type") or "").lower()
        is_password = el_type == "password"
    except Exception:
        # 讀取型別失敗時防禦性視為密碼進行遮罩，杜絕 Fail-Open 明文洩漏
        is_password = True
    await locator.fill(text)
    display_text = "********" if is_password else text
    return t("browser_mcp.filled", ref=ref, text=display_text)


@mcp.tool()
async def browser_press(key: str) -> str:
    """按下鍵盤按鍵，例如 Enter、Tab、Escape、ArrowDown。"""
    page = await _ensure_page()
    await page.keyboard.press(key)
    await page.wait_for_load_state("domcontentloaded")
    return t("browser_mcp.pressed", key=key, state=await _page_state(page))


@mcp.tool()
async def browser_scroll(direction: str = "down") -> str:
    """捲動頁面，direction 為 up 或 down。"""
    page = await _ensure_page()
    delta = -600 if direction == "up" else 600
    await page.mouse.wheel(0, delta)
    return t("browser_mcp.scrolled_up") if delta < 0 else t("browser_mcp.scrolled_down")


@mcp.tool()
async def browser_back() -> str:
    """回到上一頁。"""
    page = await _ensure_page()
    await page.go_back(wait_until="domcontentloaded")
    return await _page_state(page)


@mcp.tool()
async def browser_wait_for_user(message: str = "請在瀏覽器視窗完成操作") -> str:
    """遇到 CAPTCHA 驗證、登入頁、簡訊/兩步驟驗證等需要使用者親自操作的畫面時，
    必須呼叫此工具，不要嘗試自己完成。會跳出提示視窗並暫停等待，
    使用者在瀏覽器完成操作、按下「確定」後才回傳，接著用 browser_snapshot 確認新狀態。
    message：告訴使用者需要做什麼，例如「請完成 CAPTCHA 驗證」。"""
    import asyncio
    import ctypes

    if os.getenv("BROWSER_HEADLESS", "") in ("1", "true", "yes"):
        return t("browser_mcp.headless_wait_error")

    page = await _ensure_page()
    try:
        await page.bring_to_front()
    except Exception:
        pass

    MB_OKCANCEL, MB_ICONINFO = 0x1, 0x40
    MB_SETFOREGROUND, MB_TOPMOST = 0x10000, 0x40000

    def show_dialog() -> int:
        return ctypes.windll.user32.MessageBoxW(
            0,
            t("browser_mcp.wait_dialog_body", message=message),
            t("browser_mcp.wait_dialog_title"),
            MB_OKCANCEL | MB_ICONINFO | MB_SETFOREGROUND | MB_TOPMOST,
        )

    result = await asyncio.to_thread(show_dialog)

    if result == 1:  # IDOK
        return t("browser_mcp.wait_completed")
    return t("browser_mcp.wait_cancelled")


@mcp.tool()
async def browser_close() -> str:
    """關閉瀏覽器。"""
    global _pw, _browser, _page
    if _browser is not None:
        await _browser.close()
        _browser = _page = None
    if _pw is not None:
        await _pw.stop()
        _pw = None
        return t("browser_mcp.closed")
    return t("browser_mcp.already_closed")


if __name__ == "__main__":
    mcp.run()

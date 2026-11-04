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

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

try:
    from playwright.async_api import async_playwright  # pyright: ignore[reportMissingImports]
except ImportError:
    async_playwright = None  # type: ignore

mcp = MCPServer("browser")

_pw = None
_browser = None
_page = None

try:
    from pocket_agent.system.constants import DEFAULT_TOOL_OUTPUT_CHARS

    MAX_TEXT = DEFAULT_TOOL_OUTPUT_CHARS
except ImportError:
    MAX_TEXT = 12000

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
        raise RuntimeError("尚未安裝 playwright 套件。請在終端機執行：pip install playwright")

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
            raise RuntimeError(f"找不到可用的瀏覽器（Edge/Chrome）：{last_err}")
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
    return f"目前頁面：{await page.title()}\nURL：{page.url}"


def check_url_allowed(url: str):
    allowed = os.getenv("BROWSER_ALLOWED_DOMAINS")
    if not allowed:
        return
    import urllib.parse

    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password:
        raise ValueError("拒絕存取：URL 不得包含使用者資訊 (Userinfo) 以防範混淆攻擊。")

    hostname = (parsed.hostname or "").lower().strip()
    if not hostname:
        raise ValueError(f"拒絕存取：無法自 URL 解析有效網域：{url}")

    allowed_list = [d.strip().lower() for d in allowed.split(",") if d.strip()]
    matched = False
    for allowed_domain in allowed_list:
        if hostname == allowed_domain or hostname.endswith("." + allowed_domain):
            matched = True
            break
    if not matched:
        raise ValueError(f"拒絕存取：網域 {hostname} 不在 BROWSER_ALLOWED_DOMAINS 白名單內")


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
        return f"{state}\n\n（頁面上沒有可互動元素）"
    total_el_len = len(elements)
    if total_el_len > MAX_TEXT:
        elements = (
            elements[:MAX_TEXT]
            + f"\n\n... (元素過多，已達單次輸出上限 {MAX_TEXT} 字元，原始長度共 {total_el_len:,} 字元)"
        )
    return f"{state}\n\n可互動元素：\n{elements}"


@mcp.tool()
async def browser_get_text() -> str:
    """取得頁面的可見文字內容（閱讀網頁用）。"""
    page = await _ensure_page()
    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    text = text.strip()
    total_text_len = len(text)
    if total_text_len > MAX_TEXT:
        text = (
            text[:MAX_TEXT]
            + f"\n\n... (內容過長，已達單次輸出上限 {MAX_TEXT} 字元，原始文字共 {total_text_len:,} 字元)"
        )
    return f"{await _page_state(page)}\n\n{text}"


@mcp.tool()
async def browser_click(ref: int) -> str:
    """點擊指定編號的元素（編號來自 browser_snapshot）。"""
    page = await _ensure_page()
    await _ref_locator(page, ref).first.click()
    await page.wait_for_load_state("domcontentloaded")
    return f"已點擊 [{ref}]\n{await _page_state(page)}"


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
    return f"已在 [{ref}] 填入：{display_text}"


@mcp.tool()
async def browser_press(key: str) -> str:
    """按下鍵盤按鍵，例如 Enter、Tab、Escape、ArrowDown。"""
    page = await _ensure_page()
    await page.keyboard.press(key)
    await page.wait_for_load_state("domcontentloaded")
    return f"已按下 {key}\n{await _page_state(page)}"


@mcp.tool()
async def browser_scroll(direction: str = "down") -> str:
    """捲動頁面，direction 為 up 或 down。"""
    page = await _ensure_page()
    delta = -600 if direction == "up" else 600
    await page.mouse.wheel(0, delta)
    return f"已向{'上' if delta < 0 else '下'}捲動"


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
        return (
            "[ERROR] 目前是無頭模式（BROWSER_HEADLESS=1），使用者看不到瀏覽器視窗，"
            "無法人工操作。請告知使用者關閉無頭模式後重試。"
        )

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
            f"{message}\n\n完成後按「確定」讓 AI 繼續；按「取消」中止任務。",
            "Pocket Agent — 需要你操作瀏覽器",
            MB_OKCANCEL | MB_ICONINFO | MB_SETFOREGROUND | MB_TOPMOST,
        )

    result = await asyncio.to_thread(show_dialog)

    if result == 1:  # IDOK
        return "使用者已完成操作。請用 browser_snapshot 重新確認目前頁面狀態，再從該狀態繼續任務。"
    return "使用者取消了操作。請中止目前任務，改為詢問使用者下一步怎麼做。"


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
        return "瀏覽器已關閉"
    return "瀏覽器本來就沒開"


if __name__ == "__main__":
    mcp.run()

"""瀏覽器導覽與等待工具群組（Command / Registrar Pattern）。"""

import asyncio
from typing import Any

from mcp_servers._shared import CONSTANTS, t
from mcp_servers.browser.security import AUDIT, BROWSER_REDACTION
from mcp_servers.browser.session import browser_session
from mcp_servers.browser.tools._common import format_tool_error

_VALID_WAIT_STATES = frozenset({"load", "domcontentloaded", "networkidle"})


async def browser_navigate(url: str) -> str:
    """開啟指定網址。之後用 browser_snapshot 查看頁面內容。"""
    try:
        normalized_url = browser_session.policy.normalize_navigation_url(url)
        browser_session.policy.check_url_allowed(normalized_url)
    except ValueError as exc:
        return format_tool_error("browser_navigate", exc, target=str(url), blocked=True)

    try:
        page = await browser_session.ensure_page()
        await page.goto(normalized_url, wait_until="domcontentloaded")
        state = await browser_session.page_state(page, tool_name="browser_navigate")
        AUDIT.log("browser_navigate", normalized_url, target=normalized_url, status="OK")
        return state
    except Exception as exc:
        return format_tool_error("browser_navigate", exc, target=normalized_url)


async def browser_back() -> str:
    """回到上一頁。"""
    try:
        page = await browser_session.ensure_allowed_page("browser_back")
        await page.go_back(wait_until="domcontentloaded")
        state = await browser_session.page_state(page, tool_name="browser_back")
        AUDIT.log("browser_back", "go_back", target=str(getattr(page, "url", "")), status="OK")
        return state
    except Exception as exc:
        return format_tool_error("browser_back", exc)


async def browser_wait(
    text: str = "",
    url_contains: str = "",
    state: str = "",
    timeout_sec: float = CONSTANTS.browser_wait_default_timeout_sec,
) -> str:
    """等待頁面出現指定文字、網址片段或達到指定載入狀態（適用於非同步載入與 SPA）。"""
    clean_text = (text or "").strip()
    clean_url = (url_contains or "").strip()
    clean_state = (state or "").strip().lower()

    if not clean_text and not clean_url and not clean_state:
        return f"[ERROR] {t('browser_mcp.wait_missing_condition')}"
    if clean_state and clean_state not in _VALID_WAIT_STATES:
        return f"[ERROR] {t('browser_mcp.wait_invalid_state', state=clean_state)}"

    try:
        raw_timeout = float(timeout_sec)
    except TypeError, ValueError:
        raw_timeout = CONSTANTS.browser_wait_default_timeout_sec
    clamped_sec = max(
        CONSTANTS.browser_wait_min_timeout_sec,
        min(raw_timeout, CONSTANTS.browser_wait_max_timeout_sec),
    )
    timeout_ms = int(clamped_sec * 1000)

    try:
        page = await browser_session.ensure_allowed_page("browser_wait")
        if clean_state:
            await page.wait_for_load_state(clean_state, timeout=timeout_ms)
        if clean_url:
            await _wait_for_url_contains(page, clean_url, clamped_sec)
        if clean_text:
            await _wait_for_text(page, clean_text, timeout_ms, clamped_sec)

        page_summary = await browser_session.page_state(page, tool_name="browser_wait")
        AUDIT.log(
            "browser_wait",
            f"state={clean_state} url={clean_url}",
            target=str(getattr(page, "url", "")),
            status="OK",
        )
        return t("browser_mcp.wait_succeeded", state=page_summary)
    except TimeoutError:
        return f"[ERROR] {t('browser_mcp.wait_timeout', timeout=clamped_sec)}"
    except Exception as exc:
        if "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower():
            return f"[ERROR] {t('browser_mcp.wait_timeout', timeout=clamped_sec)}"
        return format_tool_error("browser_wait", exc)


async def _wait_for_url_contains(page: Any, fragment: str, timeout_sec: float) -> None:
    wait_for_url = getattr(page, "wait_for_url", None)
    if callable(wait_for_url):
        await wait_for_url(lambda u: fragment in str(u), timeout=int(timeout_sec * 1000))
        return
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while asyncio.get_running_loop().time() < deadline:
        if fragment in str(getattr(page, "url", "")):
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(BROWSER_REDACTION.redact(fragment))


async def _wait_for_text(page: Any, text: str, timeout_ms: int, timeout_sec: float) -> None:
    get_by_text = getattr(page, "get_by_text", None)
    if callable(get_by_text):
        locator = get_by_text(text).first
        wait_for = getattr(locator, "wait_for", None)
        if callable(wait_for):
            await wait_for(state="visible", timeout=timeout_ms)
            return
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while asyncio.get_running_loop().time() < deadline:
        body_text = str(await page.evaluate("() => document.body ? document.body.innerText : ''"))
        if text in body_text:
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(BROWSER_REDACTION.redact(text))


def register(mcp: Any) -> None:
    """註冊瀏覽器導覽與等待工具至 MCPServer。"""
    for fn in (browser_navigate, browser_back, browser_wait):
        mcp.tool()(fn)

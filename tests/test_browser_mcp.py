"""Browser MCP 伺服器單元與安全回歸測試（完全離線，使用 Fake Playwright 隔離真實瀏覽器）。"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from mcp_servers import browser_mcp
from mcp_servers.browser import prompt as browser_prompt
from mcp_servers.browser import security as browser_security
from mcp_servers.browser import session as browser_session_mod
from mcp_servers.browser import snapshot as browser_snapshot_mod


class _FakeLocator:
    def __init__(self, page: "_FakePage", selector: str, *, count: int = 1, attrs: dict[str, str] | None = None):
        self._page = page
        self._selector = selector
        self._count = count
        self._attrs = attrs or {}
        self.clicked = False
        self.filled_with: str | None = None
        self.selected_args: dict[str, Any] | None = None

    @property
    def first(self) -> "_FakeLocator":
        return self

    async def count(self) -> int:
        return self._count

    async def click(self) -> None:
        self.clicked = True
        self._page.actions.append(("click", self._selector))

    async def fill(self, text: str) -> None:
        self.filled_with = text
        self._page.actions.append(("fill", self._selector, text))

    async def select_option(self, **kwargs: Any) -> list[str]:
        self.selected_args = kwargs
        self._page.actions.append(("select_option", self._selector, kwargs))
        return [str(kwargs.get("value") or kwargs.get("label") or "")]

    async def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)

    async def wait_for(self, **kwargs: Any) -> None:
        self._page.actions.append(("wait_for", self._selector, kwargs))


class _FakeKeyboard:
    def __init__(self, page: "_FakePage"):
        self._page = page

    async def press(self, key: str) -> None:
        self._page.actions.append(("press", key))


class _FakeMouse:
    def __init__(self, page: "_FakePage"):
        self._page = page

    async def wheel(self, dx: int, dy: int) -> None:
        self._page.actions.append(("wheel", dx, dy))


class _FakePage:
    def __init__(
        self,
        url: str = "https://example.com/",
        title: str = "Example Page",
        context: "_FakeContext | None" = None,
    ):
        self.url = url
        self._title = title
        self.context = context
        self._closed = False
        self.default_timeout: int | None = None
        self.actions: list[tuple[Any, ...]] = []
        self.evaluate_calls = 0
        self.snapshot_result: Any = {"found": True, "text": "[1] <button> Submit"}
        self.body_text: str = "Hello Browser"
        self.locators: dict[str, _FakeLocator] = {}
        self.keyboard = _FakeKeyboard(self)
        self.mouse = _FakeMouse(self)
        self.handlers: dict[str, Any] = {}

    def is_closed(self) -> bool:
        return self._closed

    def set_default_timeout(self, timeout_ms: int) -> None:
        self.default_timeout = timeout_ms

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    async def title(self) -> str:
        return self._title

    async def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        self.actions.append(("goto", url, wait_until))
        self.url = url

    async def go_back(self, wait_until: str = "domcontentloaded") -> None:
        self.actions.append(("go_back", wait_until))

    async def wait_for_load_state(self, state: str = "domcontentloaded", timeout: int | None = None) -> None:
        self.actions.append(("wait_for_load_state", state, timeout))

    async def wait_for_url(self, predicate: Any, timeout: int | None = None) -> None:
        self.actions.append(("wait_for_url", timeout))
        if not predicate(self.url):
            raise TimeoutError("url mismatch")

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.evaluate_calls += 1
        self.actions.append(("evaluate", arg))
        if "document.body" in script:
            return self.body_text
        return self.snapshot_result

    def locator(self, selector: str) -> _FakeLocator:
        if selector in self.locators:
            return self.locators[selector]
        return _FakeLocator(self, selector)

    def get_by_text(self, text: str) -> _FakeLocator:
        return self.locators.get(f"text={text}", _FakeLocator(self, f"text={text}"))

    async def screenshot(self, path: str, full_page: bool = False) -> None:
        self.actions.append(("screenshot", path, full_page))
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfake")

    async def bring_to_front(self) -> None:
        self.actions.append(("bring_to_front",))

    async def close(self) -> None:
        self._closed = True


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []
        self.routes: list[tuple[str, Any]] = []
        self.handlers: dict[str, Any] = {}

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    async def new_page(self) -> _FakePage:
        page = _FakePage(context=self)
        self.pages.append(page)
        return page


class _FakeBrowser:
    def __init__(self, *, fail_close: bool = False) -> None:
        self.connected = True
        self.fail_close = fail_close
        self.close_calls = 0
        self.contexts: list[_FakeContext] = []

    def is_connected(self) -> bool:
        return self.connected

    async def new_context(self) -> _FakeContext:
        ctx = _FakeContext()
        self.contexts.append(ctx)
        return ctx

    async def new_page(self) -> _FakePage:
        ctx = self.contexts[0] if self.contexts else await self.new_context()
        return await ctx.new_page()

    async def close(self) -> None:
        self.close_calls += 1
        self.connected = False
        if self.fail_close:
            raise RuntimeError("browser already crashed")


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser | None = None) -> None:
        self.browser = browser or _FakeBrowser()
        self.launch_calls = 0
        self.cdp_calls = 0

    async def launch(self, **_kwargs: Any) -> _FakeBrowser:
        self.launch_calls += 1
        return self.browser

    async def connect_over_cdp(self, _url: str) -> _FakeBrowser:
        self.cdp_calls += 1
        return self.browser


class _FakePlaywright:
    def __init__(self, browser: _FakeBrowser | None = None) -> None:
        self.chromium = _FakeChromium(browser)
        self.stop_calls = 0

    async def start(self) -> "_FakePlaywright":
        return self

    async def stop(self) -> None:
        self.stop_calls += 1


@pytest.fixture(autouse=True)
def reset_browser_session(monkeypatch, tmp_path):
    """每個測試前後重置單例 session 並隔離稽核日誌。"""
    monkeypatch.setenv("POCKET_AGENT_LOG_FILE", str(tmp_path / "agent.log"))
    for key in (
        "BROWSER_ALLOWED_DOMAINS",
        "BROWSER_ALLOW_PRIVATE_NETWORKS",
        "BROWSER_ALLOW_LOOPBACK",
        "BROWSER_HEADLESS",
        "BROWSER_CDP_URL",
        "BROWSER_TIMEOUT_MS",
    ):
        monkeypatch.delenv(key, raising=False)
    asyncio.run(browser_mcp.browser_session.close())
    yield
    asyncio.run(browser_mcp.browser_session.close())


# ==========================================
# 1. URL 安全策略與 SSRF / 白名單測試
# ==========================================
def test_url_policy_rejects_unsafe_schemes_and_empty_userinfo(monkeypatch):
    policy = browser_security.DefaultBrowserUrlPolicy()
    for bad in ("file:///C:/Windows/win.ini", "javascript:alert(1)", "chrome://settings", "ftp://example.com"):
        with pytest.raises(ValueError):
            norm = policy.normalize_navigation_url(bad)
            policy.check_url_allowed(norm)

    assert policy.normalize_navigation_url("HTTP://Example.COM/path") == "http://Example.COM/path"
    assert policy.normalize_navigation_url("example.com/docs") == "https://example.com/docs"

    monkeypatch.setenv("BROWSER_ALLOWED_DOMAINS", "example.com")
    with pytest.raises(ValueError, match="Userinfo"):
        policy.check_url_allowed("https://@example.com/")
    with pytest.raises(ValueError, match="Userinfo"):
        policy.check_url_allowed("https://user:@example.com/")


def test_url_policy_domain_allowlist_wildcard_and_trailing_dot(monkeypatch):
    monkeypatch.setenv("BROWSER_ALLOWED_DOMAINS", "*.corp.local, example.org.")
    browser_mcp.check_url_allowed("https://app.corp.local/home")
    browser_mcp.check_url_allowed("https://example.org./")
    browser_mcp.check_url_allowed("https://sub.example.org/")

    with pytest.raises(ValueError):
        browser_mcp.check_url_allowed("https://corp.local/")
    with pytest.raises(ValueError):
        browser_mcp.check_url_allowed("https://evil.com/")


def test_url_policy_blocks_metadata_loopback_private_and_cdp(monkeypatch):
    for blocked_ip in (
        "http://169.254.169.254/latest",
        "http://[fe80::1]/",
        "http://127.0.0.1:8080/",
        "http://10.0.0.1/",
    ):
        with pytest.raises(ValueError):
            browser_mcp.check_url_allowed(blocked_ip)

    monkeypatch.setenv("BROWSER_ALLOW_LOOPBACK", "true")
    monkeypatch.setenv("BROWSER_ALLOW_PRIVATE_NETWORKS", "true")
    browser_mcp.check_url_allowed("http://127.0.0.1:8080/")
    browser_mcp.check_url_allowed("http://10.0.0.1/")

    # 即使允許 loopback，也絕不允許存取使用中的 CDP 除錯端點，且 Metadata 永遠封鎖
    monkeypatch.setenv("BROWSER_CDP_URL", "http://localhost:9222")
    with pytest.raises(ValueError, match="CDP"):
        browser_mcp.check_url_allowed("http://127.0.0.1:9222/json")
    with pytest.raises(ValueError):
        browser_mcp.check_url_allowed("http://169.254.169.254/latest")


# ==========================================
# 2. 前置守衛：動作與 JS 評估前必須驗證網址
# ==========================================
def test_pre_execution_guard_blocks_unauthorized_page_before_evaluate_or_action(monkeypatch):
    monkeypatch.setenv("BROWSER_ALLOWED_DOMAINS", "allowed.example")
    ctx = _FakeContext()
    evil_page = _FakePage(url="https://evil.example/phish", context=ctx)
    ctx.pages.append(evil_page)

    session = browser_mcp.browser_session
    session._browser = _FakeBrowser()
    session._context = ctx
    session._page = evil_page

    res_snap = asyncio.run(browser_mcp.browser_snapshot())
    assert "[ERROR]" in res_snap
    assert evil_page.evaluate_calls == 0
    assert evil_page.url == "about:blank"

    evil_page.url = "https://evil.example/phish"
    res_text = asyncio.run(browser_mcp.browser_get_text())
    assert "[ERROR]" in res_text
    assert evil_page.evaluate_calls == 0

    evil_page.url = "https://evil.example/phish"
    res_click = asyncio.run(browser_mcp.browser_click(1))
    assert "[ERROR]" in res_click
    assert not any(a[0] == "click" for a in evil_page.actions)

    evil_page.url = "https://evil.example/phish"
    res_fill = asyncio.run(browser_mcp.browser_fill(1, "secret"))
    assert "[ERROR]" in res_fill
    assert not any(a[0] == "fill" for a in evil_page.actions)


# ==========================================
# 3. 機密遮罩與集中稽核測試
# ==========================================
def test_redaction_and_audit_log_on_get_text_and_fill(monkeypatch, tmp_path):
    ctx = _FakeContext()
    page = _FakePage(url="https://example.com/login", context=ctx)
    page.body_text = (
        "Authorization: Bearer super_secret_bearer_token_123456\n"
        "url=https://admin:hunter2@db.internal:5432/prod\n" + ("A" * 12500)
    )
    page.locators['[data-mcp-ref="2"]'] = _FakeLocator(
        page,
        '[data-mcp-ref="2"]',
        attrs={"type": "text", "name": "api_key", "autocomplete": "one-time-code"},
    )
    ctx.pages.append(page)

    session = browser_mcp.browser_session
    session._browser = _FakeBrowser()
    session._context = ctx
    session._page = page

    text_out = asyncio.run(browser_mcp.browser_get_text())
    assert "super_secret_bearer_token_123456" not in text_out
    assert "hunter2" not in text_out
    assert "[REDACTED]" in text_out

    fill_out = asyncio.run(browser_mcp.browser_fill(2, "sk-1234567890abcdef1234567890"))
    assert "sk-1234567890" not in fill_out
    assert "********" in fill_out

    log_file = tmp_path / "agent.log"
    assert log_file.is_file()
    log_text = log_file.read_text(encoding="utf-8")
    assert "[AUDIT] [MCP:browser]" in log_text
    assert "sk-1234567890" not in log_text


# ==========================================
# 4. DOM 快照 JS 與選擇器檢查
# ==========================================
def test_snapshot_js_clears_stale_refs_and_handles_missing_selector():
    js = browser_snapshot_mod.build_snapshot_js()
    assert "removeAttribute('data-mcp-ref')" in js

    ctx = _FakeContext()
    page = _FakePage(url="https://example.com/", context=ctx)
    page.snapshot_result = {"found": False, "text": ""}
    ctx.pages.append(page)

    session = browser_mcp.browser_session
    session._browser = _FakeBrowser()
    session._context = ctx
    session._page = page

    out = asyncio.run(browser_mcp.browser_snapshot(selector="#missing-panel"))
    assert "[ERROR]" in out
    assert "#missing-panel" in out


# ==========================================
# 5. 生命週期、分頁重用、並發鎖與例外容錯
# ==========================================
def test_lifecycle_tab_reuse_close_resilience_and_concurrent_lock(monkeypatch):
    fake_pw = _FakePlaywright()
    monkeypatch.setattr(browser_session_mod, "load_async_playwright", lambda: lambda: fake_pw)

    async def _run_concurrent():
        p1, p2 = await asyncio.gather(
            browser_mcp.browser_session.ensure_page(),
            browser_mcp.browser_session.ensure_page(),
        )
        return p1, p2

    p1, p2 = asyncio.run(_run_concurrent())
    assert p1 is p2
    assert fake_pw.chromium.launch_calls == 1

    # 關閉單一分頁後，應重用既有瀏覽器開啟新分頁，而非啟動新瀏覽器
    asyncio.run(p1.close())
    p3 = asyncio.run(browser_mcp.browser_session.ensure_page())
    assert p3 is not p1
    assert fake_pw.chromium.launch_calls == 1

    # 即使 browser.close() 拋錯，仍應停止 Playwright 並清空狀態
    fake_pw.chromium.browser.fail_close = True
    res_close = asyncio.run(browser_mcp.browser_close())
    assert "關閉" in res_close or "closed" in res_close.lower()
    assert fake_pw.stop_calls == 1
    assert browser_mcp.browser_session._browser is None
    assert browser_mcp.browser_session._pw is None


def test_server_lifespan_closes_active_session(monkeypatch):
    fake_pw = _FakePlaywright()
    monkeypatch.setattr(browser_session_mod, "load_async_playwright", lambda: lambda: fake_pw)

    async def _exercise():
        async with browser_session_mod.server_lifespan(browser_mcp.mcp):
            await browser_mcp.browser_session.ensure_page()

    asyncio.run(_exercise())
    assert fake_pw.stop_calls == 1
    assert browser_mcp.browser_session._browser is None


# ==========================================
# 6. 跨平台人工確認策略測試
# ==========================================
def test_wait_for_user_headless_and_cross_platform_strategy(monkeypatch):
    monkeypatch.setenv("BROWSER_HEADLESS", "True")
    res_headless = asyncio.run(browser_mcp.browser_wait_for_user())
    assert "[ERROR]" in res_headless

    monkeypatch.delenv("BROWSER_HEADLESS", raising=False)
    monkeypatch.setattr(browser_prompt.sys, "platform", "linux")
    strategy = browser_prompt.get_user_prompt_strategy()
    assert isinstance(strategy, browser_prompt.TtyFallbackStrategy)

    ctx = _FakeContext()
    page = _FakePage(url="https://example.com/captcha", context=ctx)
    ctx.pages.append(page)
    browser_mcp.browser_session._browser = _FakeBrowser()
    browser_mcp.browser_session._context = ctx
    browser_mcp.browser_session._page = page

    monkeypatch.setattr(browser_prompt, "tty_prompt_text", lambda _msg, default="y": "y")
    res_ok = asyncio.run(browser_mcp.browser_wait_for_user())
    assert "browser_snapshot" in res_ok

    monkeypatch.setattr(browser_prompt, "tty_prompt_text", lambda _msg, default="y": "n")
    res_cancel = asyncio.run(browser_mcp.browser_wait_for_user("Solve CAPTCHA"))
    assert "取消" in res_cancel or "cancelled" in res_cancel.lower()


# ==========================================
# 7. P0 擴充工具：screenshot / select_option / wait / tabs
# ==========================================
def test_p0_screenshot_select_wait_and_tabs(monkeypatch, tmp_path):
    fake_user_dir = tmp_path / "home" / ".pocket-agent"
    monkeypatch.setattr("pocket_agent.system.paths.user_dir", lambda: fake_user_dir)

    ctx = _FakeContext()
    page1 = _FakePage(url="https://example.com/form", title="Form", context=ctx)
    page2 = _FakePage(url="https://example.com/docs", title="Docs", context=ctx)
    page1.locators['[data-mcp-ref="3"]'] = _FakeLocator(page1, '[data-mcp-ref="3"]')
    ctx.pages.extend([page1, page2])

    session = browser_mcp.browser_session
    session._browser = _FakeBrowser()
    session._context = ctx
    session._page = page1

    # browser_screenshot 應寫入 ~/.pocket-agent/artifacts/browser_shots/
    shot_res = asyncio.run(browser_mcp.browser_screenshot(full_page=True))
    assert "browser_shots" in shot_res
    saved_files = list((fake_user_dir / "artifacts" / "browser_shots").glob("browser_*.png"))
    assert len(saved_files) == 1

    # browser_select_option 參數驗證與選取
    assert "[ERROR]" in asyncio.run(browser_mcp.browser_select_option(3))
    sel_res = asyncio.run(browser_mcp.browser_select_option(3, label="Taiwan"))
    assert "Taiwan" in sel_res

    # browser_wait 驗證
    assert "[ERROR]" in asyncio.run(browser_mcp.browser_wait())
    assert "[ERROR]" in asyncio.run(browser_mcp.browser_wait(state="invalid"))
    wait_res = asyncio.run(browser_mcp.browser_wait(text="Ready", url_contains="example.com", timeout_sec=99))
    assert "example.com" in wait_res

    # 分頁列表、切換與關閉
    tabs_res = asyncio.run(browser_mcp.browser_list_tabs())
    assert "*[1] Form" in tabs_res
    assert " [2] Docs" in tabs_res

    switch_res = asyncio.run(browser_mcp.browser_switch_tab(2))
    assert "[2]" in switch_res
    assert session._page is page2

    close_tab_res = asyncio.run(browser_mcp.browser_close_tab(2))
    assert "[2]" in close_tab_res
    assert page2.is_closed()
    assert session._page is page1

"""Playwright 瀏覽器連線與分頁生命週期轉接器（Adapter Pattern）。"""

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp_servers._shared import CONSTANTS, t
from mcp_servers.browser.security import (
    AUDIT,
    BROWSER_REDACTION,
    BrowserUrlPolicy,
    DefaultBrowserUrlPolicy,
    load_browser_config,
)


def load_async_playwright() -> Any:
    """延遲載入 playwright.async_api，缺少選用套件時回傳 None 以保持可攜版相容。"""
    try:
        from playwright.async_api import async_playwright  # pyright: ignore[reportMissingImports]

        return async_playwright
    except ImportError:
        return None


def resolve_screenshot_dir() -> Path:
    """解析截圖輸出目錄（集中於 ~/.pocket-agent/artifacts/browser_shots/，零工作區污染）。"""
    try:
        from pocket_agent.system.paths import user_dir

        base = user_dir()
    except ImportError:
        base = Path.home() / ".pocket-agent"
    return base / "artifacts" / "browser_shots"


class BrowserSessionAdapter:
    """封裝 Playwright、Browser、Context、分頁清單與並發鎖的生命週期管理。"""

    def __init__(self, policy: BrowserUrlPolicy | None = None) -> None:
        self._policy: BrowserUrlPolicy = policy or DefaultBrowserUrlPolicy()
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._is_cdp: bool = False
        self._recent_dialogs: deque[str] = deque(maxlen=CONSTANTS.browser_max_tracked_dialogs)
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    @property
    def policy(self) -> BrowserUrlPolicy:
        return self._policy

    def _get_lock(self) -> asyncio.Lock:
        """取得綁定至目前 Event Loop 的非同步鎖，相容測試環境多迴圈切換。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    @staticmethod
    def _is_browser_connected(browser: Any) -> bool:
        if browser is None:
            return False
        is_conn = getattr(browser, "is_connected", None)
        if callable(is_conn):
            try:
                return bool(is_conn())
            except Exception:
                return False
        return True

    def _attach_page_listeners(self, page: Any, timeout_ms: int) -> None:
        if page is None:
            return
        if hasattr(page, "set_default_timeout"):
            page.set_default_timeout(timeout_ms)
        if getattr(page, "_pocket_listeners_installed", False):
            return
        on_fn = getattr(page, "on", None)
        if callable(on_fn):

            def _on_dialog(dialog: Any) -> None:
                dtype = getattr(dialog, "type", "dialog")
                dmsg = BROWSER_REDACTION.clip_text(
                    BROWSER_REDACTION.redact(str(getattr(dialog, "message", ""))),
                    200,
                )
                self._recent_dialogs.append(f"[{dtype}] {dmsg}")
                dismiss = getattr(dialog, "dismiss", None)
                if callable(dismiss):
                    try:
                        asyncio.create_task(dismiss())
                    except Exception:
                        pass

            on_fn("dialog", _on_dialog)
            page._pocket_listeners_installed = True

    async def _install_context_guards(self, ctx: Any, timeout_ms: int) -> None:
        if ctx is None or getattr(ctx, "_pocket_route_installed", False):
            return

        async def _intercept_request(route: Any) -> None:
            req = getattr(route, "request", None)
            req_url = getattr(req, "url", "")
            resource_type = getattr(req, "resource_type", "")
            is_sub = bool(resource_type and resource_type != "document")
            try:
                self._policy.check_url_allowed(req_url, is_subresource=is_sub)
                await route.continue_()
            except ValueError as exc:
                AUDIT.log("route_guard", str(req_url), status="BLOCKED", detail=str(exc))
                await route.abort("blockedbyclient")

        route_fn = getattr(ctx, "route", None)
        if callable(route_fn):
            await route_fn("**/*", _intercept_request)
        on_fn = getattr(ctx, "on", None)
        if callable(on_fn):
            on_fn("page", lambda p: self._attach_page_listeners(p, timeout_ms))
        ctx._pocket_route_installed = True

    async def _cleanup_dead_browser(self) -> None:
        if self._browser is not None and not self._is_browser_connected(self._browser):
            self._browser = None
            self._context = None
            self._page = None
            self._is_cdp = False

    async def ensure_page(self) -> Any:
        """取得作用中分頁；若分頁已關閉但瀏覽器仍連線則重用既有瀏覽器開新分頁。"""
        async with self._get_lock():
            if self._page is not None and not self._page.is_closed():
                return self._page

            cfg = load_browser_config()
            await self._cleanup_dead_browser()

            if self._browser is not None and self._is_browser_connected(self._browser):
                ctx = self._context
                if ctx is None:
                    contexts = getattr(self._browser, "contexts", None) or []
                    ctx = contexts[0] if contexts else await self._browser.new_context()
                    self._context = ctx
                await self._install_context_guards(ctx, cfg.timeout_ms)
                pages = [p for p in (getattr(ctx, "pages", None) or []) if not p.is_closed()]
                self._page = pages[0] if pages else await ctx.new_page()
                self._attach_page_listeners(self._page, cfg.timeout_ms)
                return self._page

            pw_factory = load_async_playwright()
            if pw_factory is None:
                raise RuntimeError(t("browser_mcp.playwright_missing"))

            if self._pw is None:
                self._pw = await pw_factory().start()

            try:
                if cfg.cdp_url:
                    self._is_cdp = True
                    self._browser = await self._pw.chromium.connect_over_cdp(cfg.cdp_url)
                    contexts = getattr(self._browser, "contexts", None) or []
                    ctx = contexts[0] if contexts else await self._browser.new_context()
                    self._context = ctx
                    pages = [p for p in (getattr(ctx, "pages", None) or []) if not p.is_closed()]
                    self._page = pages[0] if pages else await ctx.new_page()
                else:
                    self._is_cdp = False
                    last_err: Exception | None = None
                    for channel in ("msedge", "chrome", None):
                        try:
                            self._browser = await self._pw.chromium.launch(
                                headless=cfg.headless,
                                channel=channel,
                            )
                            break
                        except Exception as exc:
                            last_err = exc
                    else:
                        raise RuntimeError(t("browser_mcp.browser_not_found", error=last_err))
                    self._page = await self._browser.new_page()
                    self._context = getattr(self._page, "context", None)

                await self._install_context_guards(self._context, cfg.timeout_ms)
                self._attach_page_listeners(self._page, cfg.timeout_ms)
                return self._page
            except Exception:
                await self._close_unlocked()
                raise

    async def validate_page_url(self, page: Any, tool_name: str = "guard") -> None:
        """在操作或讀取前驗證頁面目前網址，違規時重置為 about:blank 並拒絕。"""
        curr_url = getattr(page, "url", "") or ""
        if curr_url and curr_url != "about:blank":
            try:
                self._policy.check_url_allowed(curr_url)
            except ValueError as exc:
                AUDIT.log(tool_name, curr_url, status="BLOCKED", detail=str(exc))
                try:
                    await page.goto("about:blank")
                except Exception:
                    pass
                raise

    async def ensure_allowed_page(self, tool_name: str = "guard") -> Any:
        """確保取得合法且通過網域政策驗證之作用中頁面。"""
        page = await self.ensure_page()
        await self.validate_page_url(page, tool_name=tool_name)
        return page

    async def page_state(self, page: Any, *, tool_name: str = "state") -> str:
        """驗證頁面安全後回傳經過機密遮罩的標題、網址與近期對話框摘要。"""
        await self.validate_page_url(page, tool_name=tool_name)
        title = BROWSER_REDACTION.redact(str(await page.title()))
        url = BROWSER_REDACTION.redact(str(getattr(page, "url", "")))
        state = t("browser_mcp.page_state", title=title, url=url)
        if self._recent_dialogs:
            dialogs_text = "\n".join(self._recent_dialogs)
            self._recent_dialogs.clear()
            state += t("browser_mcp.dialogs_notice", dialogs=dialogs_text)
        return state

    async def open_pages(self) -> list[Any]:
        """取得目前瀏覽器上下文中所有未關閉的分頁清單。"""
        if self._context is not None:
            pages = getattr(self._context, "pages", None)
            if pages is not None:
                return [p for p in pages if not p.is_closed()]
        if self._page is not None and not self._page.is_closed():
            return [self._page]
        return []

    async def list_tabs(self) -> list[dict[str, Any]]:
        """列出所有開啟中的分頁中繼資料（1-based 索引，標題與網址均經遮罩）。"""
        pages = await self.open_pages()
        tabs: list[dict[str, Any]] = []
        for idx, page in enumerate(pages, start=1):
            try:
                raw_title = str(await page.title())
            except Exception:
                raw_title = ""
            tabs.append(
                {
                    "index": idx,
                    "title": BROWSER_REDACTION.redact(raw_title),
                    "url": BROWSER_REDACTION.redact(str(getattr(page, "url", ""))),
                    "active": page is self._page,
                }
            )
        return tabs

    async def switch_tab(self, index: int) -> Any:
        """切換至指定編號（1-based）之分頁，切換前強制通過安全策略驗證。"""
        pages = await self.open_pages()
        if not pages:
            raise ValueError(t("browser_mcp.tabs_empty"))
        if isinstance(index, bool) or index < 1 or index > len(pages):
            raise ValueError(t("browser_mcp.tab_invalid_index", index=index, count=len(pages)))
        target = pages[index - 1]
        await self.validate_page_url(target, tool_name="browser_switch_tab")
        self._page = target
        bring_to_front = getattr(target, "bring_to_front", None)
        if callable(bring_to_front):
            try:
                await bring_to_front()
            except Exception:
                pass
        return target

    async def close_tab(self, index: int = 0) -> tuple[int, Any | None]:
        """關閉指定編號分頁（0 代表目前分頁），並回傳實際編號與接續作用分頁。"""
        pages = await self.open_pages()
        if not pages:
            raise ValueError(t("browser_mcp.tabs_empty"))
        if isinstance(index, bool) or index < 0 or index > len(pages):
            raise ValueError(t("browser_mcp.tab_invalid_index", index=index, count=len(pages)))

        if index == 0:
            if self._page in pages:
                target_idx = pages.index(self._page) + 1
                target = self._page
            else:
                target_idx = len(pages)
                target = pages[-1]
        else:
            target_idx = index
            target = pages[index - 1]

        await target.close()
        remaining = [p for p in pages if p is not target and not p.is_closed()]
        self._page = remaining[-1] if remaining else None
        return target_idx, self._page

    async def _close_unlocked(self) -> bool:
        had_resource = self._browser is not None or self._pw is not None
        browser = self._browser
        pw = self._pw
        self._browser = None
        self._context = None
        self._page = None
        self._pw = None
        self._is_cdp = False
        self._recent_dialogs.clear()

        try:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
        finally:
            if pw is not None:
                try:
                    await pw.stop()
                except Exception:
                    pass
        return had_resource

    async def close(self) -> bool:
        """關閉瀏覽器並釋放 Playwright 資源（無論例外皆保證狀態歸零）。"""
        async with self._get_lock():
            return await self._close_unlocked()


browser_session = BrowserSessionAdapter()


@asynccontextmanager
async def server_lifespan(_server: Any) -> AsyncIterator[None]:
    """MCPServer 生命週期鉤子：伺服器結束時確保關閉瀏覽器與 Playwright。"""
    try:
        yield
    finally:
        await browser_session.close()

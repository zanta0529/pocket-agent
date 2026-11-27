"""瀏覽器工具群組共用輔助函式（參數驗證、定位器確認與統一錯誤封裝）。"""

from typing import Any

from mcp_servers._shared import t
from mcp_servers.browser.security import AUDIT, BROWSER_REDACTION


def validate_ref(ref: int) -> int:
    """驗證互動元素編號必須為大於等於 1 的整數。"""
    if isinstance(ref, bool) or not isinstance(ref, int) or ref < 1:
        raise ValueError(t("browser_mcp.invalid_ref", ref=ref))
    return ref


def ref_locator(page: Any, ref: int) -> Any:
    """取得對應 data-mcp-ref 編號之 Playwright Locator。"""
    return page.locator(f'[data-mcp-ref="{ref}"]')


async def require_ref_locator(page: Any, ref: int) -> Any:
    """驗證元素編號並確認元素存在於頁面，避免無效編號苦等逾時。"""
    valid_ref = validate_ref(ref)
    locator = ref_locator(page, valid_ref)
    count_fn = getattr(locator, "count", None)
    if callable(count_fn):
        if await count_fn() < 1:
            raise ValueError(t("browser_mcp.ref_not_found", ref=valid_ref))
    return locator.first


def format_tool_error(
    tool_name: str,
    exc: Exception,
    *,
    target: str = "",
    blocked: bool = False,
) -> str:
    """記錄集中化稽核日誌並回傳經機密遮罩之 [ERROR] 訊息。"""
    status = "BLOCKED" if blocked else "ERROR"
    safe_msg = BROWSER_REDACTION.redact(str(exc))
    AUDIT.log(tool_name, tool_name, target=target, status=status, detail=safe_msg)
    if safe_msg.startswith("[ERROR]"):
        return safe_msg
    if isinstance(exc, ValueError):
        return f"[ERROR] {safe_msg}"
    return f"[ERROR] {t('browser_mcp.operation_failed', error=safe_msg)}"

"""GitHub REST API 非同步傳輸轉接器（Adapter Pattern）：連線池、手動重導向驗證、串流上限與錯誤映射。"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

import certifi
import httpx

from mcp_servers._shared import (
    CONSTANTS,
    AuditLogger,
    ConnAuthError,
    CredentialCache,
    is_empty_or_null,
    tty_prompt_secret,
)
from mcp_servers.github import security

GITHUB_API_BASE = "https://api.github.com"
NETWORK_TIMEOUT_SEC = CONSTANTS.network_timeout_sec
MAX_REDIRECTS = CONSTANTS.max_redirects
_MAX_ERROR_DETAIL_CHARS = 800
_MAX_422_ERROR_ITEMS = 10

CREDS_CACHE = CredentialCache(ttl_seconds=600.0)
AUDIT = AuditLogger("github", redaction_policy=security.GITHUB_REDACTION)

_SHARED_CLIENT: httpx.AsyncClient | None = None
_SHARED_CLIENT_LOOP: asyncio.AbstractEventLoop | None = None


def clear_creds_cache() -> None:
    """清除執行期 Token 快取並清空共用連線池 Cookies（保留連線池供後續呼叫重用）。"""
    CREDS_CACHE.clear()
    if _SHARED_CLIENT is not None and not _SHARED_CLIENT.is_closed:
        _SHARED_CLIENT.cookies.clear()


async def close_shared_client() -> None:
    """關閉共用 HTTP 連線池。"""
    global _SHARED_CLIENT, _SHARED_CLIENT_LOOP
    client = _SHARED_CLIENT
    _SHARED_CLIENT = None
    _SHARED_CLIENT_LOOP = None
    if client is not None and not client.is_closed:
        try:
            await client.aclose()
        except Exception:
            pass


@asynccontextmanager
async def server_lifespan(_server: Any) -> AsyncIterator[None]:
    """MCPServer 生命週期鉤子：結束時關閉共用連線池。"""
    try:
        yield
    finally:
        await close_shared_client()


@lru_cache(maxsize=1)
def _ssl_verify() -> bool | str:
    """優先注入作業系統憑證庫 (truststore) 以相容企業 SSL 代理，失敗時降級至 certifi。"""
    try:
        import truststore

        truststore.inject_into_ssl()
        return True
    except ImportError:
        return certifi.where()


def build_async_client() -> httpx.AsyncClient:
    """建立具備逾時、停用自動重導向與系統 TLS 憑證驗證之非同步 HTTP 客戶端。"""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(NETWORK_TIMEOUT_SEC),
        follow_redirects=False,
        verify=_ssl_verify(),
    )


@asynccontextmanager
async def create_http_client() -> AsyncIterator[httpx.AsyncClient]:
    """提供可跨呼叫重用 TCP/TLS 連線池之非同步 HTTP 客戶端上下文管理器。"""
    global _SHARED_CLIENT, _SHARED_CLIENT_LOOP
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    old_client: httpx.AsyncClient | None = None
    if _SHARED_CLIENT is None or _SHARED_CLIENT.is_closed or _SHARED_CLIENT_LOOP is not current_loop:
        old_client = _SHARED_CLIENT
        _SHARED_CLIENT = build_async_client()
        _SHARED_CLIENT_LOOP = current_loop
    client = _SHARED_CLIENT

    if old_client is not None and not old_client.is_closed:
        try:
            await old_client.aclose()
        except Exception:
            pass

    yield client


def resolve_token(github_token: str = "") -> str:
    """依「動態注入 → 環境變數 → 記憶體快取 → 終端機詢問」順序解析 GITHUB_TOKEN。"""
    raw = github_token.strip().strip("'\"") if github_token else ""
    if is_empty_or_null(raw):
        env_val = os.getenv("GITHUB_TOKEN")
        if not is_empty_or_null(env_val):
            raw = (env_val or "").strip().strip("'\"")
        else:
            cached = CREDS_CACHE.get_secret("GITHUB_TOKEN")
            if not is_empty_or_null(cached):
                raw = cached
            else:
                try:
                    raw = tty_prompt_secret("請輸入 GitHub Personal Access Token (GITHUB_TOKEN): ").strip().strip("'\"")
                except ValueError as exc:
                    msg = "[CONN_AUTH] 缺少 GITHUB_TOKEN 憑證。請於 mcp_config.yaml 的 env 區段設定 GITHUB_TOKEN。"
                    raise ConnAuthError(msg) from exc

    if is_empty_or_null(raw):
        clear_creds_cache()
        raise ConnAuthError(
            "[CONN_AUTH] 未提供有效的 GITHUB_TOKEN。請於 mcp_config.yaml 的 env 區段設定 GITHUB_TOKEN。"
        )

    if any(ch in raw for ch in ("\r", "\n", "\x00")):
        clear_creds_cache()
        raise ConnAuthError("[CONN_AUTH] GITHUB_TOKEN 包含無效的控制字元。")

    CREDS_CACHE.set_secret("GITHUB_TOKEN", raw)
    return raw


def get_headers(github_token: str = "") -> dict[str, str]:
    """建立 GitHub REST API 標準請求標頭。"""
    token = resolve_token(github_token)
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "PocketAgent-GitHub-MCP",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _read_bounded_stream(resp: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """以串流方式累計讀取 HTTP 回應本文，超過上限立即中止並回傳 (資料, 是否超限)。"""
    content_len = resp.headers.get("content-length")
    if content_len and content_len.isascii() and content_len.isdigit() and int(content_len) > max_bytes:
        return b"", True
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf.extend(chunk)
        if len(buf) > max_bytes:
            return b"", True
    return bytes(buf), False


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str | int] | None = None,
    json_body: dict[str, Any] | None = None,
    allow_not_found: bool = False,
) -> Any:
    """執行具備手動重導向白名單驗證與串流大小硬上限的 GitHub REST API 請求。"""
    upper_method = method.upper()
    current_url: str | httpx.URL = url
    current_headers = dict(headers)
    current_params: dict[str, str | int] | None = params
    current_json: dict[str, Any] | None = json_body

    for hops in range(MAX_REDIRECTS + 1):
        stream_kwargs: dict[str, Any] = {
            "headers": current_headers,
            "follow_redirects": False,
        }
        if current_params is not None:
            stream_kwargs["params"] = current_params
        if current_json is not None:
            stream_kwargs["json"] = current_json

        async with client.stream(upper_method, current_url, **stream_kwargs) as resp:
            if resp.is_redirect and resp.has_redirect_location:
                if upper_method != "GET":
                    raise PermissionError("拒絕存取：寫入請求禁止自動重導向，請確認目標儲存庫與分支狀態。")
                if hops >= MAX_REDIRECTS:
                    raise httpx.TooManyRedirects("Exceeded maximum allowed redirects.", request=resp.request)
                if resp.next_request is None:
                    raise PermissionError("拒絕存取：無法解析重導向目標網址。")
                target_url = resp.next_request.url
                security.validate_redirect_url(target_url)
                current_url = target_url
                current_headers = dict(resp.next_request.headers)
                current_params = None
                current_json = None
                continue

            if allow_not_found and resp.status_code == 404:
                return None

            max_bytes = security.MAX_RESPONSE_BYTES
            body_bytes, exceeded = await _read_bounded_stream(resp, max_bytes)
            if not resp.is_success:
                safe_headers = [
                    (k, v)
                    for k, v in resp.headers.multi_items()
                    if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
                ]
                bounded_resp = httpx.Response(
                    status_code=resp.status_code,
                    headers=safe_headers,
                    content=b"" if exceeded else body_bytes,
                    request=resp.request,
                )
                bounded_resp.raise_for_status()

            if exceeded:
                raise ValueError(f"GitHub API 回應內容過大（超過 {max_bytes} 位元組上限）。")
            if not body_bytes:
                return None
            return json.loads(body_bytes)

    raise httpx.TooManyRedirects("Exceeded maximum allowed redirects.")


def format_http_error(exc: httpx.HTTPStatusError, *, active_token: str = "") -> tuple[str, str]:
    """將 GitHub HTTP 狀態碼轉換為明確且不含機密的中文訊息與稽核狀態碼。"""
    status_code = exc.response.status_code
    payload: Any = None
    try:
        payload = exc.response.json()
    except Exception:
        payload = None

    api_msg = ""
    if isinstance(payload, dict) and payload.get("message"):
        api_msg = security.GITHUB_REDACTION.clip_error_text(str(payload["message"]), active_token, limit=200)

    if status_code == 401:
        clear_creds_cache()
        return "[CONN_AUTH] GitHub 認證失敗 (HTTP 401)：Token 無效或已過期，已清除記憶體快取。", "AUTH_FAIL"
    if status_code == 403:
        remaining = exc.response.headers.get("x-ratelimit-remaining", "")
        if remaining == "0" or "rate limit" in api_msg.lower():
            reset_raw = (exc.response.headers.get("x-ratelimit-reset") or "").strip()
            reset_ts = reset_raw if (reset_raw.isascii() and reset_raw.isdigit() and len(reset_raw) <= 20) else ""
            reset_note = f"，重置時間戳：{reset_ts}" if reset_ts else ""
            return f"GitHub API 已達速率限制 (HTTP 403 Rate Limit Exceeded{reset_note})。", "ERROR"
        suffix = f"（{api_msg}）" if api_msg else ""
        return f"GitHub 拒絕存取 (HTTP 403)：Token 權限不足或資源受保護{suffix}。", "AUTH_FAIL"
    if status_code == 404:
        return "找不到指定的儲存庫、Issue 或 Pull Request，或 Token 無權存取該資源 (HTTP 404)。", "ERROR"
    if status_code == 422:
        err_details: list[str] = []
        if isinstance(payload, dict) and isinstance(payload.get("errors"), list):
            for item in payload["errors"][:_MAX_422_ERROR_ITEMS]:
                if not isinstance(item, dict):
                    continue
                if item.get("message"):
                    err_details.append(
                        security.GITHUB_REDACTION.clip_error_text(str(item["message"]), active_token, limit=160)
                    )
                elif item.get("field") and item.get("code"):
                    raw_pair = f"{item['field']}:{item['code']}"
                    err_details.append(security.GITHUB_REDACTION.clip_error_text(raw_pair, active_token, limit=160))
        raw_joined = "；".join(err_details) if err_details else api_msg
        joined_err = (
            security.GITHUB_REDACTION.clip_error_text(raw_joined, active_token, limit=_MAX_ERROR_DETAIL_CHARS)
            if raw_joined
            else ""
        )
        suffix = f"（{joined_err}）" if joined_err else ""
        return (
            f"GitHub 驗證失敗 (HTTP 422 Unprocessable Entity){suffix}。"
            "請確認來源分支 (head) 是否已 git push 至遠端，且尚未存在相同分支的開啟中 PR。",
            "ERROR",
        )
    suffix = f"：{api_msg}" if api_msg else ""
    return f"GitHub API 回傳錯誤 (HTTP {status_code}){suffix}", "ERROR"


def handle_tool_error(
    tool_name: str,
    cmd_desc: str,
    target: str,
    exc: Exception,
    action_label: str,
    *,
    active_token: str = "",
) -> str:
    """統一記錄失敗稽核日誌並格式化為 [ERROR] 字串回傳。"""
    if isinstance(exc, PermissionError):
        safe_detail = security.GITHUB_REDACTION.clip_error_text(str(exc), active_token)
        AUDIT.log(tool_name, cmd_desc, target=target, status="BLOCKED", detail=safe_detail, extra_secret=active_token)
        return f"[ERROR] {safe_detail}"
    if isinstance(exc, ConnAuthError):
        safe_err = security.GITHUB_REDACTION.clip_error_text(str(exc), active_token)
        AUDIT.log(
            tool_name,
            cmd_desc,
            target=target,
            status="AUTH_FAIL",
            detail="conn_auth_failed",
            extra_secret=active_token,
        )
        return f"[ERROR] {safe_err}"
    if isinstance(exc, httpx.HTTPStatusError):
        msg, status_label = format_http_error(exc, active_token=active_token)
        safe_msg = security.GITHUB_REDACTION.clip_error_text(msg, active_token)
        AUDIT.log(
            tool_name,
            cmd_desc,
            target=target,
            status=status_label,
            detail=safe_msg,
            extra_secret=active_token,
        )
        return f"[ERROR] {action_label}：{safe_msg}"
    safe_err = security.GITHUB_REDACTION.clip_error_text(str(exc), active_token)
    AUDIT.log(tool_name, cmd_desc, target=target, status="ERROR", detail=safe_err, extra_secret=active_token)
    return f"[ERROR] {action_label}：{safe_err}"

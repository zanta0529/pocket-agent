"""GitHub REST API 整合 MCP 伺服器（PR 列表、Issue/PR 檢視與留言新增）。

安全設計原則：
1. 嚴格驗證 owner / repo 識別字格式並編碼路徑，杜絕 `#` 片段截斷與 `..` 路徑穿越。
2. 支援 GITHUB_ALLOWED_REPOS 儲存庫白名單控管（逗號分隔 `owner/repo`，不分大小寫）。
3. 支援彈性憑證解析：動態隱藏注入、環境變數或終端機隱藏輸入（記憶體快取 10 分鐘）。
4. 雙向機密防護：回傳文字自動遮罩機密；外發留言偵測到私鑰或真實 Token 時直接阻斷。
5. 單次輸出受 MAX_OUTPUT_CHARS 限制，整合 truststore 系統憑證與集中稽核日誌。
"""

import getpass
import os
import re
import sys
import time
import warnings
from pathlib import Path
from urllib.parse import quote

import certifi
import httpx

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

try:
    from pocket_agent.system.constants import (
        DEFAULT_MAX_REDIRECTS,
        DEFAULT_NETWORK_TIMEOUT_SEC,
        DEFAULT_TOOL_OUTPUT_CHARS,
    )

    MAX_OUTPUT_CHARS = DEFAULT_TOOL_OUTPUT_CHARS
    NETWORK_TIMEOUT_SEC = DEFAULT_NETWORK_TIMEOUT_SEC
    MAX_REDIRECTS = DEFAULT_MAX_REDIRECTS
except ImportError:
    MAX_OUTPUT_CHARS = 12000
    NETWORK_TIMEOUT_SEC = 15.0
    MAX_REDIRECTS = 5

mcp = MCPServer("github")

GITHUB_API_BASE = "https://api.github.com"
_ALLOWED_PR_STATES = frozenset({"open", "closed", "all"})
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_RUNTIME_CREDS: dict[str, str] = {}
_RUNTIME_CREDS_TS: float = 0.0
_CREDS_TTL_SECONDS: float = 600.0

_PEM_PATTERN = r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
_TOKEN_PATTERN = r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b"
_BEARER_PATTERN = r"(?i)(\bauthorization\s*[:=]\s*(?:bearer|token)\s+)([^\s\"',;]+)"

_REDACT_PEM_RE = re.compile(_PEM_PATTERN)
_REDACT_URL_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^\s/@\"']+:[^\s/@\"']+@")
_REDACT_TOKEN_RE = re.compile(_TOKEN_PATTERN)
_REDACT_AUTH_BEARER_RE = re.compile(_BEARER_PATTERN)
_REDACT_KV_RE = re.compile(
    r"(?i)(\b(?:password|passwd|secret|token|github_token|api[_-]?key|client_secret"
    r"|passphrase|connectionstring)\b\s*[:=]\s*)([^\s#;\"',)]+|\"[^\"]*\"|'[^']*')",
)


class _ConnAuthError(RuntimeError):
    """表示 GitHub 憑證缺失或認證失敗，供上層識別 [CONN_AUTH] 並清除快取。"""


def _clear_creds_cache() -> None:
    global _RUNTIME_CREDS_TS
    _RUNTIME_CREDS.clear()
    _RUNTIME_CREDS_TS = 0.0


def _ensure_creds_ttl() -> None:
    if _RUNTIME_CREDS and _RUNTIME_CREDS_TS > 0:
        if time.monotonic() - _RUNTIME_CREDS_TS > _CREDS_TTL_SECONDS:
            _clear_creds_cache()


def _touch_creds_cache() -> None:
    global _RUNTIME_CREDS_TS
    _RUNTIME_CREDS_TS = time.monotonic()


def _is_empty_or_null(val: str | None) -> bool:
    if val is None:
        return True
    cleaned = val.strip()
    return not cleaned or cleaned.lower() in ("null", "none", "~")


def _ssl_verify() -> bool | str:
    """優先注入作業系統憑證庫 (truststore) 以相容企業 SSL 代理，失敗時降級至 certifi。"""
    try:
        import truststore

        truststore.inject_into_ssl()
        return True
    except ImportError:
        return certifi.where()


def _create_http_client() -> httpx.AsyncClient:
    """建立具備標準逾時、重導向限制與系統 TLS 憑證驗證之非同步 HTTP 客戶端。"""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(NETWORK_TIMEOUT_SEC),
        max_redirects=MAX_REDIRECTS,
        follow_redirects=True,
        verify=_ssl_verify(),
    )


def _redact_output(text: str, active_token: str = "") -> str:
    """遮罩文字中的私鑰、內嵌帳密 URL、API Token 與敏感鍵值對。"""
    if not text:
        return text
    out = text.replace(active_token, "[REDACTED_TOKEN]") if active_token else text
    out = _REDACT_PEM_RE.sub("[REDACTED_PRIVATE_KEY]", out)
    out = _REDACT_URL_RE.sub(r"\1[REDACTED]@", out)
    out = _REDACT_TOKEN_RE.sub("[REDACTED_TOKEN]", out)
    out = _REDACT_AUTH_BEARER_RE.sub(r"\1[REDACTED]", out)
    return _REDACT_KV_RE.sub(r"\1[REDACTED]", out)


def _detect_outbound_secret(body: str, active_token: str = "") -> str | None:
    """掃描準備發送至外部 GitHub 的留言內容，偵測高風險機密以防憑證外洩。"""
    if active_token and active_token in body:
        return "目前使用中的 GITHUB_TOKEN"
    if _REDACT_PEM_RE.search(body):
        return "PEM 私鑰區塊"
    if _REDACT_TOKEN_RE.search(body):
        return "API 金鑰或 Personal Access Token"
    if _REDACT_URL_RE.search(body):
        return "包含內嵌帳密的連線 URL"
    if _REDACT_AUTH_BEARER_RE.search(body):
        return "Authorization 認證標頭憑證"
    return None


def _truncate_output(text: str, hint: str) -> tuple[str, bool]:
    """限制單次輸出不超過 MAX_OUTPUT_CHARS，超限時附加引導提示。"""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    notice = f"\n\n... [輸出已截斷：超過單次 {MAX_OUTPUT_CHARS} 字元上限。{hint}]"
    return text[:MAX_OUTPUT_CHARS] + notice, True


def _get_audit_log_path() -> Path:
    """解析集中化稽核日誌 (agent.log) 路徑。"""
    env_log = (os.getenv("POCKET_AGENT_LOG_FILE") or "").strip()
    if env_log:
        return Path(env_log).expanduser().resolve()
    try:
        from pocket_agent.system.paths import log_dir

        return log_dir() / "agent.log"
    except ImportError:
        src_dir = Path(__file__).resolve().parent.parent / "src"
        if src_dir.is_dir() and str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            try:
                from pocket_agent.system.paths import log_dir

                return log_dir() / "agent.log"
            except ImportError:
                pass
        return Path.home() / ".pocket-agent" / "logs" / "agent.log"


def _audit_log(
    tool_name: str,
    command: str,
    *,
    target: str = "",
    status: str = "OK",
    detail: str = "",
    active_token: str = "",
) -> None:
    """將查詢、留言寫入與攔截事件寫入集中化 agent.log 供稽核使用。"""
    try:
        log_path = _get_audit_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        now_t = time.time()
        lt = time.localtime(now_t)
        ms = int((now_t - int(now_t)) * 1000)
        ts = f"{time.strftime('%Y-%m-%d %H:%M:%S', lt)},{ms:03d}"
        safe_cmd = " ".join(_redact_output(command, active_token).splitlines()).strip()
        safe_target = " ".join(_redact_output(target, active_token).splitlines()).strip()
        safe_detail = " ".join(_redact_output(detail, active_token).splitlines()).strip()
        parts = [f"{ts} [AUDIT] [MCP:github] tool={tool_name} status={status}"]
        if safe_target:
            parts.append(f"target={safe_target}")
        if safe_cmd:
            parts.append(f"command={safe_cmd!r}")
        if safe_detail:
            parts.append(f"detail={safe_detail!r}")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(" ".join(parts) + "\n")
    except Exception:
        pass


def _tty_secret_prompt(prompt_text: str) -> str:
    """當未設定 GITHUB_TOKEN 時，透過安全終端機隱藏詢問（不讀取 MCP stdin）。"""
    err_msg = "[CONN_AUTH] 缺少 GITHUB_TOKEN 憑證。請於 mcp_config.yaml 的 env 區段設定 GITHUB_TOKEN。"
    if os.name == "nt" and sys.stdin is not sys.__stdin__:
        raise _ConnAuthError(err_msg)
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt_text)
        except (getpass.GetPassWarning, EOFError, OSError) as exc:
            raise _ConnAuthError(err_msg) from exc


def _resolve_token(github_token: str = "") -> str:
    """依「動態注入 → 環境變數 → 記憶體快取 → 終端機詢問」順序解析 GITHUB_TOKEN。"""
    _ensure_creds_ttl()
    raw = github_token.strip().strip("'\"") if github_token else ""

    if _is_empty_or_null(raw):
        env_val = os.getenv("GITHUB_TOKEN")
        if not _is_empty_or_null(env_val):
            raw = (env_val or "").strip().strip("'\"")
        elif not _is_empty_or_null(_RUNTIME_CREDS.get("GITHUB_TOKEN")):
            raw = _RUNTIME_CREDS["GITHUB_TOKEN"]
        else:
            prompt_label = "請輸入 GitHub Personal Access Token (GITHUB_TOKEN): "
            raw = _tty_secret_prompt(prompt_label).strip().strip("'\"")

    if _is_empty_or_null(raw):
        _clear_creds_cache()
        msg = "[CONN_AUTH] 未提供有效的 GITHUB_TOKEN。請於 mcp_config.yaml 的 env 區段設定 GITHUB_TOKEN。"
        raise _ConnAuthError(msg)

    if any(ch in raw for ch in ("\r", "\n", "\x00")):
        _clear_creds_cache()
        raise _ConnAuthError("[CONN_AUTH] GITHUB_TOKEN 包含無效的控制字元。")

    _RUNTIME_CREDS["GITHUB_TOKEN"] = raw
    _touch_creds_cache()
    return raw


def get_headers(github_token: str = "") -> dict[str, str]:
    """建立 GitHub REST API 標準請求標頭。"""
    token = _resolve_token(github_token)
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "PocketAgent-GitHub-MCP",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _validate_repo_segment(value: str, field_name: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned or cleaned in (".", "..") or not _SEGMENT_RE.match(cleaned):
        msg = f"無效的 {field_name} 名稱：{value!r}（僅允許英數字、點、底線與連字號，不可為 . 或 ..）。"
        raise ValueError(msg)
    return cleaned


def check_repo_allowed(owner: str, repo: str) -> tuple[str, str]:
    """驗證 owner/repo 格式合法性，並核對 GITHUB_ALLOWED_REPOS 白名單（若有設定）。"""
    clean_owner = _validate_repo_segment(owner, "owner")
    clean_repo = _validate_repo_segment(repo, "repo")
    allowed = os.getenv("GITHUB_ALLOWED_REPOS")
    if allowed is not None and allowed.strip() != "":
        allowed_list = [r.strip().lower() for r in allowed.split(",") if r.strip()]
        target = f"{clean_owner}/{clean_repo}".lower()
        if target not in allowed_list:
            raise PermissionError(f"拒絕存取：{target} 不在允許的 GITHUB_ALLOWED_REPOS 白名單內。")
    return clean_owner, clean_repo


def _validate_positive_int(value: int, field_name: str, *, max_val: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"無效的 {field_name} 參數：{value!r}（必須為正整數）。")
    if max_val is not None and value > max_val:
        raise ValueError(f"無效的 {field_name} 參數：{value!r}（範圍須為 1 ~ {max_val}）。")
    return value


def _format_http_error(exc: httpx.HTTPStatusError, active_token: str = "") -> tuple[str, str]:
    """將 GitHub HTTP 狀態碼轉換為明確且不含機密的中文訊息與稽核狀態碼。"""
    status_code = exc.response.status_code
    api_msg = ""
    try:
        payload = exc.response.json()
        if isinstance(payload, dict) and payload.get("message"):
            api_msg = _redact_output(str(payload["message"]), active_token)[:200]
    except Exception:
        api_msg = ""

    if status_code == 401:
        _clear_creds_cache()
        msg = "[CONN_AUTH] GitHub 認證失敗 (HTTP 401)：Token 無效或已過期，已清除記憶體快取。"
        return msg, "AUTH_FAIL"
    if status_code == 403:
        remaining = exc.response.headers.get("x-ratelimit-remaining", "")
        if remaining == "0" or "rate limit" in api_msg.lower():
            reset_ts = exc.response.headers.get("x-ratelimit-reset", "")
            reset_note = f"，重置時間戳：{reset_ts}" if reset_ts else ""
            return f"GitHub API 已達速率限制 (HTTP 403 Rate Limit Exceeded{reset_note})。", "ERROR"
        suffix = f"（{api_msg}）" if api_msg else ""
        return f"GitHub 拒絕存取 (HTTP 403)：Token 權限不足或資源受保護{suffix}。", "AUTH_FAIL"
    if status_code == 404:
        msg = "找不到指定的儲存庫、Issue 或 Pull Request，或 Token 無權存取該資源 (HTTP 404)。"
        return msg, "ERROR"
    suffix = f"：{api_msg}" if api_msg else ""
    return f"GitHub API 回傳錯誤 (HTTP {status_code}){suffix}", "ERROR"


def _handle_tool_error(
    tool_name: str,
    cmd_desc: str,
    target: str,
    exc: Exception,
    action_label: str,
    active_token: str = "",
) -> str:
    """統一記錄失敗稽核日誌並格式化為 [ERROR] 字串回傳。"""
    if isinstance(exc, PermissionError):
        _audit_log(tool_name, cmd_desc, target=target, status="BLOCKED", detail=str(exc))
        return f"[ERROR] {exc}"
    if isinstance(exc, _ConnAuthError):
        _audit_log(tool_name, cmd_desc, target=target, status="AUTH_FAIL", detail=str(exc))
        return f"[ERROR] {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        msg, status_label = _format_http_error(exc, active_token)
        _audit_log(
            tool_name,
            cmd_desc,
            target=target,
            status=status_label,
            detail=msg,
            active_token=active_token,
        )
        return f"[ERROR] {action_label}：{msg}"
    safe_err = _redact_output(str(exc), active_token)
    _audit_log(
        tool_name,
        cmd_desc,
        target=target,
        status="ERROR",
        detail=safe_err,
        active_token=active_token,
    )
    return f"[ERROR] {action_label}：{safe_err}"


def _extract_login(item: dict) -> str:
    user_obj = item.get("user")
    if isinstance(user_obj, dict) and user_obj.get("login"):
        return str(user_obj["login"])
    return "ghost"


@mcp.tool()
async def github_list_prs(
    owner: str,
    repo: str,
    state: str = "open",
    per_page: int = 30,
    page: int = 1,
    github_token: str = "",
) -> str:
    """列出指定儲存庫的 Pull Requests（支援 state 與分頁查詢）。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"GET pulls state={state} per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = check_repo_allowed(owner, repo)
        target = f"{clean_owner}/{clean_repo}"
        norm_state = (state or "").strip().lower()
        if norm_state not in _ALLOWED_PR_STATES:
            raise ValueError(f"無效的 state 參數：{state!r}（僅允許 open、closed 或 all）。")
        clean_per_page = _validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = _validate_positive_int(page, "page")

        headers = get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"
        url = f"{GITHUB_API_BASE}/repos/{safe_path}/pulls"

        async with _create_http_client() as client:
            resp = await client.get(
                url,
                headers=headers,
                params={"state": norm_state, "per_page": clean_per_page, "page": clean_page},
            )
            resp.raise_for_status()
            prs = resp.json()

        if not isinstance(prs, list) or not prs:
            _audit_log("github_list_prs", cmd_desc, target=target, status="OK", active_token=active_token)
            return f"目前沒有狀態為 {norm_state} 的 Pull Requests（第 {clean_page} 頁）。"

        lines: list[str] = []
        for pr in prs:
            if not isinstance(pr, dict):
                continue
            num = pr.get("number", "?")
            title = (pr.get("title") or "").strip()
            lines.append(f"#{num} {title} ({_extract_login(pr)})")

        redacted = _redact_output("\n".join(lines), active_token)
        output, was_truncated = _truncate_output(redacted, "請縮小 per_page 或搭配 page 分頁檢視。")
        _audit_log(
            "github_list_prs",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(lines)}",
            active_token=active_token,
        )
        return output
    except Exception as exc:
        return _handle_tool_error("github_list_prs", cmd_desc, target, exc, "無法取得 PR 列表", active_token)


@mcp.tool()
async def github_read_issue(
    owner: str,
    repo: str,
    issue_number: int,
    github_token: str = "",
) -> str:
    """讀取指定的 Issue 或 Pull Request 標題、狀態與內文。"""
    active_token = ""
    target = f"{owner}/{repo}#{issue_number}"
    cmd_desc = f"GET issues/{issue_number}"
    try:
        clean_owner, clean_repo = check_repo_allowed(owner, repo)
        clean_num = _validate_positive_int(issue_number, "issue_number")
        target = f"{clean_owner}/{clean_repo}#{clean_num}"

        headers = get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"
        url = f"{GITHUB_API_BASE}/repos/{safe_path}/issues/{clean_num}"

        async with _create_http_client() as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        if not isinstance(data, dict):
            raise ValueError("GitHub API 回傳的 Issue 資料格式異常。")

        title = data.get("title") or ""
        issue_state = data.get("state") or ""
        author = _extract_login(data)
        issue_body = data.get("body") or ""
        raw_output = f"標題：{title}\n作者：{author}\n狀態：{issue_state}\n\n{issue_body}"

        redacted = _redact_output(raw_output, active_token)
        output, was_truncated = _truncate_output(redacted, "內文過長已自動截斷前段。")
        _audit_log(
            "github_read_issue",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            active_token=active_token,
        )
        return output
    except Exception as exc:
        return _handle_tool_error("github_read_issue", cmd_desc, target, exc, "無法取得 Issue 內容", active_token)


@mcp.tool()
async def github_create_comment(
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
    github_token: str = "",
) -> str:
    """在指定的 Issue 或 Pull Request 新增留言。"""
    active_token = ""
    target = f"{owner}/{repo}#{issue_number}"
    cmd_desc = f"POST issues/{issue_number}/comments"
    try:
        clean_owner, clean_repo = check_repo_allowed(owner, repo)
        clean_num = _validate_positive_int(issue_number, "issue_number")
        target = f"{clean_owner}/{clean_repo}#{clean_num}"

        if not isinstance(body, str) or not body.strip():
            raise ValueError("留言內容 (body) 不可為空白。")

        headers = get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")

        secret_reason = _detect_outbound_secret(body, active_token)
        if secret_reason:
            _audit_log(
                "github_create_comment",
                cmd_desc,
                target=target,
                status="BLOCKED",
                detail=f"outbound_secret_detected:{secret_reason}",
                active_token=active_token,
            )
            return f"[ERROR] 安全攔截：留言內容疑似包含機密（{secret_reason}），已阻斷外發以防外洩。"

        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"
        url = f"{GITHUB_API_BASE}/repos/{safe_path}/issues/{clean_num}/comments"
        async with _create_http_client() as client:
            resp = await client.post(url, headers=headers, json={"body": body})
            resp.raise_for_status()

        _audit_log(
            "github_create_comment",
            cmd_desc,
            target=target,
            status="OK",
            detail=f"chars={len(body)}",
            active_token=active_token,
        )
        return f"成功在 {clean_owner}/{clean_repo}#{clean_num} 新增留言。"
    except Exception as exc:
        return _handle_tool_error("github_create_comment", cmd_desc, target, exc, "無法新增留言", active_token)


if __name__ == "__main__":
    mcp.run()

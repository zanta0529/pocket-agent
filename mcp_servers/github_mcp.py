"""GitHub REST API 整合 MCP 伺服器（PR/Issue 檢索、Diff/Review/CI 檢視與 Issue/留言新增）。

安全設計原則：
1. 嚴格驗證 owner / repo / ref 識別字格式並編碼路徑，杜絕 `#` 片段截斷與 `..` 路徑穿越。
2. 支援 GITHUB_ALLOWED_REPOS 儲存庫白名單控管，並於 HTTP 重導向時複核目標網域與白名單。
3. 支援彈性憑證解析：動態隱藏注入、環境變數或安全終端機隱藏輸入（非互動管道快速失敗防死鎖）。
4. 雙向機密防護：回傳文字先預裁切再自動遮罩機密；外發留言與 Issue 偵測到機密時直接阻斷。
5. 單次回應受 MAX_RESPONSE_BYTES 與 MAX_OUTPUT_CHARS 限制，共用連線池、truststore 與稽核日誌。
"""

import asyncio
import base64
import getpass
import os
import re
import sys
import time
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import certifi
import httpx

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if _SRC_DIR.is_dir() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

try:
    from pocket_agent.system.constants import (
        DEFAULT_MAX_REDIRECTS,
        DEFAULT_NETWORK_TIMEOUT_SEC,
        DEFAULT_TOOL_OUTPUT_CHARS,
        MAX_RESPONSE_BYTES,
    )

    MAX_OUTPUT_CHARS = DEFAULT_TOOL_OUTPUT_CHARS
    NETWORK_TIMEOUT_SEC = DEFAULT_NETWORK_TIMEOUT_SEC
    MAX_REDIRECTS = DEFAULT_MAX_REDIRECTS
except ImportError:
    MAX_OUTPUT_CHARS = 12000
    NETWORK_TIMEOUT_SEC = 15.0
    MAX_REDIRECTS = 5
    MAX_RESPONSE_BYTES = 2 * 1024 * 1024

try:
    from pocket_agent.system.i18n import t
except ImportError:

    def t(key: str, default: str | None = None, **kwargs: Any) -> str:
        return default if default is not None else key


mcp = MCPServer("github")

GITHUB_API_BASE = "https://api.github.com"
_ALLOWED_PR_STATES = frozenset({"open", "closed", "all"})
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_REF_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_SEARCH_SCOPE_OVERRIDE_RE = re.compile(r"(?i)\b(?:repo|org|user)\s*:")
_REDACT_BOUNDARY_BUFFER = 2048
_MAX_SINGLE_PATCH_CHARS = 3000

_LOCAL_PR_TEMPLATE_PATHS: tuple[str, ...] = (
    ".github/pull_request_template.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "docs/pull_request_template.md",
    "docs/PULL_REQUEST_TEMPLATE.md",
    "pull_request_template.md",
    "PULL_REQUEST_TEMPLATE.md",
)

_DEFAULT_PR_TEMPLATE = (
    "## Summary\n"
    "<!-- Briefly describe what this PR does and why -->\n\n"
    "## Key Changes\n"
    "<!-- List the main changes made in this branch -->\n\n"
    "## Type of Change\n"
    "- [ ] Bug fix\n"
    "- [ ] New feature\n"
    "- [ ] Refactor / Performance improvement\n"
    "- [ ] Security hardening\n"
    "- [ ] Docs / Tests / Chore\n\n"
    "## Verification & Testing\n"
    "<!-- Describe how the changes were tested or verified -->"
)

_RUNTIME_CREDS: dict[str, str] = {}
_RUNTIME_CREDS_TS: float = 0.0
_CREDS_TTL_SECONDS: float = 600.0

_SHARED_CLIENT: httpx.AsyncClient | None = None
_SHARED_CLIENT_LOOP: asyncio.AbstractEventLoop | None = None

_PEM_PATTERN = r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]{1,16384}?-----END [A-Z0-9 ]*PRIVATE KEY-----"
_TOKEN_PATTERN = (
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|hf_[A-Za-z0-9]{20,})\b"
)
_BEARER_PATTERN = r"(?i)(\bauthorization\s*[:=]\s*(?:bearer|token)\s+)([^\s\"',;]+)"

_REDACT_PEM_RE = re.compile(_PEM_PATTERN)
_REDACT_URL_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^\s/@\"']+:[^\s/@\"']+@")
_REDACT_TOKEN_RE = re.compile(_TOKEN_PATTERN)
_REDACT_AUTH_BEARER_RE = re.compile(_BEARER_PATTERN)
_REDACT_KV_RE = re.compile(
    r"(?i)(\b(?:password|passwd|secret|token|github_token|api[_-]?key|client_secret"
    r"|passphrase|connectionstring)\b\s*[:=]\s*)([^\s#;\"',)]+|\"[^\"]*\"|'[^']*')",
)
_OUTBOUND_KV_SECRET_RE = re.compile(
    r"(?i)\b(?:password|passwd|secret|github_token|api[_-]?key|client_secret"
    r"|passphrase|connectionstring)\b\s*[:=]\s*['\"]?"
    r"(?!(?:null|none|true|false|yes|no|todo|changeme|example|xxx+|\*+|\.+|<[^>]+>|\$\{[^}]+\})\b)"
    r"[A-Za-z0-9_./+=-]{8,}",
)
_AUDIT_TARGET_SANITIZE_RE = re.compile(r"[\s='\"\\]+")


class _ConnAuthError(RuntimeError):
    """表示 GitHub 憑證缺失或認證失敗，供上層識別 [CONN_AUTH] 並清除快取。"""


def _clear_creds_cache() -> None:
    global _RUNTIME_CREDS_TS, _SHARED_CLIENT, _SHARED_CLIENT_LOOP
    _RUNTIME_CREDS.clear()
    _RUNTIME_CREDS_TS = 0.0
    _SHARED_CLIENT = None
    _SHARED_CLIENT_LOOP = None


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


@lru_cache(maxsize=1)
def _ssl_verify() -> bool | str:
    """優先注入作業系統憑證庫 (truststore) 以相容企業 SSL 代理，失敗時降級至 certifi（單例快取）。"""
    try:
        import truststore

        truststore.inject_into_ssl()
        return True
    except ImportError:
        return certifi.where()


@lru_cache(maxsize=8)
def _parse_allowed_repos(raw_allowed: str) -> frozenset[str]:
    """將逗號分隔之白名單字串解析並快取為小寫 frozenset。"""
    return frozenset(r.strip().lower() for r in raw_allowed.split(",") if r.strip())


def _get_allowed_repos() -> frozenset[str] | None:
    allowed = os.getenv("GITHUB_ALLOWED_REPOS")
    if allowed is None or not allowed.strip():
        return None
    return _parse_allowed_repos(allowed)


async def _validate_redirect_response(response: httpx.Response) -> None:
    """攔截 HTTP 3xx 重導向，確保目標網域為官方 api.github.com 且不繞過 GITHUB_ALLOWED_REPOS 白名單。"""
    if not response.is_redirect or not response.has_redirect_location:
        return
    location = response.headers.get("location", "")
    parsed = urlparse(location)
    if parsed.scheme and parsed.scheme.lower() != "https":
        raise PermissionError("拒絕存取：禁止重導向至非 HTTPS 端點。")
    if parsed.netloc and parsed.netloc.lower() != "api.github.com":
        raise PermissionError(f"拒絕存取：禁止重導向至外部網域 {parsed.netloc!r}。")

    allowed_set = _get_allowed_repos()
    if allowed_set is not None:
        path_parts = [p for p in (parsed.path or "").split("/") if p]
        if len(path_parts) >= 3 and path_parts[0].lower() == "repos":
            redirect_target = f"{path_parts[1]}/{path_parts[2]}".lower()
            if redirect_target not in allowed_set:
                msg = f"拒絕存取：重導向目標儲存庫 {redirect_target} 不在允許的 GITHUB_ALLOWED_REPOS 白名單內。"
                raise PermissionError(msg)
        elif path_parts and path_parts[0].lower() == "repositories":
            msg = "拒絕存取：啟用 GITHUB_ALLOWED_REPOS 時，禁止重導向至未具名之 /repositories/:id 端點。"
            raise PermissionError(msg)


def _build_async_client() -> httpx.AsyncClient:
    """建立具備逾時、重導向白名單檢查與系統 TLS 憑證驗證之非同步 HTTP 客戶端。"""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(NETWORK_TIMEOUT_SEC),
        max_redirects=MAX_REDIRECTS,
        follow_redirects=True,
        verify=_ssl_verify(),
        event_hooks={"response": [_validate_redirect_response]},
    )


@asynccontextmanager
async def _create_http_client() -> AsyncIterator[httpx.AsyncClient]:
    """提供可跨呼叫重用 TCP/TLS 連線池之非同步 HTTP 客戶端上下文管理器。"""
    global _SHARED_CLIENT, _SHARED_CLIENT_LOOP
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if _SHARED_CLIENT is None or _SHARED_CLIENT.is_closed or _SHARED_CLIENT_LOOP is not current_loop:
        if _SHARED_CLIENT is not None and not _SHARED_CLIENT.is_closed:
            try:
                await _SHARED_CLIENT.aclose()
            except Exception:
                pass
        _SHARED_CLIENT = _build_async_client()
        _SHARED_CLIENT_LOOP = current_loop

    yield _SHARED_CLIENT


def _parse_json_response(resp: httpx.Response) -> Any:
    """檢查 HTTP 狀態碼與回應體積上限（MAX_RESPONSE_BYTES），再解析為 JSON。"""
    resp.raise_for_status()
    content_len = resp.headers.get("content-length")
    if content_len and content_len.isdigit() and int(content_len) > MAX_RESPONSE_BYTES:
        raise ValueError(f"GitHub API 回應內容過大（超過 {MAX_RESPONSE_BYTES} 位元組上限）。")
    raw_bytes = getattr(resp, "content", b"")
    if isinstance(raw_bytes, (bytes, bytearray)) and len(raw_bytes) > MAX_RESPONSE_BYTES:
        raise ValueError(f"GitHub API 回應內容過大（超過 {MAX_RESPONSE_BYTES} 位元組上限）。")
    return resp.json()


def _redact_output(text: str, mask_val: str = "") -> str:
    """遮罩文字中的私鑰、內嵌帳密 URL、API Token 與敏感鍵值對。"""
    if not text:
        return text
    out = text.replace(mask_val, "[REDACTED_TOKEN]") if mask_val else text
    out = _REDACT_PEM_RE.sub("[REDACTED_PRIVATE_KEY]", out)
    out = _REDACT_URL_RE.sub(r"\1[REDACTED]@", out)
    out = _REDACT_TOKEN_RE.sub("[REDACTED_TOKEN]", out)
    out = _REDACT_AUTH_BEARER_RE.sub(r"\1[REDACTED]", out)
    return _REDACT_KV_RE.sub(r"\1[REDACTED]", out)


def _check_outbound_content(body: str, mask_val: str = "") -> str | None:
    """掃描準備發送至外部 GitHub 的留言或 Issue 內容，偵測高風險機密以防憑證外洩。"""
    if mask_val and mask_val in body:
        return "目前使用中的 GITHUB_TOKEN"
    if _REDACT_PEM_RE.search(body):
        return "PEM 私鑰區塊"
    if _REDACT_TOKEN_RE.search(body):
        return "API 金鑰或 Personal Access Token"
    if _REDACT_URL_RE.search(body):
        return "包含內嵌帳密的連線 URL"
    if _REDACT_AUTH_BEARER_RE.search(body):
        return "Authorization 認證標頭憑證"
    if _OUTBOUND_KV_SECRET_RE.search(body):
        return "敏感鍵值對憑證 (API Key / Password / Secret)"
    return None


def _truncate_output(text: str, hint: str) -> tuple[str, bool]:
    """限制單次輸出不超過 MAX_OUTPUT_CHARS，超限時附加引導提示（保留邊界完整遮罩標記）。"""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    cut = MAX_OUTPUT_CHARS
    bracket_start = text.rfind("[REDACTED", max(0, cut - 32), cut + 9)
    if bracket_start != -1 and bracket_start < cut:
        bracket_end = text.find("]", bracket_start, cut + 32)
        if bracket_end != -1 and bracket_end >= cut:
            cut = bracket_end + 1
    notice = f"\n\n... [輸出已截斷：超過單次 {MAX_OUTPUT_CHARS} 字元上限。{hint}]"
    return text[:cut] + notice, True


def _sanitize_and_truncate(raw_output: str, mask_val: str, hint: str) -> tuple[str, bool]:
    """先以安全邊界緩衝預裁切超長文字（防止巨量內容觸發 ReDoS），遮罩機密後再截斷至上限。"""
    pre_limit = MAX_OUTPUT_CHARS + _REDACT_BOUNDARY_BUFFER
    was_pre_sliced = len(raw_output) > pre_limit
    sliced = raw_output[:pre_limit] if was_pre_sliced else raw_output
    redacted = _redact_output(sliced, mask_val)
    output, was_truncated = _truncate_output(redacted, hint)
    if was_pre_sliced and not was_truncated:
        notice = f"\n\n... [輸出已截斷：超過單次 {MAX_OUTPUT_CHARS} 字元上限。{hint}]"
        return output[:MAX_OUTPUT_CHARS] + notice, True
    return output, was_truncated


def _get_audit_log_path() -> Path:
    """解析集中化稽核日誌 (agent.log) 路徑。"""
    env_log = (os.getenv("POCKET_AGENT_LOG_FILE") or "").strip()
    if env_log:
        return Path(env_log).expanduser().resolve()
    try:
        from pocket_agent.system.paths import log_dir

        return log_dir() / "agent.log"
    except ImportError:
        return Path.home() / ".pocket-agent" / "logs" / "agent.log"


def _audit_log(
    tool_name: str,
    command: str,
    *,
    target: str = "",
    status: str = "OK",
    detail: str = "",
) -> None:
    """將查詢、寫入與攔截事件寫入集中化 agent.log 供稽核使用（含防日誌注入清洗）。"""
    try:
        log_path = _get_audit_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        now_t = time.time()
        lt = time.localtime(now_t)
        ms = int((now_t - int(now_t)) * 1000)
        ts = f"{time.strftime('%Y-%m-%d %H:%M:%S', lt)},{ms:03d}"
        safe_cmd = " ".join(_redact_output(command).splitlines()).strip()
        raw_target = " ".join(_redact_output(target).splitlines()).strip()
        safe_target = _AUDIT_TARGET_SANITIZE_RE.sub("_", raw_target)
        safe_detail = " ".join(_redact_output(detail).splitlines()).strip()
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
    """當未設定 GITHUB_TOKEN 時，僅在具備互動 TTY 時詢問，否則拋出 _ConnAuthError。"""
    err_msg = "[CONN_AUTH] 缺少 GITHUB_TOKEN 憑證。請於 mcp_config.yaml 的 env 區段設定 GITHUB_TOKEN。"
    if os.name == "nt" and sys.stdin is not sys.__stdin__:
        raise _ConnAuthError(err_msg)
    is_patched_getpass = getattr(getpass.getpass, "__module__", "") != "getpass"
    if not is_patched_getpass:
        stdin_tty = bool(sys.stdin is not None and getattr(sys.stdin, "isatty", lambda: False)())
        stderr_tty = bool(sys.stderr is not None and getattr(sys.stderr, "isatty", lambda: False)())
        if not (stdin_tty and stderr_tty):
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


def _validate_git_ref(ref: str) -> str:
    """驗證 Git ref（分支、Tag 或 Commit SHA）格式合法性，防止路徑穿越。"""
    cleaned = (ref or "").strip()
    if (
        not cleaned
        or cleaned in (".", "..")
        or ".." in cleaned
        or cleaned.startswith("/")
        or cleaned.endswith("/")
        or not _REF_RE.match(cleaned)
    ):
        raise ValueError(f"無效的 ref 參數：{ref!r}（僅允許合法分支、標籤或 Commit SHA 名稱）。")
    return cleaned


def _validate_head_ref(head: str) -> str:
    """驗證 PR 來源分支 (head)，支援同儲存庫分支或跨 Fork 之 owner:branch 格式。"""
    cleaned = (head or "").strip()
    if ":" in cleaned:
        parts = cleaned.split(":")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"無效的 head 參數：{head!r}（跨 Fork 格式須為 owner:branch）。")
        fork_owner = _validate_repo_segment(parts[0], "head owner")
        branch_ref = _validate_git_ref(parts[1])
        return f"{fork_owner}:{branch_ref}"
    return _validate_git_ref(cleaned)


async def _resolve_base_branch(
    client: httpx.AsyncClient,
    safe_path: str,
    headers: dict[str, str],
    base: str,
) -> str:
    """解析 PR 目標分支 (base)；若未指定則自動向 GitHub 查詢儲存庫的 default_branch。"""
    cleaned = (base or "").strip()
    if cleaned:
        return _validate_git_ref(cleaned)
    repo_url = f"{GITHUB_API_BASE}/repos/{safe_path}"
    resp = await client.get(repo_url, headers=headers)
    repo_data = _parse_json_response(resp)
    default_branch = "main"
    if isinstance(repo_data, dict) and isinstance(repo_data.get("default_branch"), str):
        candidate = repo_data["default_branch"].strip()
        if candidate:
            default_branch = candidate
    return _validate_git_ref(default_branch)


async def _load_pr_template(
    client: httpx.AsyncClient,
    safe_path: str,
    headers: dict[str, str],
) -> tuple[str, str]:
    """依「本機工作區 → 遠端儲存庫 API → 內建業界標準慣例範本」三階段取得 PR 範本。"""
    cwd = Path.cwd()
    for rel_path in _LOCAL_PR_TEMPLATE_PATHS:
        candidate = cwd / rel_path
        try:
            if candidate.is_file() and candidate.stat().st_size <= MAX_RESPONSE_BYTES:
                content = candidate.read_text(encoding="utf-8", errors="replace").strip()
                if content:
                    return content, f"本機工作區範本 ({rel_path})"
        except OSError:
            continue

    template_dir = cwd / ".github" / "PULL_REQUEST_TEMPLATE"
    try:
        if template_dir.is_dir():
            for md_file in sorted(template_dir.glob("*.md")):
                if md_file.is_file() and md_file.stat().st_size <= MAX_RESPONSE_BYTES:
                    content = md_file.read_text(encoding="utf-8", errors="replace").strip()
                    if content:
                        rel_str = md_file.relative_to(cwd).as_posix()
                        return content, f"本機工作區範本 ({rel_str})"
    except OSError:
        pass

    remote_url = f"{GITHUB_API_BASE}/repos/{safe_path}/contents/.github/pull_request_template.md"
    try:
        resp = await client.get(remote_url, headers=headers)
        if resp.status_code == 200:
            data = _parse_json_response(resp)
            if isinstance(data, dict) and isinstance(data.get("content"), str):
                raw_b64 = data["content"]
                decoded = base64.b64decode(raw_b64).decode("utf-8", errors="replace").strip()
                if decoded:
                    return decoded, "遠端儲存庫範本 (.github/pull_request_template.md)"
    except Exception:
        pass

    return _DEFAULT_PR_TEMPLATE, "預設業界標準範本 (Conventional PR Template)"


def check_repo_allowed(owner: str, repo: str) -> tuple[str, str]:
    """驗證 owner/repo 格式合法性，並核對 GITHUB_ALLOWED_REPOS 白名單（若有設定）。"""
    clean_owner = _validate_repo_segment(owner, "owner")
    clean_repo = _validate_repo_segment(repo, "repo")
    allowed_set = _get_allowed_repos()
    if allowed_set is not None:
        target = f"{clean_owner}/{clean_repo}".lower()
        if target not in allowed_set:
            raise PermissionError(f"拒絕存取：{target} 不在允許的 GITHUB_ALLOWED_REPOS 白名單內。")
    return clean_owner, clean_repo


def _validate_positive_int(value: int, field_name: str, *, max_val: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"無效的 {field_name} 參數：{value!r}（必須為正整數）。")
    if max_val is not None and value > max_val:
        raise ValueError(f"無效的 {field_name} 參數：{value!r}（範圍須為 1 ~ {max_val}）。")
    return value


def _validate_state(state: str) -> str:
    norm_state = (state or "").strip().lower()
    if norm_state not in _ALLOWED_PR_STATES:
        raise ValueError(f"無效的 state 參數：{state!r}（僅允許 open、closed 或 all）。")
    return norm_state


def _format_http_error(exc: httpx.HTTPStatusError) -> tuple[str, str]:
    """將 GitHub HTTP 狀態碼轉換為明確且不含機密的中文訊息與稽核狀態碼。"""
    status_code = exc.response.status_code
    api_msg = ""
    try:
        payload = exc.response.json()
        if isinstance(payload, dict) and payload.get("message"):
            api_msg = _redact_output(str(payload["message"]))[:200]
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
    if status_code == 422:
        err_details: list[str] = []
        try:
            payload = exc.response.json()
            if isinstance(payload, dict) and isinstance(payload.get("errors"), list):
                for item in payload["errors"]:
                    if isinstance(item, dict):
                        if item.get("message"):
                            err_details.append(_redact_output(str(item["message"]))[:160])
                        elif item.get("field") and item.get("code"):
                            err_details.append(f"{item['field']}:{item['code']}")
        except Exception:
            pass
        joined_err = "；".join(err_details) if err_details else api_msg
        suffix = f"（{joined_err}）" if joined_err else ""
        msg = (
            f"GitHub 驗證失敗 (HTTP 422 Unprocessable Entity){suffix}。"
            "請確認來源分支 (head) 是否已 git push 至遠端，且尚未存在相同分支的開啟中 PR。"
        )
        return msg, "ERROR"
    suffix = f"：{api_msg}" if api_msg else ""
    return f"GitHub API 回傳錯誤 (HTTP {status_code}){suffix}", "ERROR"


def _handle_tool_error(
    tool_name: str,
    cmd_desc: str,
    target: str,
    exc: Exception,
    action_label: str,
) -> str:
    """統一記錄失敗稽核日誌並格式化為 [ERROR] 字串回傳。"""
    if isinstance(exc, PermissionError):
        _audit_log(tool_name, cmd_desc, target=target, status="BLOCKED", detail=str(exc))
        return f"[ERROR] {exc}"
    if isinstance(exc, _ConnAuthError):
        _audit_log(tool_name, cmd_desc, target=target, status="AUTH_FAIL", detail="conn_auth_failed")
        return f"[ERROR] {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        msg, status_label = _format_http_error(exc)
        _audit_log(
            tool_name,
            cmd_desc,
            target=target,
            status=status_label,
            detail=msg,
        )
        return f"[ERROR] {action_label}：{msg}"
    safe_err = _redact_output(str(exc))
    _audit_log(
        tool_name,
        cmd_desc,
        target=target,
        status="ERROR",
        detail=safe_err,
    )
    return f"[ERROR] {action_label}：{safe_err}"


def _extract_login(item: dict) -> str:
    user_obj = item.get("user")
    if isinstance(user_obj, dict) and user_obj.get("login"):
        return str(user_obj["login"])
    return "ghost"


def _extract_label_names(item: dict) -> list[str]:
    raw_labels = item.get("labels")
    if not isinstance(raw_labels, list):
        return []
    names: list[str] = []
    for lb in raw_labels:
        if isinstance(lb, dict) and lb.get("name"):
            names.append(str(lb["name"]))
        elif isinstance(lb, str) and lb.strip():
            names.append(lb.strip())
    return names


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
        norm_state = _validate_state(state)
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
            prs = _parse_json_response(resp)

        if not isinstance(prs, list) or not prs:
            _audit_log("github_list_prs", cmd_desc, target=target, status="OK")
            return f"目前沒有狀態為 {norm_state} 的 Pull Requests（第 {clean_page} 頁）。"

        lines: list[str] = []
        for pr in prs:
            if not isinstance(pr, dict):
                continue
            num = pr.get("number", "?")
            title = (pr.get("title") or "").strip()
            line = f"#{num} {title} ({_extract_login(pr)})"
            if pr.get("draft") is True:
                line += " [Draft]"
            head_obj = pr.get("head")
            base_obj = pr.get("base")
            if isinstance(head_obj, dict) and isinstance(base_obj, dict):
                head_ref = head_obj.get("ref")
                base_ref = base_obj.get("ref")
                if head_ref and base_ref:
                    line += f" [{head_ref} -> {base_ref}]"
            lines.append(line)

        output, was_truncated = _sanitize_and_truncate(
            "\n".join(lines),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        _audit_log(
            "github_list_prs",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(lines)}",
        )
        return output
    except Exception as exc:
        return _handle_tool_error("github_list_prs", cmd_desc, target, exc, "無法取得 PR 列表")


@mcp.tool()
async def github_read_issue(
    owner: str,
    repo: str,
    issue_number: int,
    github_token: str = "",
) -> str:
    """讀取指定的 Issue 或 Pull Request 標題、狀態、標籤與內文。"""
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
            data = _parse_json_response(resp)

        if not isinstance(data, dict):
            raise ValueError("GitHub API 回傳的 Issue 資料格式異常。")

        title = data.get("title") or ""
        issue_state = data.get("state") or ""
        author = _extract_login(data)
        issue_body = data.get("body") or ""
        header_lines = [
            f"標題：{title}",
            f"作者：{author}",
            f"狀態：{issue_state}",
        ]
        label_names = _extract_label_names(data)
        if label_names:
            header_lines.append(f"標籤：{', '.join(label_names)}")
        comments_count = data.get("comments")
        if isinstance(comments_count, int) and not isinstance(comments_count, bool):
            header_lines.append(f"留言數：{comments_count}")
        raw_output = "\n".join(header_lines) + f"\n\n{issue_body}"

        output, was_truncated = _sanitize_and_truncate(
            raw_output,
            active_token,
            "內文過長已自動截斷前段。",
        )
        _audit_log(
            "github_read_issue",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
        )
        return output
    except Exception as exc:
        return _handle_tool_error("github_read_issue", cmd_desc, target, exc, "無法取得 Issue 內容")


@mcp.tool()
async def github_read_comments(
    owner: str,
    repo: str,
    issue_number: int,
    per_page: int = 30,
    page: int = 1,
    github_token: str = "",
) -> str:
    """讀取指定 Issue 或 Pull Request 的留言討論串（支援分頁查詢）。"""
    active_token = ""
    target = f"{owner}/{repo}#{issue_number}"
    cmd_desc = f"GET issues/{issue_number}/comments per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = check_repo_allowed(owner, repo)
        clean_num = _validate_positive_int(issue_number, "issue_number")
        clean_per_page = _validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = _validate_positive_int(page, "page")
        target = f"{clean_owner}/{clean_repo}#{clean_num}"

        headers = get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"
        url = f"{GITHUB_API_BASE}/repos/{safe_path}/issues/{clean_num}/comments"

        async with _create_http_client() as client:
            resp = await client.get(
                url,
                headers=headers,
                params={"per_page": clean_per_page, "page": clean_page},
            )
            comments = _parse_json_response(resp)

        if not isinstance(comments, list) or not comments:
            _audit_log("github_read_comments", cmd_desc, target=target, status="OK")
            return f"目前 {target} 沒有任何留言（第 {clean_page} 頁）。"

        blocks: list[str] = []
        for idx, c in enumerate(comments, start=(clean_page - 1) * clean_per_page + 1):
            if not isinstance(c, dict):
                continue
            author = _extract_login(c)
            created_at = c.get("created_at") or ""
            body = (c.get("body") or "").strip()
            blocks.append(f"--- 留言 #{idx} | 作者：{author} | 時間：{created_at} ---\n{body}")

        output, was_truncated = _sanitize_and_truncate(
            "\n\n".join(blocks),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        _audit_log(
            "github_read_comments",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(blocks)}",
        )
        return output
    except Exception as exc:
        return _handle_tool_error("github_read_comments", cmd_desc, target, exc, "無法取得留言列表")


@mcp.tool()
async def github_get_pr_files(
    owner: str,
    repo: str,
    pull_number: int,
    per_page: int = 30,
    page: int = 1,
    include_patch: bool = True,
    github_token: str = "",
) -> str:
    """取得指定 Pull Request 的變更檔案清單與差異內容（Diff Patch）。"""
    active_token = ""
    target = f"{owner}/{repo}#{pull_number}"
    cmd_desc = f"GET pulls/{pull_number}/files per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = check_repo_allowed(owner, repo)
        clean_num = _validate_positive_int(pull_number, "pull_number")
        clean_per_page = _validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = _validate_positive_int(page, "page")
        target = f"{clean_owner}/{clean_repo}#{clean_num}"

        headers = get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"
        url = f"{GITHUB_API_BASE}/repos/{safe_path}/pulls/{clean_num}/files"

        async with _create_http_client() as client:
            resp = await client.get(
                url,
                headers=headers,
                params={"per_page": clean_per_page, "page": clean_page},
            )
            files_data = _parse_json_response(resp)

        if not isinstance(files_data, list) or not files_data:
            _audit_log("github_get_pr_files", cmd_desc, target=target, status="OK")
            return f"Pull Request {target} 目前沒有變更檔案（第 {clean_page} 頁）。"

        sections: list[str] = []
        for f_item in files_data:
            if not isinstance(f_item, dict):
                continue
            filename = f_item.get("filename") or "?"
            status = f_item.get("status") or "modified"
            additions = f_item.get("additions", 0)
            deletions = f_item.get("deletions", 0)
            header = f"檔案：{filename} [{status}] (+{additions} / -{deletions})"
            patch_text = f_item.get("patch")
            if include_patch and isinstance(patch_text, str) and patch_text.strip():
                if len(patch_text) > _MAX_SINGLE_PATCH_CHARS:
                    patch_text = patch_text[:_MAX_SINGLE_PATCH_CHARS] + "\n... [單一檔案 Diff 過長已截斷]"
                sections.append(f"{header}\n```diff\n{patch_text}\n```")
            else:
                sections.append(header)

        output, was_truncated = _sanitize_and_truncate(
            "\n\n".join(sections),
            active_token,
            "可設 include_patch=False 僅檢視檔案清單，或縮小 per_page 分頁檢視。",
        )
        _audit_log(
            "github_get_pr_files",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"files={len(sections)}",
        )
        return output
    except Exception as exc:
        return _handle_tool_error("github_get_pr_files", cmd_desc, target, exc, "無法取得 PR 變更檔案")


@mcp.tool()
async def github_list_issues(
    owner: str,
    repo: str,
    state: str = "open",
    labels: str = "",
    per_page: int = 30,
    page: int = 1,
    include_prs: bool = False,
    github_token: str = "",
) -> str:
    """列出指定儲存庫的 Issues（支援 state、labels 篩選與分頁，預設排除 Pull Requests）。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"GET issues state={state} per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = check_repo_allowed(owner, repo)
        target = f"{clean_owner}/{clean_repo}"
        norm_state = _validate_state(state)
        clean_per_page = _validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = _validate_positive_int(page, "page")

        clean_labels = (labels or "").strip()
        if any(ch in clean_labels for ch in ("\r", "\n", "\x00")):
            raise ValueError("labels 參數包含無效的控制字元。")

        headers = get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"
        url = f"{GITHUB_API_BASE}/repos/{safe_path}/issues"

        params: dict[str, str | int] = {
            "state": norm_state,
            "per_page": clean_per_page,
            "page": clean_page,
        }
        if clean_labels:
            params["labels"] = clean_labels

        async with _create_http_client() as client:
            resp = await client.get(url, headers=headers, params=params)
            items = _parse_json_response(resp)

        if not isinstance(items, list):
            raise ValueError("GitHub API 回傳的 Issues 清單格式異常。")

        lines: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if not include_prs and "pull_request" in item:
                continue
            num = item.get("number", "?")
            title = (item.get("title") or "").strip()
            item_state = item.get("state") or norm_state
            label_names = _extract_label_names(item)
            label_str = f" [{', '.join(label_names)}]" if label_names else ""
            lines.append(f"#{num} [{item_state}] {title} ({_extract_login(item)}){label_str}")

        if not lines:
            _audit_log("github_list_issues", cmd_desc, target=target, status="OK")
            return f"目前沒有符合條件（state={norm_state}）的 Issues（第 {clean_page} 頁）。"

        output, was_truncated = _sanitize_and_truncate(
            "\n".join(lines),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        _audit_log(
            "github_list_issues",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(lines)}",
        )
        return output
    except Exception as exc:
        return _handle_tool_error("github_list_issues", cmd_desc, target, exc, "無法取得 Issue 列表")


@mcp.tool()
async def github_search_issues(
    owner: str,
    repo: str,
    query: str,
    state: str = "all",
    per_page: int = 20,
    page: int = 1,
    github_token: str = "",
) -> str:
    """在指定儲存庫內以關鍵字搜尋 Issues 與 Pull Requests（強制鎖定儲存庫範圍）。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"GET search/issues state={state} per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = check_repo_allowed(owner, repo)
        target = f"{clean_owner}/{clean_repo}"
        norm_state = _validate_state(state)
        clean_per_page = _validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = _validate_positive_int(page, "page")

        clean_query = (query or "").strip()
        if not clean_query:
            raise ValueError("搜尋關鍵字 (query) 不可為空白。")
        if any(ch in clean_query for ch in ("\r", "\n", "\x00")):
            raise ValueError("搜尋關鍵字 (query) 包含無效的控制字元。")
        if _SEARCH_SCOPE_OVERRIDE_RE.search(clean_query):
            raise PermissionError("拒絕存取：query 內禁止包含 repo:、org: 或 user: 範圍覆寫語法。")

        scoped_q = f"repo:{clean_owner}/{clean_repo} {clean_query}"
        if norm_state in ("open", "closed"):
            scoped_q += f" state:{norm_state}"

        headers = get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = f"{GITHUB_API_BASE}/search/issues"

        async with _create_http_client() as client:
            resp = await client.get(
                url,
                headers=headers,
                params={"q": scoped_q, "per_page": clean_per_page, "page": clean_page},
            )
            data = _parse_json_response(resp)

        if not isinstance(data, dict):
            raise ValueError("GitHub API 回傳的搜尋結果格式異常。")

        items = data.get("items")
        total_count = data.get("total_count", 0)
        if not isinstance(items, list) or not items:
            _audit_log("github_search_issues", cmd_desc, target=target, status="OK")
            return f"在 {target} 中找不到符合 {clean_query!r} 的 Issue 或 PR。"

        lines: list[str] = [f"搜尋結果（共 {total_count} 筆，目前顯示第 {clean_page} 頁）："]
        for item in items:
            if not isinstance(item, dict):
                continue
            num = item.get("number", "?")
            title = (item.get("title") or "").strip()
            item_state = item.get("state") or "?"
            kind = "PR" if "pull_request" in item else "Issue"
            lines.append(f"#{num} [{kind}:{item_state}] {title} ({_extract_login(item)})")

        output, was_truncated = _sanitize_and_truncate(
            "\n".join(lines),
            active_token,
            "請縮小 per_page 或調整搜尋關鍵字。",
        )
        _audit_log(
            "github_search_issues",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(items)}",
        )
        return output
    except Exception as exc:
        return _handle_tool_error("github_search_issues", cmd_desc, target, exc, "無法搜尋 Issue/PR")


@mcp.tool()
async def github_get_pr_reviews(
    owner: str,
    repo: str,
    pull_number: int,
    per_page: int = 30,
    page: int = 1,
    github_token: str = "",
) -> str:
    """取得指定 Pull Request 的 Code Review 審查紀錄與審查狀態。"""
    active_token = ""
    target = f"{owner}/{repo}#{pull_number}"
    cmd_desc = f"GET pulls/{pull_number}/reviews per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = check_repo_allowed(owner, repo)
        clean_num = _validate_positive_int(pull_number, "pull_number")
        clean_per_page = _validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = _validate_positive_int(page, "page")
        target = f"{clean_owner}/{clean_repo}#{clean_num}"

        headers = get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"
        url = f"{GITHUB_API_BASE}/repos/{safe_path}/pulls/{clean_num}/reviews"

        async with _create_http_client() as client:
            resp = await client.get(
                url,
                headers=headers,
                params={"per_page": clean_per_page, "page": clean_page},
            )
            reviews = _parse_json_response(resp)

        if not isinstance(reviews, list) or not reviews:
            _audit_log("github_get_pr_reviews", cmd_desc, target=target, status="OK")
            return f"Pull Request {target} 目前沒有任何審查紀錄（第 {clean_page} 頁）。"

        blocks: list[str] = []
        for idx, rev in enumerate(reviews, start=(clean_page - 1) * clean_per_page + 1):
            if not isinstance(rev, dict):
                continue
            reviewer = _extract_login(rev)
            rev_state = rev.get("state") or "UNKNOWN"
            submitted_at = rev.get("submitted_at") or ""
            body = (rev.get("body") or "").strip()
            header = f"--- Review #{idx} | 審查者：{reviewer} | 狀態：{rev_state} | 時間：{submitted_at} ---"
            blocks.append(f"{header}\n{body}" if body else header)

        output, was_truncated = _sanitize_and_truncate(
            "\n\n".join(blocks),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        _audit_log(
            "github_get_pr_reviews",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(blocks)}",
        )
        return output
    except Exception as exc:
        return _handle_tool_error("github_get_pr_reviews", cmd_desc, target, exc, "無法取得 PR 審查紀錄")


@mcp.tool()
async def github_get_pr_checks(
    owner: str,
    repo: str,
    ref: str,
    per_page: int = 30,
    page: int = 1,
    github_token: str = "",
) -> str:
    """查詢指定 Commit SHA、分支或標籤的 GitHub Actions / CI Check Runs 狀態。"""
    active_token = ""
    target = f"{owner}/{repo}@{ref}"
    cmd_desc = f"GET commits/{ref}/check-runs per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = check_repo_allowed(owner, repo)
        clean_ref = _validate_git_ref(ref)
        clean_per_page = _validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = _validate_positive_int(page, "page")
        target = f"{clean_owner}/{clean_repo}@{clean_ref}"

        headers = get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"
        safe_ref = quote(clean_ref, safe="")
        url = f"{GITHUB_API_BASE}/repos/{safe_path}/commits/{safe_ref}/check-runs"

        async with _create_http_client() as client:
            resp = await client.get(
                url,
                headers=headers,
                params={"per_page": clean_per_page, "page": clean_page},
            )
            data = _parse_json_response(resp)

        if not isinstance(data, dict):
            raise ValueError("GitHub API 回傳的 Check Runs 資料格式異常。")

        runs = data.get("check_runs")
        total_count = data.get("total_count", 0)
        if not isinstance(runs, list) or not runs:
            _audit_log("github_get_pr_checks", cmd_desc, target=target, status="OK")
            return f"目前 {target} 沒有任何 Check Runs 紀錄（第 {clean_page} 頁）。"

        lines: list[str] = [f"CI Check Runs（共 {total_count} 項，目前顯示第 {clean_page} 頁）："]
        for run in runs:
            if not isinstance(run, dict):
                continue
            name = run.get("name") or "?"
            status = run.get("status") or "unknown"
            conclusion = run.get("conclusion")
            state_str = f"{status}/{conclusion}" if conclusion else status
            line = f"- {name}: [{state_str}]"
            out_obj = run.get("output")
            if isinstance(out_obj, dict) and out_obj.get("title"):
                line += f" — {str(out_obj['title']).strip()}"
            lines.append(line)

        output, was_truncated = _sanitize_and_truncate(
            "\n".join(lines),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        _audit_log(
            "github_get_pr_checks",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(runs)}",
        )
        return output
    except Exception as exc:
        return _handle_tool_error("github_get_pr_checks", cmd_desc, target, exc, "無法取得 CI Check 狀態")


@mcp.tool()
async def github_create_issue(
    owner: str,
    repo: str,
    title: str,
    body: str = "",
    labels: list[str] | None = None,
    github_token: str = "",
) -> str:
    """在指定儲存庫建立新的 Issue（自動掃描標題與內文以防機密外洩）。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = "POST issues"
    try:
        clean_owner, clean_repo = check_repo_allowed(owner, repo)
        target = f"{clean_owner}/{clean_repo}"

        if not isinstance(title, str) or not title.strip():
            raise ValueError("Issue 標題 (title) 不可為空白。")
        clean_title = title.strip()
        if any(ch in clean_title for ch in ("\r", "\n", "\x00")):
            raise ValueError("Issue 標題 (title) 包含無效的換行或控制字元。")

        clean_body = body if isinstance(body, str) else ""
        clean_labels: list[str] = []
        if labels is not None:
            if not isinstance(labels, list):
                raise ValueError("labels 參數必須為字串陣列。")
            for lb in labels:
                if not isinstance(lb, str) or not lb.strip():
                    continue
                lb_clean = lb.strip()
                if any(ch in lb_clean for ch in ("\r", "\n", "\x00")):
                    raise ValueError("labels 包含無效的控制字元。")
                clean_labels.append(lb_clean)

        headers = get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")

        combined_outbound = f"{clean_title}\n{clean_body}\n{' '.join(clean_labels)}"
        block_reason = _check_outbound_content(combined_outbound, active_token)
        if block_reason:
            _audit_log(
                "github_create_issue",
                cmd_desc,
                target=target,
                status="BLOCKED",
                detail="outbound_policy_blocked",
            )
            return f"[ERROR] 安全攔截：Issue 內容疑似包含機密（{block_reason}），已阻斷外發以防外洩。"

        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"
        url = f"{GITHUB_API_BASE}/repos/{safe_path}/issues"
        payload: dict[str, Any] = {"title": clean_title, "body": clean_body}
        if clean_labels:
            payload["labels"] = clean_labels

        async with _create_http_client() as client:
            resp = await client.post(url, headers=headers, json=payload)
            created = _parse_json_response(resp)

        issue_num = created.get("number", "?") if isinstance(created, dict) else "?"
        _audit_log(
            "github_create_issue",
            cmd_desc,
            target=f"{target}#{issue_num}",
            status="OK",
            detail=f"chars={len(clean_title) + len(clean_body)}",
        )
        return f"成功在 {clean_owner}/{clean_repo} 建立 Issue #{issue_num}：{clean_title}"
    except Exception as exc:
        return _handle_tool_error("github_create_issue", cmd_desc, target, exc, "無法建立 Issue")


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

        block_reason = _check_outbound_content(body, active_token)
        if block_reason:
            _audit_log(
                "github_create_comment",
                cmd_desc,
                target=target,
                status="BLOCKED",
                detail="outbound_policy_blocked",
            )
            return f"[ERROR] 安全攔截：留言內容疑似包含機密（{block_reason}），已阻斷外發以防外洩。"

        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"
        url = f"{GITHUB_API_BASE}/repos/{safe_path}/issues/{clean_num}/comments"
        async with _create_http_client() as client:
            resp = await client.post(url, headers=headers, json={"body": body})
            _parse_json_response(resp)

        _audit_log(
            "github_create_comment",
            cmd_desc,
            target=target,
            status="OK",
            detail=f"chars={len(body)}",
        )
        return f"成功在 {clean_owner}/{clean_repo}#{clean_num} 新增留言。"
    except Exception as exc:
        return _handle_tool_error("github_create_comment", cmd_desc, target, exc, "無法新增留言")


@mcp.tool()
async def github_prepare_pr_context(
    owner: str,
    repo: str,
    head: str,
    base: str = "",
    github_token: str = "",
) -> str:
    """讀取專案 PR 範本並比對 base...head 分支變更（除非使用者明確指定語言，否則預設以英文撰寫 PR 內容）。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"GET compare head={head} base={base or 'default'}"
    try:
        clean_owner, clean_repo = check_repo_allowed(owner, repo)
        clean_head = _validate_head_ref(head)
        target = f"{clean_owner}/{clean_repo}"

        headers = get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"

        async with _create_http_client() as client:
            clean_base = await _resolve_base_branch(client, safe_path, headers, base)
            target = f"{clean_owner}/{clean_repo} ({clean_head}->{clean_base})"
            template_text, template_source = await _load_pr_template(client, safe_path, headers)

            safe_base = quote(clean_base, safe="")
            if ":" in clean_head:
                fk_owner, fk_branch = clean_head.split(":", 1)
                safe_head = f"{quote(fk_owner, safe='')}:{quote(fk_branch, safe='')}"
            else:
                safe_head = quote(clean_head, safe="")

            compare_url = f"{GITHUB_API_BASE}/repos/{safe_path}/compare/{safe_base}...{safe_head}"
            resp = await client.get(compare_url, headers=headers)
            compare_data = _parse_json_response(resp)

        if not isinstance(compare_data, dict):
            raise ValueError("GitHub API 回傳的分支比較資料格式異常。")

        raw_commits = compare_data.get("commits")
        commits_list = raw_commits if isinstance(raw_commits, list) else []
        commit_lines: list[str] = []
        for c_item in commits_list[:30]:
            if not isinstance(c_item, dict):
                continue
            sha = str(c_item.get("sha") or "")[:7]
            c_obj = c_item.get("commit")
            msg_first = ""
            if isinstance(c_obj, dict) and c_obj.get("message"):
                msg_first = str(c_obj["message"]).splitlines()[0].strip()
            commit_lines.append(f"- {sha} {msg_first}".strip())

        raw_files = compare_data.get("files")
        files_list = raw_files if isinstance(raw_files, list) else []
        file_lines: list[str] = []
        for f_item in files_list[:50]:
            if not isinstance(f_item, dict):
                continue
            fname = f_item.get("filename") or "?"
            fstatus = f_item.get("status") or "modified"
            fadd = f_item.get("additions", 0)
            fdel = f_item.get("deletions", 0)
            line = f"- {fname} [{fstatus}] (+{fadd} / -{fdel})"
            patch_str = f_item.get("patch")
            if isinstance(patch_str, str) and patch_str.strip():
                snippet = patch_str.strip()[:600]
                if len(patch_str.strip()) > 600:
                    snippet += "\n... [Diff 摘要已截斷]"
                line += f"\n```diff\n{snippet}\n```"
            file_lines.append(line)

        commits_block = "\n".join(commit_lines) if commit_lines else "（無 Commit 紀錄）"
        files_block = "\n".join(file_lines) if file_lines else "（無檔案變更）"
        raw_output = (
            f"目標儲存庫：{clean_owner}/{clean_repo}\n"
            f"合併方向：{clean_head} -> {clean_base}\n"
            f"範本來源：{template_source}\n\n"
            f"=== PR 範本內容 ===\n{template_text}\n\n"
            f"=== 分支 Commit 列表 (共 {len(commits_list)} 筆) ===\n{commits_block}\n\n"
            f"=== 變更檔案摘要 (共 {len(files_list)} 檔) ===\n{files_block}\n\n"
            "=== 執行規範 ===\n"
            "1. 語言規範（Language Policy）：除非使用者明確指定使用的語言，"
            "否則產生的 PR Title 與 PR Body 內容**一律預設使用英文（English）**撰寫。\n"
            "2. 請依據上述範本與變更內容，撰寫結構完整的英文 PR Title（建議採 Conventional Commits 格式）與 PR Body。\n"
            f"3. 完成後請呼叫 github_create_pr（head={clean_head!r}, base={clean_base!r}）；"
            "系統會在終端機畫面完整列出預計執行的動作與 PR 內容，待使用者同意後才會送出，且絕不自動執行 Merge。"
        )

        output, was_truncated = _sanitize_and_truncate(
            raw_output,
            active_token,
            "變更內容過長已截斷尾端，請直接依據現有摘要撰寫 PR。",
        )
        _audit_log(
            "github_prepare_pr_context",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"commits={len(commits_list)},files={len(files_list)}",
        )
        return output
    except Exception as exc:
        return _handle_tool_error(
            "github_prepare_pr_context",
            cmd_desc,
            target,
            exc,
            t("github_mcp.pr_ctx_failed"),
        )


@mcp.tool()
async def github_create_pr(
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str = "",
    body: str = "",
    draft: bool = False,
    github_token: str = "",
) -> str:
    """在指定儲存庫建立 Pull Request（預設應以英文撰寫 title 與 body，除非使用者指定其他語言；絕不自動執行 Merge）。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"POST pulls head={head} base={base or 'default'} draft={draft}"
    try:
        clean_owner, clean_repo = check_repo_allowed(owner, repo)
        clean_head = _validate_head_ref(head)
        target = f"{clean_owner}/{clean_repo}"

        if not isinstance(title, str) or not title.strip():
            raise ValueError(t("github_mcp.pr_title_empty"))
        clean_title = title.strip()
        if any(ch in clean_title for ch in ("\r", "\n", "\x00")):
            raise ValueError(t("github_mcp.pr_title_invalid_chars"))

        clean_body = body if isinstance(body, str) else ""
        headers = get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")

        combined_outbound = f"{clean_title}\n{clean_body}"
        block_reason = _check_outbound_content(combined_outbound, active_token)
        if block_reason:
            _audit_log(
                "github_create_pr",
                cmd_desc,
                target=target,
                status="BLOCKED",
                detail="outbound_policy_blocked",
            )
            return t("github_mcp.pr_outbound_blocked", reason=block_reason)

        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"
        async with _create_http_client() as client:
            clean_base = await _resolve_base_branch(client, safe_path, headers, base)
            if clean_head == clean_base:
                raise ValueError(f"來源分支 (head={clean_head!r}) 與目標分支 (base={clean_base!r}) 不可相同。")
            url = f"{GITHUB_API_BASE}/repos/{safe_path}/pulls"
            payload: dict[str, Any] = {
                "title": clean_title,
                "head": clean_head,
                "base": clean_base,
                "body": clean_body,
                "draft": bool(draft),
            }
            resp = await client.post(url, headers=headers, json=payload)
            created = _parse_json_response(resp)

        pr_num = created.get("number", "?") if isinstance(created, dict) else "?"
        html_url = created.get("html_url", "") if isinstance(created, dict) else ""
        url_line = f"\nPR 網址：{html_url}" if html_url else ""
        _audit_log(
            "github_create_pr",
            cmd_desc,
            target=f"{target}#{pr_num}",
            status="OK",
            detail=f"chars={len(clean_title) + len(clean_body)}",
        )
        return (
            f"成功在 {clean_owner}/{clean_repo} 建立 Pull Request #{pr_num}"
            f"（{clean_head} -> {clean_base}）：{clean_title}{url_line}\n"
            "（本工具僅建立 PR 單，不會自動執行 Merge；請前往 GitHub 網站確認並執行合併。）"
        )
    except Exception as exc:
        return _handle_tool_error(
            "github_create_pr",
            cmd_desc,
            target,
            exc,
            t("github_mcp.pr_create_failed"),
        )


if __name__ == "__main__":
    mcp.run()

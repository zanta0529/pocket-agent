"""GitHub MCP 安全策略模組（Strategy Pattern）：白名單、路徑防穿越、重導向驗證與雙向機密防護。"""

import os
import re
from functools import lru_cache
from urllib.parse import unquote

import httpx

from mcp_servers._shared import CONSTANTS, DefaultRedactionPolicy

DEFAULT_API_BASE = "https://api.github.com"

MAX_OUTPUT_CHARS = CONSTANTS.max_output_chars
MAX_RESPONSE_BYTES = CONSTANTS.max_response_bytes

_ALLOWED_PR_STATES = frozenset({"open", "closed", "all"})
_ALLOWED_UPDATE_STATES = frozenset({"open", "closed"})
_ALLOWED_STATE_REASONS = frozenset({"completed", "not_planned", "reopened"})
ALLOWED_DIRECTIONS = frozenset({"asc", "desc"})
ALLOWED_PR_SORTS = frozenset({"created", "updated", "popularity", "long-running"})
ALLOWED_ISSUE_SORTS = frozenset({"created", "updated", "comments"})
_ALLOWED_REVIEW_EVENTS = frozenset({"COMMENT", "REQUEST_CHANGES"})
ALLOWED_WORKFLOW_STATUSES = frozenset(
    {
        "completed",
        "action_required",
        "cancelled",
        "failure",
        "neutral",
        "skipped",
        "stale",
        "success",
        "timed_out",
        "in_progress",
        "queued",
        "requested",
        "waiting",
        "pending",
    }
)
ALLOWED_JOB_FILTERS = frozenset({"latest", "all"})
ALLOWED_DIFF_SIDES = frozenset({"LEFT", "RIGHT"})
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_REF_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_SEARCH_SCOPE_OVERRIDE_RE = re.compile(r"(?i)\b(?:repo|org|user)\s*:")
_REDACT_BOUNDARY_BUFFER = 20480
_MAX_ERROR_OUTPUT_CHARS = 1500

_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]{1,16384}?-----END [A-Z0-9 ]*PRIVATE KEY-----")
_URL_CREDS_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]{0,31}://)[^\s/@\"':]+:[^\s/@\"']+@")
_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|hf_[A-Za-z0-9]{20,})\b"
)
_AUTH_BEARER_RE = re.compile(r"(?i)(\bauthorization\s*[:=]\s*(?:bearer|token)\s+)([^\s\"',;]+)")
_SENSITIVE_KV_KEYS = (
    r"(?:password|passwd|secret(?:[_-]?key)?|github_token|(?:access|auth|refresh|bearer|session|api)[_-]token"
    r"|api[_-]?key|client_secret|passphrase|connectionstring)"
)
_OUTBOUND_PLACEHOLDER_RE = (
    r"(?!(?:null|none|true|false|yes|no|todo|changeme|example|xxx+|\*+|\.+|<[^>]+>|\$\{[^}]+\})"
    r"(?:\b|['\"\s,}\]]|$))"
)
_OUTBOUND_KV_SECRET_RE = re.compile(
    r"(?i)(?:"
    rf"(?:['\"]{_SENSITIVE_KV_KEYS}['\"]|\b{_SENSITIVE_KV_KEYS}\b)\s*[:=]\s*['\"]?"
    r"|(?:['\"]token['\"]\s*[:=]\s*['\"]?|\btoken\s*=\s*['\"]?"
    r"|\btoken\s*:\s*['\"]|(?:^|\n)[ \t]*token\s*:\s*['\"]?)"
    rf"){_OUTBOUND_PLACEHOLDER_RE}[A-Za-z0-9_./+=-]{{8,}}",
)


class GitHubRedactionPolicy(DefaultRedactionPolicy):
    """GitHub 專用雙向機密防護策略（回傳遮罩截斷＋外發內容機密掃描）。"""

    def check_outbound_content(self, body: str, mask_val: str = "") -> str | None:
        """掃描準備發送至外部 GitHub 的留言、Issue 或 PR 內容，偵測高風險機密以防外洩。"""
        if mask_val and mask_val in body:
            return "目前使用中的 GITHUB_TOKEN"
        if "-----END " in body and _PEM_RE.search(body):
            return "PEM 私鑰區塊"
        if _TOKEN_RE.search(body):
            return "API 金鑰或 Personal Access Token"
        if "://" in body and "@" in body and _URL_CREDS_RE.search(body):
            return "包含內嵌帳密的連線 URL"
        if _AUTH_BEARER_RE.search(body):
            return "Authorization 認證標頭憑證"
        if _OUTBOUND_KV_SECRET_RE.search(body):
            return "敏感鍵值對憑證 (API Key / Password / Secret)"
        return None

    def redact_and_clip_patch(self, patch: str, max_chars: int, mask_val: str, notice: str) -> str:
        """先對完整單檔 Diff 執行機密遮罩，再套用安全字元上限截斷。"""
        return self.clip_text(self.redact(patch, extra_secret=mask_val), max_chars, notice)

    def sanitize_and_truncate(self, raw_output: str, mask_val: str, hint: str) -> tuple[str, bool]:
        """對受大小上限約束的完整輸出先遮罩再截斷；若觸發預裁切則捨棄無法判定安全性的尾段。"""
        pre_limit = MAX_RESPONSE_BYTES
        was_pre_sliced = pre_limit > 0 and len(raw_output) > pre_limit
        sliced = raw_output[:pre_limit] if was_pre_sliced else raw_output
        redacted = self.redact(sliced, extra_secret=mask_val)
        if was_pre_sliced:
            safe_end = max(0, len(redacted) - _REDACT_BOUNDARY_BUFFER)
            redacted = redacted[:safe_end]
        output, was_truncated = self.truncate_output(redacted, max_chars=MAX_OUTPUT_CHARS, hint=hint)
        if was_pre_sliced and not was_truncated:
            notice = f"\n\n... [輸出已截斷：超過單次 {MAX_OUTPUT_CHARS} 字元上限。{hint}]"
            return output + notice, True
        return output, was_truncated

    def clip_error_text(self, text: str, mask_val: str = "", limit: int = _MAX_ERROR_OUTPUT_CHARS) -> str:
        """遮罩敏感資訊並安全裁切錯誤訊息。"""
        return self.clip_text(self.redact(text, extra_secret=mask_val), limit)


GITHUB_REDACTION = GitHubRedactionPolicy()


@lru_cache(maxsize=8)
def _parse_allowed_repos(raw_allowed: str) -> frozenset[str]:
    return frozenset(r.strip().lower() for r in raw_allowed.split(",") if r.strip())


def get_allowed_repos() -> frozenset[str] | None:
    allowed = os.getenv("GITHUB_ALLOWED_REPOS")
    if allowed is None or not allowed.strip():
        return None
    return _parse_allowed_repos(allowed)


def validate_repo_segment(value: str, field_name: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned or cleaned in (".", "..") or not _SEGMENT_RE.match(cleaned):
        msg = f"無效的 {field_name} 名稱：{value!r}（僅允許英數字、點、底線與連字號，不可為 . 或 ..）。"
        raise ValueError(msg)
    return cleaned


def validate_git_ref(ref: str) -> str:
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


def validate_head_ref(head: str) -> str:
    """驗證 PR 來源分支 (head)，支援同儲存庫分支或跨 Fork 之 owner:branch 格式。"""
    cleaned = (head or "").strip()
    if ":" in cleaned:
        parts = cleaned.split(":")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"無效的 head 參數：{head!r}（跨 Fork 格式須為 owner:branch）。")
        fork_owner = validate_repo_segment(parts[0], "head owner")
        branch_ref = validate_git_ref(parts[1])
        return f"{fork_owner}:{branch_ref}"
    return validate_git_ref(cleaned)


def check_repo_allowed(owner: str, repo: str) -> tuple[str, str]:
    """驗證 owner/repo 格式合法性，並核對 GITHUB_ALLOWED_REPOS 白名單（若有設定）。"""
    clean_owner = validate_repo_segment(owner, "owner")
    clean_repo = validate_repo_segment(repo, "repo")
    allowed_set = get_allowed_repos()
    if allowed_set is not None:
        target = f"{clean_owner}/{clean_repo}".lower()
        if target not in allowed_set:
            raise PermissionError(f"拒絕存取：{target} 不在允許的 GITHUB_ALLOWED_REPOS 白名單內。")
    return clean_owner, clean_repo


def validate_state(state: str) -> str:
    norm_state = (state or "").strip().lower()
    if norm_state not in _ALLOWED_PR_STATES:
        raise ValueError(f"無效的 state 參數：{state!r}（僅允許 open、closed 或 all）。")
    return norm_state


def check_search_query_scope(query: str) -> str:
    clean_query = (query or "").strip()
    if not clean_query:
        raise ValueError("搜尋關鍵字 (query) 不可為空白。")
    if any(ch in clean_query for ch in ("\r", "\n", "\x00")):
        raise ValueError("搜尋關鍵字 (query) 包含無效的控制字元。")
    if _SEARCH_SCOPE_OVERRIDE_RE.search(clean_query):
        raise PermissionError("拒絕存取：query 內禁止包含 repo:、org: 或 user: 範圍覆寫語法。")
    return clean_query


def get_api_base_url() -> str:
    """取得並驗證 GitHub API 基底網址（預設官方端點，支援企業自架 GHES）。"""
    raw = (os.getenv("GITHUB_API_BASE_URL") or "").strip()
    if not raw:
        return DEFAULT_API_BASE
    if any(ch in raw for ch in ("\r", "\n", "\x00")):
        raise ValueError("GITHUB_API_BASE_URL 包含無效的控制字元。")
    try:
        parsed = httpx.URL(raw)
    except Exception as exc:
        raise ValueError(f"無效的 GITHUB_API_BASE_URL 格式：{raw!r}") from exc
    if parsed.scheme.lower() != "https":
        raise ValueError("無效的 GITHUB_API_BASE_URL：僅允許 HTTPS 協定。")
    if parsed.userinfo:
        raise ValueError("無效的 GITHUB_API_BASE_URL：禁止於網址內嵌使用者憑證資訊。")
    if not parsed.host:
        raise ValueError("無效的 GITHUB_API_BASE_URL：缺少主機名稱。")
    if parsed.query or parsed.fragment:
        raise ValueError("無效的 GITHUB_API_BASE_URL：不可包含查詢參數或片段。")
    norm_path = parsed.path.rstrip("/")
    return str(parsed.copy_with(path=norm_path))


def validate_redirect_url(target_url: httpx.URL) -> None:
    """驗證 HTTPX 解析後之實際重導向目標 URL，確保為受信任 HTTPS 端點且受白名單約束。"""
    expected = httpx.URL(get_api_base_url())
    if target_url.scheme.lower() != "https":
        raise PermissionError("拒絕存取：禁止重導向至非 HTTPS 端點。")
    if target_url.userinfo:
        raise PermissionError("拒絕存取：禁止重導向至含使用者憑證資訊之端點。")
    if target_url.host.lower() != expected.host.lower():
        raise PermissionError(f"拒絕存取：禁止重導向至外部網域 {target_url.host!r}。")
    expected_port = expected.port if expected.port is not None else 443
    target_port = target_url.port if target_url.port is not None else 443
    if target_port != expected_port:
        raise PermissionError(f"拒絕存取：禁止重導向至非標準連接埠 {target_url.port}。")

    allowed_set = get_allowed_repos()
    if allowed_set is None:
        return

    raw_path = target_url.raw_path.decode("ascii", errors="replace").split("?", 1)[0]
    raw_parts = [p for p in raw_path.split("/") if p]
    expected_prefix = [unquote(p).lower() for p in expected.path.split("/") if p]
    if expected_prefix:
        if len(raw_parts) < len(expected_prefix):
            raise PermissionError("拒絕存取：重導向路徑不符合設定之 API 前綴。")
        actual_prefix = [unquote(p).lower() for p in raw_parts[: len(expected_prefix)]]
        if actual_prefix != expected_prefix:
            raise PermissionError("拒絕存取：重導向路徑不符合設定之 API 前綴。")
        raw_parts = raw_parts[len(expected_prefix) :]

    if not raw_parts:
        raise PermissionError("拒絕存取：啟用 GITHUB_ALLOWED_REPOS 時，禁止重導向至無法確認儲存庫歸屬的路徑。")

    first_decoded = unquote(raw_parts[0]).lower()
    if first_decoded == "repositories":
        raise PermissionError("拒絕存取：啟用 GITHUB_ALLOWED_REPOS 時，禁止重導向至未具名之 /repositories/:id 端點。")

    if len(raw_parts) >= 3 and first_decoded == "repos":
        for seg in raw_parts:
            decoded_seg = unquote(seg)
            if "/" in decoded_seg or "\\" in decoded_seg or decoded_seg in (".", ".."):
                raise PermissionError("拒絕存取：重導向路徑包含無效的路徑穿越或編碼分隔字元。")
        owner_seg = unquote(raw_parts[1])
        repo_seg = unquote(raw_parts[2])
        if not _SEGMENT_RE.match(owner_seg) or not _SEGMENT_RE.match(repo_seg):
            raise PermissionError("拒絕存取：重導向目標儲存庫識別字格式無效。")
        redirect_target = f"{owner_seg}/{repo_seg}".lower()
        if redirect_target not in allowed_set:
            raise PermissionError(
                f"拒絕存取：重導向目標儲存庫 {redirect_target} 不在允許的 GITHUB_ALLOWED_REPOS 白名單內。"
            )
        return

    raise PermissionError("拒絕存取：啟用 GITHUB_ALLOWED_REPOS 時，禁止重導向至無法確認儲存庫歸屬的路徑。")


def validate_update_state(state: str) -> str:
    """驗證更新 PR 或 Issue 的目標狀態（僅允許 open 或 closed）。"""
    norm_state = (state or "").strip().lower()
    if norm_state not in _ALLOWED_UPDATE_STATES:
        raise ValueError(f"無效的 state 參數：{state!r}（更新時僅允許 open 或 closed）。")
    return norm_state


def validate_state_reason(reason: str) -> str:
    """驗證 Issue 關閉或重開原因（completed、not_planned 或 reopened）。"""
    norm_reason = (reason or "").strip().lower()
    if norm_reason not in _ALLOWED_STATE_REASONS:
        raise ValueError(f"無效的 state_reason 參數：{reason!r}（僅允許 completed、not_planned 或 reopened）。")
    return norm_reason


def validate_choice(value: str, allowed: frozenset[str], field_name: str) -> str:
    """驗證列舉字串參數（正規化為小寫）。"""
    norm_val = (value or "").strip().lower()
    if norm_val not in allowed:
        opts = "、".join(sorted(allowed))
        raise ValueError(f"無效的 {field_name} 參數：{value!r}（僅允許 {opts}）。")
    return norm_val


def validate_review_event(event: str) -> str:
    """驗證 PR Review 提交事件類型；基於安全政策禁止代理直接核准 (APPROVE)。"""
    norm_event = (event or "").strip().upper()
    if norm_event == "APPROVE":
        raise PermissionError(
            "拒絕存取：基於安全政策禁止代理直接提交 APPROVE 審查，"
            "請改用 COMMENT 或 REQUEST_CHANGES，並由人工於 GitHub 網站核准。"
        )
    if norm_event not in _ALLOWED_REVIEW_EVENTS:
        raise ValueError(f"無效的 event 參數：{event!r}（僅允許 COMMENT 或 REQUEST_CHANGES）。")
    return norm_event


def validate_repo_file_path(path: str, *, allow_empty: bool = True) -> str:
    """驗證儲存庫內的相對檔案或目錄路徑，防範路徑穿越與反斜線混淆。"""
    raw = (path or "").strip()
    if any(ch in raw for ch in ("\r", "\n", "\x00", "\\")):
        raise ValueError(f"無效的檔案路徑：{path!r}（包含非法字元或反斜線）。")
    cleaned = raw.strip("/")
    if not cleaned:
        if allow_empty:
            return ""
        raise ValueError("檔案路徑 (path) 不可為空白。")
    segments = cleaned.split("/")
    for seg in segments:
        if not seg or seg in (".", ".."):
            raise ValueError(f"無效的檔案路徑：{path!r}（禁止空白區段、. 或 ..）。")
    return "/".join(segments)


def validate_filter_text(value: str, field_name: str, *, max_len: int = 100) -> str:
    """驗證一般篩選文字參數，排除控制字元並限制長度。"""
    cleaned = (value or "").strip()
    if any(ch in cleaned for ch in ("\r", "\n", "\x00")):
        raise ValueError(f"{field_name} 參數包含無效的控制字元。")
    if len(cleaned) > max_len:
        raise ValueError(f"{field_name} 參數過長（上限 {max_len} 字元）。")
    return cleaned

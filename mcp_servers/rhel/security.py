"""RHEL MCP 安全驗證、遠端路徑守衛與多行 fail-closed 輸出遮罩策略（Strategy Pattern）。"""

import posixpath
import re
import shlex

from mcp_servers._shared import CONSTANTS
from mcp_servers._shared import validate_host as shared_validate_host

MAX_OUTPUT_CHARS = CONSTANTS.max_output_chars

_IDENT_RE = re.compile(r"^[a-zA-Z0-9_.@-]+$")
_SINCE_RE = re.compile(r"^[a-zA-Z0-9_.:+\- ]+$")
_SEARCH_RE = re.compile(r"^[a-zA-Z0-9_.:@/+\- ]+$")

VALID_SORT_KEYS = frozenset({"cpu", "mem", "memory"})
VALID_PRIORITIES = frozenset(
    {
        "emerg",
        "alert",
        "crit",
        "err",
        "warning",
        "notice",
        "info",
        "debug",
        "0",
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
    }
)

_ALLOWED_CONFIG_PREFIXES = (
    "/etc/",
    "/opt/",
    "/run/systemd/",
    "/srv/",
    "/usr/lib/systemd/",
    "/usr/local/etc/",
    "/usr/share/",
    "/var/log/",
)

_DENIED_PATH_KEYWORDS = (
    "shadow",
    "gshadow",
    "sudoers",
    "private",
    "secret",
    "key",
    "token",
    "password",
    "passwd-",
    "opasswd",
    ".ssh",
    ".env",
    "kubeconfig",
    "admin.conf",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "secure",
    "audit",
    "wtmp",
    "btmp",
    "lastlog",
    ".netrc",
)

_REDACT_KV_RE = re.compile(
    r"(?i)(\b(?:password|passwd|secret|token|api_key|apikey|client_secret|passphrase"
    r"|private_key|privatekey|preshared_key|presharedkey)\b"
    r"\s*[:=]\s*)([^\s#;\"']+|\"[^\"]*\"|'[^']*')"
)
_REDACT_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)")
_REDACT_SECRET_LINE_RE = re.compile(
    r"(?im)\b(?:[\w-]*(?:password|passwd|secret|token|passphrase|api[_-]?key)[\w-]*"
    r"|psk|authorization|database_url|aws_access_key_id|private_key|preshared_key)\b[^\r\n\x00]*"
)
_REDACT_URL_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@")


def validate_host(value: str) -> str:
    return shared_validate_host(value, "RHEL_HOST", bracket_ipv6=True)


def validate_identifier(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _IDENT_RE.match(clean):
        raise ValueError(f"非法的 {field_name} 參數格式（僅允許英數字、點、底線、連字號與 @，且不得以連字號開頭）")
    return clean


def validate_search_pattern(value: str, field_name: str, max_len: int = 50) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _SEARCH_RE.match(clean) or len(clean) > max_len:
        raise ValueError(
            f"非法的 {field_name} 參數格式（僅允許英數字、斜線、冒號、點、空白等安全符號，且限 {max_len} 字元以內）"
        )
    return clean


def validate_port(value: str) -> str:
    clean = value.strip()
    if not clean.isdigit() or not (1 <= int(clean) <= 65535):
        raise ValueError("非法的連接埠數值（必須為 1~65535 之間的整數）")
    return clean


def validate_pid(value: str | int) -> str:
    clean = str(value).strip()
    if not clean.isdigit() or int(clean) <= 0:
        raise ValueError("非法的行程 PID 數值（必須為正整數）")
    return clean


def validate_since(value: str) -> str:
    clean = value.strip()
    if not _SINCE_RE.match(clean) or len(clean) > 40:
        raise ValueError("非法的 since 時間格式（僅允許安全字元集且限 40 字元以內）")
    return clean


def validate_config_path(file_path: str) -> str:
    raw = file_path.strip()
    if not raw.startswith("/"):
        raise ValueError("設定檔路徑必須為絕對路徑（以 / 開頭）。")
    if ".." in raw.split("/"):
        raise ValueError("拒絕存取：路徑不得包含 '..' 跳躍符號。")

    norm = posixpath.normpath(raw)
    if not any(norm.startswith(prefix) or norm == prefix.rstrip("/") for prefix in _ALLOWED_CONFIG_PREFIXES):
        allowed_str = "、".join(_ALLOWED_CONFIG_PREFIXES)
        raise ValueError(f"拒絕存取：指定路徑不在允許的唯讀探查目錄白名單內（{allowed_str}）。")

    lower_path = norm.lower()
    for kw in _DENIED_PATH_KEYWORDS:
        if kw in lower_path:
            raise ValueError("拒絕存取：路徑包含敏感保護關鍵字，禁止讀取。")
    return norm


def remote_path_guard(path: str, *, directory: bool) -> str:
    """以同一套規則在遠端檢查實際解析後路徑。"""
    denied = "|".join(f"*{keyword}*" for keyword in _DENIED_PATH_KEYWORDS)
    allowed = [f"{prefix.rstrip('/')}/*" for prefix in _ALLOWED_CONFIG_PREFIXES]
    if directory:
        allowed.extend(prefix.rstrip("/") for prefix in _ALLOWED_CONFIG_PREFIXES)
    kind = "d" if directory else "f"
    return (
        f"real=$(realpath -e -- {shlex.quote(path)} 2>/dev/null) || "
        "{ echo '[ERROR] 路徑不存在或無法解析'; exit 1; }; "
        "lower=$(printf '%s' \"$real\" | LC_ALL=C tr '[:upper:]' '[:lower:]') || exit 1; "
        f'case "$lower" in {denied}) '
        "echo '[ERROR] 拒絕存取：解析後路徑包含敏感關鍵字'; exit 1 ;; esac; "
        f'case "$real" in {"|".join(allowed)}) ;; '
        "*) echo '[ERROR] 拒絕存取：解析後路徑不在目錄白名單內'; exit 1 ;; esac; "
        f'[ -{kind} "$real" ] || '
        "{ echo '[ERROR] 不支援的檔案類型'; exit 1; }; "
    )


def diagnostic_command(command: str) -> str:
    """使用 RHEL Bash 保存管線錯誤；限量顯示仍排空上游，避免 head 的 SIGPIPE。"""
    script = (
        "export LC_ALL=C; set -E -o pipefail; "
        'trap \'rc=$?; printf "[ERROR] 查詢區段失敗 (exit %s)\\n" "$rc" >&2\' ERR; ' + command
    )
    return "bash -c " + shlex.quote(script)


def redact_and_truncate(text: str) -> str:
    """RHEL 專屬嚴格輸出檢查（二進位／多行未知敏感邊界整份 fail-closed 拒收）與截斷。"""
    if not text:
        return "(沒有輸出)"
    if any(ord(char) < 32 and char not in "\n\r\t" for char in text) or "\ufffd" in text:
        return "[ERROR] 輸出包含二進位或無法安全解碼的內容，已捨棄。"
    if re.search(
        r"(?im)\b(?:[\w-]*(?:password|passwd|secret|token|passphrase|api[_-]?key)[\w-]*"
        r"|psk|authorization|private_key|preshared_key)\b[\"']?\s*[:=][ \t]*"
        r"(?:[|>][^\r\n]*|\"[^\"\r\n]*|'[^'\r\n]*|[ \t]*)(?:\r?\n|\Z)",
        text,
    ):
        return "[ERROR] 偵測到無法安全判定範圍的敏感欄位，已捨棄內容。"
    if re.search(
        r"(?im)^[^\r\n]*(?:password|passwd|secret|token|passphrase|api[_-]?key|psk)"
        r"[^\r\n]*[:=][^\r\n]*\r?\n[ \t]+\S",
        text,
    ):
        return "[ERROR] 敏感欄位後含縮排延續內容，已捨棄。"
    if re.search(r"(?im)^\s*(?:machine|default|login|password)\s*$", text):
        return "[ERROR] 偵測到跨行憑證格式，已捨棄內容。"

    redacted = _REDACT_PEM_RE.sub("[REDACTED PRIVATE KEY]", text)
    redacted = _REDACT_KV_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _REDACT_SECRET_LINE_RE.sub("[REDACTED]", redacted)
    redacted = _REDACT_URL_RE.sub(r"\1[REDACTED]@", redacted)
    if len(redacted) > MAX_OUTPUT_CHARS:
        return (
            redacted[:MAX_OUTPUT_CHARS] + f"\n... (輸出過長已截斷，已達 {MAX_OUTPUT_CHARS} 字元上限)\n"
            "[提示：若需檢視後續內容或縮小範圍，請指定更精確的查詢條件、縮短 since 時間範圍，或調整行數上限]"
        )
    return redacted

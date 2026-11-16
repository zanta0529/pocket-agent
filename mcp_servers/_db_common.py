"""遠端唯讀資料庫 MCP 伺服器（MySQL / MSSQL）共用基礎設施模組。"""

import os
import re
import sys
import time
from pathlib import Path

try:
    from pocket_agent.system.constants import DEFAULT_TOOL_OUTPUT_CHARS

    MAX_OUTPUT_CHARS = DEFAULT_TOOL_OUTPUT_CHARS
except ImportError:
    MAX_OUTPUT_CHARS = 12000

_IDENT_RE = re.compile(r"^[a-zA-Z0-9_.@$\-]+$")

_REDACT_SECRET_RE = re.compile(
    r"(?im)\b(?:[\w-]*(?:password|passwd|secret|token|passphrase|api[_-]?key)[\w-]*"
    r"|authorization|private_key|preshared_key|connectionstring)\b[^\r\n\x00]*"
)
_REDACT_URL_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^\s/@\"']+:[^\s/@\"']+@")
_REDACT_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----")
_REDACT_KV_RE = re.compile(
    r"(?i)(\b(?:password|passwd|secret|token|api_key|apikey|client_secret|passphrase|connectionstring)\b"
    r"\s*[:=]\s*)([^\s#;\"',)]+|\"[^\"]*\"|'[^']*')"
)
SENSITIVE_COL_RE = re.compile(
    r"(?i)(?:password|passwd|secret|token|passphrase|api[_-]?key"
    r"|private[_-]?key|preshared[_-]?key|credential|connectionstring)"
)


class ConnAuthError(Exception):
    """表示連線參數驗證失敗、網路無法連線或資料庫帳號密碼認證失敗。"""


def get_audit_log_path() -> Path:
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


def audit_log(
    mcp_tag: str,
    tool_name: str,
    command: str,
    *,
    target: str = "",
    status: str = "OK",
    detail: str = "",
) -> None:
    """將 MCP 工具執行的 SQL 指令與敏感操作寫入 Pocket Agent 集中化日誌 (agent.log)。"""
    try:
        log_path = get_audit_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        now_t = time.time()
        lt = time.localtime(now_t)
        ms = int((now_t - int(now_t)) * 1000)
        ts = f"{time.strftime('%Y-%m-%d %H:%M:%S', lt)},{ms:03d}"
        safe_cmd = " ".join(_REDACT_KV_RE.sub(r"\1[REDACTED]", command).splitlines()).strip()
        safe_target = " ".join(_REDACT_KV_RE.sub(r"\1[REDACTED]", target).splitlines()).strip()
        safe_detail = " ".join(_REDACT_KV_RE.sub(r"\1[REDACTED]", detail).splitlines()).strip()
        parts = [f"{ts} [AUDIT] [MCP:{mcp_tag}] tool={tool_name} status={status}"]
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


class RuntimeCredsCache:
    """執行期記憶體憑證快取（具 TTL 閒置清除，不落盤）。"""

    def __init__(self, ttl_seconds: float = 600.0) -> None:
        self.ttl_seconds = ttl_seconds
        self.store: dict[str, str] = {}
        self.updated_at: float = 0.0

    def check_ttl(self) -> None:
        now = time.monotonic()
        if self.store and (now - self.updated_at > self.ttl_seconds):
            self.store.clear()
            self.updated_at = 0.0

    def clear(self) -> None:
        self.store.clear()
        self.updated_at = 0.0

    def touch(self) -> None:
        self.updated_at = time.monotonic()


def redact_output(text: str) -> str:
    """自動遮罩查詢結果中的敏感機密欄位、連線 URI 帳密與私鑰。"""
    if not text:
        return text
    out = _REDACT_PEM_RE.sub("[REDACTED_PRIVATE_KEY]", text)
    out = _REDACT_URL_RE.sub(r"\1[REDACTED]@", out)
    return _REDACT_SECRET_RE.sub("[REDACTED]", out)


def format_cell_value(val: object, *, is_sensitive: bool = False) -> str:
    """格式化單一欄位值：敏感欄強制遮罩、None 轉為 NULL、多行文字遮罩後單行化。"""
    if is_sensitive:
        return "[REDACTED]"
    if val is None:
        return "NULL"
    redacted = redact_output(str(val))
    return redacted.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


def is_empty_or_null(val: str | None) -> bool:
    """檢查字串是否為空值、空白、null 或無效預留值。"""
    if val is None:
        return True
    cleaned = str(val).strip()
    return not cleaned or cleaned.lower() in ("null", "none", "~")


def clean_param(val: str | None) -> str:
    """清理並移除外層引號；若為空或無效預留值則回傳空字串。"""
    if is_empty_or_null(val):
        return ""
    return str(val).strip().strip("'\"")


def validate_port(value: str, env_name: str) -> str:
    clean = value.strip()
    if not clean.isdigit() or not (1 <= int(clean) <= 65535):
        raise ValueError(f"非法的 {env_name} 連接埠數值（必須為 1~65535 之間的整數）。")
    return str(int(clean))


def validate_identifier(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _IDENT_RE.match(clean):
        raise ValueError(
            f"非法的 {field_name} 參數格式（僅允許英數字、點、底線、連字號、@ 與 $，且不得以連字號開頭）。"
        )
    return clean

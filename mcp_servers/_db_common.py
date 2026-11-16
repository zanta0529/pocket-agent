"""遠端唯讀資料庫 MCP 伺服器（MySQL / MSSQL）共用基礎設施模組。"""

import os
import re
import sys
import time
from pathlib import Path

try:
    from pocket_agent.system.constants import (
        DB_AUTOINC_WARN_RATIO,
        DB_FRAGMENTATION_MIN_FREE_BYTES,
        DB_FRAGMENTATION_WARN_RATIO,
        DB_HEALTH_MAX_TABLES,
        DEFAULT_TOOL_OUTPUT_CHARS,
        MYSQL_CHECK_TABLE_MAX_BYTES,
        SQLITE_QUICK_CHECK_MAX_BYTES,
    )

    MAX_OUTPUT_CHARS = DEFAULT_TOOL_OUTPUT_CHARS
except ImportError:
    MAX_OUTPUT_CHARS = 12000
    DB_HEALTH_MAX_TABLES = 200
    MYSQL_CHECK_TABLE_MAX_BYTES = 2 * 1024 * 1024 * 1024
    SQLITE_QUICK_CHECK_MAX_BYTES = 500 * 1024 * 1024
    DB_FRAGMENTATION_WARN_RATIO = 0.20
    DB_FRAGMENTATION_MIN_FREE_BYTES = 10 * 1024 * 1024
    DB_AUTOINC_WARN_RATIO = 0.75

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


def format_bytes(num_bytes: int | float | None) -> str:
    """將位元組數轉換為易讀之容量單位字串（B / KB / MB / GB）。"""
    if num_bytes is None:
        return "0 B"
    try:
        val = max(0.0, float(num_bytes))
    except TypeError, ValueError:
        return "0 B"
    if val < 1024:
        return f"{int(val)} B"
    if val < 1024 * 1024:
        return f"{val / 1024:.2f} KB"
    if val < 1024 * 1024 * 1024:
        return f"{val / (1024 * 1024):.2f} MB"
    return f"{val / (1024 * 1024 * 1024):.2f} GB"


def find_redundant_prefix_indexes(
    index_map: dict[str, tuple[bool, list[str]]],
) -> list[str]:
    """偵測單一資料表內具有相同前導欄位序列的冗餘／重複索引。

    index_map 格式：{index_name: (is_unique, [col1, col2, ...])}
    回傳人類易讀的冗餘索引描述清單。
    """
    items = [(name, is_uniq, cols) for name, (is_uniq, cols) in index_map.items() if cols]
    findings: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()
    for i, (name_a, uniq_a, cols_a) in enumerate(items):
        norm_a = [c.lower() for c in cols_a]
        for j, (name_b, uniq_b, cols_b) in enumerate(items):
            if i == j:
                continue
            norm_b = [c.lower() for c in cols_b]
            if len(norm_a) > len(norm_b):
                continue
            if norm_b[: len(norm_a)] != norm_a:
                continue
            # 若兩者完全相同，僅回報一次；若 A 為唯一索引且為 B 的真前綴，A 仍具唯一約束價值不視為冗餘
            if len(norm_a) == len(norm_b):
                pair_key = (min(name_a, name_b), max(name_a, name_b))
                if pair_key in seen_pairs:
                    continue
                if uniq_a and not uniq_b:
                    continue
                seen_pairs.add(pair_key)
                findings.append(f"{name_a} ({', '.join(cols_a)}) 與 {name_b} ({', '.join(cols_b)}) 欄位完全重複")
            elif not uniq_a:
                findings.append(f"{name_a} ({', '.join(cols_a)}) 為 {name_b} ({', '.join(cols_b)}) 的冗餘前導索引")
    return findings


def truncate_health_report(lines: list[str], max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """將健康檢查報告行清單合併並套用機密遮罩與字元上限截斷保護。"""
    text = redact_output("\n".join(lines))
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars].rsplit("\n", 1)[0]
    return (
        f"{clipped}\n\n... (健康檢查報告已截斷，達單次上限 {max_chars} 字元；"
        "建議指定 table_name 或 database 縮小巡檢範圍)"
    )

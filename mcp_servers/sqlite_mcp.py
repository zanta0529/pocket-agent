"""SQLite 唯讀查詢與結構檢索 MCP 伺服器。

安全設計原則：
1. 強制以 URI mode=ro 模式開啟連線，並配置 SQLite Authorizer 嚴密攔截非唯讀操作。
2. 串流分批讀取（fetchmany），防範巨量資料表一次性抓取導致記憶體耗盡 (OOM DoS)。
3. 連線資源確保釋放，杜絕 Windows 檔案佔用與鎖死。
4. 內建機密欄位與正則遮罩，防止資料庫機敏欄位（密碼、Token、金鑰）外洩至 LLM 上下文。
5. 100% 記憶體運作，不產生任何磁碟暫存檔（零落盤）。
6. 支援彈性路徑設定：DB_PATH 可於 mcp_config.yaml 預填、留空、設為 null 或省略；
   未設定或為空時於執行期互動詢問並快取於記憶體。
"""

import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

_MCP_DIR = str(Path(__file__).resolve().parent)
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)

import _db_common  # noqa: E402

MAX_OUTPUT_CHARS = _db_common.MAX_OUTPUT_CHARS
DB_HEALTH_MAX_TABLES = _db_common.DB_HEALTH_MAX_TABLES
SQLITE_QUICK_CHECK_MAX_BYTES = _db_common.SQLITE_QUICK_CHECK_MAX_BYTES
DB_FRAGMENTATION_WARN_RATIO = _db_common.DB_FRAGMENTATION_WARN_RATIO
DB_FRAGMENTATION_MIN_FREE_BYTES = _db_common.DB_FRAGMENTATION_MIN_FREE_BYTES
_find_redundant_prefix_indexes = _db_common.find_redundant_prefix_indexes
_format_bytes = _db_common.format_bytes
_truncate_health_report = _db_common.truncate_health_report
_validate_identifier = _db_common.validate_identifier

mcp = MCPServer("sqlite")

# 執行期記憶體快取（僅存活於當前行程生命週期，不落盤；閒置超過 10 分鐘自動失效）
_RUNTIME_CREDS: dict[str, str] = {}
_RUNTIME_CREDS_TS: float = 0.0
_CREDS_TTL_SECONDS = 600
_CREDS_LOCK = threading.RLock()


class _ConnAuthError(RuntimeError):
    """表示資料庫路徑設定錯誤或連線失敗，供上層清除快取並重新詢問。"""


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
_SENSITIVE_COL_RE = re.compile(r"(?i)(?:password|passwd|pwd|secret|token|api_key|apikey|credential|private_key|ssn)")
_FORBIDDEN_SQLITE_RE = re.compile(
    r"(?i)\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|VACUUM|REINDEX|PRAGMA)\b"
)


def _clear_creds_cache() -> None:
    global _RUNTIME_CREDS_TS
    with _CREDS_LOCK:
        _RUNTIME_CREDS.clear()
        _RUNTIME_CREDS_TS = 0.0


def _ensure_creds_ttl() -> None:
    global _RUNTIME_CREDS_TS
    if _RUNTIME_CREDS and _RUNTIME_CREDS_TS > 0:
        if time.monotonic() - _RUNTIME_CREDS_TS > _CREDS_TTL_SECONDS:
            _clear_creds_cache()


def _touch_creds_cache() -> None:
    global _RUNTIME_CREDS_TS
    _RUNTIME_CREDS_TS = time.monotonic()


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
) -> None:
    """將 MCP 工具執行的 SQL 指令與敏感操作寫入 Pocket Agent 集中化日誌 (agent.log) 供稽核使用。"""
    try:
        log_path = _get_audit_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        now_t = time.time()
        lt = time.localtime(now_t)
        ms = int((now_t - int(now_t)) * 1000)
        ts = f"{time.strftime('%Y-%m-%d %H:%M:%S', lt)},{ms:03d}"
        safe_cmd = " ".join(_REDACT_KV_RE.sub(r"\1[REDACTED]", command).splitlines()).strip()
        safe_target = " ".join(_REDACT_KV_RE.sub(r"\1[REDACTED]", target).splitlines()).strip()
        safe_detail = " ".join(_REDACT_KV_RE.sub(r"\1[REDACTED]", detail).splitlines()).strip()
        parts = [f"{ts} [AUDIT] [MCP:sqlite] tool={tool_name} status={status}"]
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


def _redact_output(text: str) -> str:
    """自動遮罩查詢結果中的敏感機密欄位、連線 URI 帳密與私鑰。"""
    if not text:
        return text
    out = _REDACT_PEM_RE.sub("[REDACTED_PRIVATE_KEY]", text)
    out = _REDACT_URL_RE.sub(r"\1[REDACTED]@", out)
    return _REDACT_SECRET_RE.sub("[REDACTED]", out)


def _format_cell_value(val: object, *, is_sensitive: bool = False) -> str:
    """格式化單一欄位值：敏感欄強制遮罩、None 轉為 NULL、多行文字遮罩後單行化。"""
    if is_sensitive:
        return "[REDACTED]"
    if val is None:
        return "NULL"
    redacted = _redact_output(str(val))
    return redacted.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


def _strip_leading_sql_comments(sql: str) -> str:
    """移除 SQL 開頭的單行 (--) 與多行 (/* */) 註解。"""
    s = sql.strip()
    while s:
        if s.startswith("--"):
            nl = s.find("\n")
            s = s[nl + 1 :].strip() if nl != -1 else ""
        elif s.startswith("/*"):
            end = s.find("*/")
            if end == -1:
                return ""
            s = s[end + 2 :].strip()
        else:
            break
    return s


def _validate_select_query(query: str) -> str:
    """嚴格驗證單一唯讀 SELECT / WITH / EXPLAIN 查詢語句，禁止多重語句與危險關鍵字。"""
    stripped = _strip_leading_sql_comments(query)
    while stripped.endswith(";"):
        stripped = stripped[:-1].strip()
    if not stripped:
        raise ValueError("查詢語句不可為空。")
    if ";" in stripped:
        raise ValueError("禁止執行多重 SQL 語句（不可包含分號 `;`）。")
    if not re.match(r"^(?:EXPLAIN\s+(?:QUERY\s+PLAN\s+)?)?(?:SELECT|WITH)\b", stripped, re.IGNORECASE):
        raise ValueError("基於安全考量，僅允許執行以 SELECT、WITH 或 EXPLAIN SELECT 開頭之唯讀查詢。")
    if _FORBIDDEN_SQLITE_RE.search(stripped):
        raise ValueError("查詢包含不允許的寫入或管理關鍵字，僅允許純唯讀查詢。")
    return stripped


def _is_empty_or_null(val: str | None) -> bool:
    """檢查字串是否為空值、空白、null 或無效預留值。"""
    if val is None:
        return True
    cleaned = val.strip()
    return not cleaned or cleaned.lower() in ("null", "none", "~")


def _tty_prompt(prompt_text: str) -> str:
    """當未於設定檔指定 DB_PATH 時，直接透過作業系統終端機設備（CONIN$/CONOUT$ 或 /dev/tty）詢問使用者。"""
    tty_in_name = "CONIN$" if os.name == "nt" else "/dev/tty"
    tty_out_name = "CONOUT$" if os.name == "nt" else "/dev/tty"
    try:
        with (
            open(tty_out_name, "w", encoding="utf-8") as tty_out,
            open(tty_in_name, encoding="utf-8") as tty_in,
        ):
            tty_out.write(prompt_text)
            tty_out.flush()
            return tty_in.readline().strip()
    except OSError as exc:
        raise ValueError(
            "沒有可用的互動終端機，請於環境變數或 mcp_config.yaml 設定 DB_PATH；不讀取 MCP stdin。"
        ) from exc


_UNC_PREFIXES = ("\\\\", "//")


def _is_network_path(raw: str | None) -> bool:
    """判斷是否為 UNC／網路路徑（須在任何檔案系統操作前呼叫，避免 SMB 認證雜湊外洩）。"""
    if not raw:
        return False
    return raw.strip().strip("'\"").startswith(_UNC_PREFIXES)


def get_db_path(custom_path: str = "") -> Path:
    """解析並驗證 DB_PATH 目標檔案路徑。若未設定或為空，則提示使用者輸入並快取於記憶體。"""
    with _CREDS_LOCK:
        return _get_db_path_locked(custom_path)


def _get_db_path_locked(custom_path: str = "") -> Path:
    _ensure_creds_ttl()
    raw_path = custom_path.strip().strip("'\"") if custom_path else ""

    if _is_empty_or_null(raw_path):
        # 執行期已切換並驗證的路徑優先於啟動環境變數，避免切換後被 DB_PATH 蓋回
        env_val = os.getenv("DB_PATH")
        if not _is_empty_or_null(_RUNTIME_CREDS.get("DB_PATH")):
            raw_path = _RUNTIME_CREDS["DB_PATH"]
        elif not _is_empty_or_null(env_val):
            raw_path = env_val.strip().strip("'\"")
        else:
            prompt_text = "請輸入要連線的 SQLite 資料庫完整路徑與檔名 (DB_PATH): "
            raw_path = _tty_prompt(prompt_text).strip().strip("'\"")

    if _is_empty_or_null(raw_path):
        _clear_creds_cache()
        raise ValueError("未提供 SQLite 資料庫路徑 (DB_PATH)。")

    # 必須早於 resolve()/exists()，否則對外 SMB 連線已發生
    if _is_network_path(raw_path):
        raise ValueError("安全限制：不接受 UNC／網路路徑，請先將資料庫複製到本機。")

    p = Path(raw_path).expanduser().resolve()
    if not p.exists():
        _clear_creds_cache()
        raise FileNotFoundError(f"找不到指定的資料庫檔案：{p}")
    if not p.is_file():
        _clear_creds_cache()
        raise ValueError(f"指定的資料庫路徑並非檔案：{p}")

    _RUNTIME_CREDS["DB_PATH"] = str(p)
    _touch_creds_cache()
    return p


def _get_readonly_connection(
    tool_name: str = "sqlite",
    sql_context: str = "",
    db_path: str = "",
    *,
    allow_diagnostic_pragmas: bool = False,
) -> sqlite3.Connection:
    """以唯讀 URI 模式開啟 SQLite 連線，並註冊 authorizer 阻止任何寫入操作。"""
    try:
        resolved_path = get_db_path(db_path)
    except Exception as exc:
        raise _ConnAuthError(f"[CONN_AUTH] {exc}") from exc

    uri_path = resolved_path.as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri_path, uri=True)
    except Exception as exc:
        _clear_creds_cache()
        raise _ConnAuthError(f"[CONN_AUTH] 無法開啟 SQLite 唯讀連線：{exc}") from exc

    allowed_pragmas = {"table_info", "table_xinfo", "index_list", "index_info", "foreign_key_list"}
    if allow_diagnostic_pragmas:
        allowed_pragmas = allowed_pragmas | {
            "page_count",
            "page_size",
            "freelist_count",
            "journal_mode",
            "auto_vacuum",
            "encoding",
            "user_version",
            "application_id",
            "quick_check",
            "foreign_key_check",
        }

    def _authorizer(action_code, arg1, arg2, db_name, trigger_name):
        # 針對 PRAGMA 進行細部限制：僅允許安全的結構／唯讀診斷 PRAGMA，嚴禁路徑或設定 PRAGMA
        if action_code == sqlite3.SQLITE_PRAGMA:
            pragma_name = str(arg1 or "").lower()
            if pragma_name in allowed_pragmas:
                return sqlite3.SQLITE_OK
            _audit_log(
                tool_name,
                sql_context or f"PRAGMA {arg1}",
                target=str(resolved_path),
                status="SECURITY_BLOCK",
                detail=f"Authorizer blocked PRAGMA {arg1!r}",
            )
            return sqlite3.SQLITE_DENY

        # 允許讀取、查詢相關操作，阻擋任何建立、寫入、修改、刪除操作
        allowed_actions = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_RECURSIVE,
        }
        if action_code in allowed_actions:
            return sqlite3.SQLITE_OK
        _audit_log(
            tool_name,
            sql_context or f"action={action_code}",
            target=str(resolved_path),
            status="SECURITY_BLOCK",
            detail=f"Authorizer blocked action_code={action_code} arg1={arg1!r} arg2={arg2!r}",
        )
        return sqlite3.SQLITE_DENY

    conn.set_authorizer(_authorizer)
    return conn


@mcp.tool()
def sqlite_list_tables(db_path: str = "") -> str:
    """列出 SQLite 資料庫中的所有資料表與檢視表 (VIEW)"""
    conn = None
    sql = (
        "SELECT name, type FROM sqlite_master "
        "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name;"
    )
    try:
        conn = _get_readonly_connection("sqlite_list_tables", sql, db_path=db_path)
        target = _RUNTIME_CREDS.get("DB_PATH", "")
        cursor = conn.cursor()
        cursor.execute(sql)
        tables = cursor.fetchall()
        _audit_log("sqlite_list_tables", sql, target=target, status="OK", detail=f"tables={len(tables)}")
        if not tables:
            return "資料庫中沒有資料表。"
        lines = [f"{t[0]} (VIEW)" if len(t) > 1 and str(t[1]).lower() == "view" else str(t[0]) for t in tables]
        return "\n".join(lines)
    except Exception as e:
        _audit_log(
            "sqlite_list_tables",
            sql,
            target=db_path or _RUNTIME_CREDS.get("DB_PATH") or (os.getenv("DB_PATH") or ""),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    finally:
        if conn:
            conn.close()


@mcp.tool()
def sqlite_describe_schema(table_name: str, db_path: str = "") -> str:
    """顯示指定 SQLite 資料表或檢視表的 Schema（含欄位、型別、預設值、主鍵、索引與外鍵）"""
    conn = None
    clean_name = table_name.strip().strip("'\"`")
    sql = "SELECT name, type, [notnull], dflt_value, pk FROM pragma_table_info(?);"
    try:
        if not clean_name:
            _audit_log(
                "sqlite_describe_schema",
                f"{sql} [table_name='']",
                target=db_path or _RUNTIME_CREDS.get("DB_PATH") or (os.getenv("DB_PATH") or ""),
                status="BLOCKED",
                detail="缺少 table_name 參數",
            )
            return "[ERROR] 缺少 table_name 參數。"

        conn = _get_readonly_connection(
            "sqlite_describe_schema",
            f"{sql} [table={clean_name!r}]",
            db_path=db_path,
        )
        target = _RUNTIME_CREDS.get("DB_PATH", "")
        cursor = conn.cursor()
        # 透過參數化查詢 table_info 避免注入，並相容各種命名的資料表
        cursor.execute(sql, (clean_name,))
        columns = cursor.fetchall()
        if not columns:
            _audit_log(
                "sqlite_describe_schema",
                f"{sql} [table={clean_name!r}]",
                target=target,
                status="NOT_FOUND",
                detail=f"找不到資料表 {clean_name}",
            )
            return f"找不到資料表 {clean_name}。"
        _audit_log(
            "sqlite_describe_schema",
            f"{sql} [table={clean_name!r}]",
            target=target,
            status="OK",
            detail=f"columns={len(columns)}",
        )
        lines = []
        for c in columns:
            col_name, col_type, not_null, dflt_val, is_pk = c[0], c[1] or "TEXT", c[2], c[3], c[4]
            pk_tag = " [PK]" if is_pk else ""
            null_tag = " NOT NULL" if not_null else ""
            default_tag = f" DEFAULT {dflt_val}" if dflt_val is not None else ""
            lines.append(f"{col_name} ({col_type}{pk_tag}{null_tag}{default_tag})")

        try:
            cursor.execute("SELECT name, [unique] FROM pragma_index_list(?);", (clean_name,))
            indexes = cursor.fetchall()
            if indexes:
                lines.append("\n[Indexes]")
                for idx_row in indexes:
                    idx_name, is_unique = idx_row[0], idx_row[1]
                    cursor.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno;", (idx_name,))
                    idx_cols = [r[0] for r in cursor.fetchall() if r[0]]
                    uniq_str = "UNIQUE " if is_unique else ""
                    lines.append(f"- {idx_name}: {uniq_str}({', '.join(idx_cols)})")

            cursor.execute("SELECT [from], [table], [to] FROM pragma_foreign_key_list(?);", (clean_name,))
            fks = cursor.fetchall()
            if fks:
                lines.append("\n[Foreign Keys]")
                for fk_row in fks:
                    lines.append(f"- {fk_row[0]} -> {fk_row[1]}.{fk_row[2]}")
        except Exception:
            pass

        return "\n".join(lines)
    except Exception as e:
        _audit_log(
            "sqlite_describe_schema",
            f"{sql} [table={clean_name!r}]",
            target=db_path or _RUNTIME_CREDS.get("DB_PATH") or (os.getenv("DB_PATH") or ""),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    finally:
        if conn:
            conn.close()


@mcp.tool()
def sqlite_run_select_query(query: str, db_path: str = "") -> str:
    """執行唯讀的 SELECT、WITH 或 EXPLAIN 查詢以檢視資料 (具備串流防爆與機密欄位遮罩)"""
    try:
        clean_query = _validate_select_query(query)
    except ValueError as e:
        _audit_log(
            "sqlite_run_select_query",
            query,
            target=db_path or _RUNTIME_CREDS.get("DB_PATH") or (os.getenv("DB_PATH") or ""),
            status="BLOCKED",
            detail=str(e),
        )
        return f"[ERROR] {e}"

    conn = None
    try:
        conn = _get_readonly_connection("sqlite_run_select_query", clean_query, db_path=db_path)
        target = _RUNTIME_CREDS.get("DB_PATH", "")
        cursor = conn.cursor()
        cursor.execute(clean_query)
        if not cursor.description:
            _audit_log("sqlite_run_select_query", clean_query, target=target, status="OK", detail="rows=0")
            return "查詢未產生欄位結果。"

        col_names = [str(description[0]) for description in cursor.description]
        sensitive_indices = {idx for idx, name in enumerate(col_names) if _SENSITIVE_COL_RE.search(name)}
        result = [", ".join(col_names)]
        chars_count = len(result[0]) + 1
        total_rows = 0
        truncated = False

        # 批次提取防範大表 OOM
        batch_size = 100
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                break
            for row in batch:
                total_rows += 1
                formatted_vals = [
                    _format_cell_value(val, is_sensitive=(idx in sensitive_indices)) for idx, val in enumerate(row)
                ]
                row_str = ", ".join(formatted_vals)
                if chars_count + len(row_str) + 1 > MAX_OUTPUT_CHARS:
                    truncated = True
                    break
                result.append(row_str)
                chars_count += len(row_str) + 1
            if truncated:
                break

        if total_rows == 0:
            _audit_log("sqlite_run_select_query", clean_query, target=target, status="OK", detail="rows=0")
            return "查詢結果為空。"

        output = "\n".join(result)
        shown_rows = len(result) - 1
        _audit_log(
            "sqlite_run_select_query",
            clean_query,
            target=target,
            status="TRUNCATED" if truncated else "OK",
            detail=f"rows={shown_rows}",
        )

        if truncated:
            output += (
                f"\n\n... (查詢結果已截斷，達單次上限 {MAX_OUTPUT_CHARS} 字元，"
                f"已呈現前 {shown_rows} 筆紀錄)\n"
                "[提示：若需檢視特定區段資料，請在 SQL 中加入 WHERE 條件或 LIMIT/OFFSET 分頁]"
            )
        return output
    except _ConnAuthError as e:
        _audit_log(
            "sqlite_run_select_query",
            clean_query,
            target=db_path or _RUNTIME_CREDS.get("DB_PATH") or (os.getenv("DB_PATH") or ""),
            status="AUTH_FAIL",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    except Exception as e:
        _audit_log(
            "sqlite_run_select_query",
            clean_query,
            target=db_path or _RUNTIME_CREDS.get("DB_PATH") or (os.getenv("DB_PATH") or ""),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] 查詢失敗: {e}"
    finally:
        if conn:
            conn.close()


@mcp.tool()
def sqlite_switch_database(target_db_path: str) -> str:
    """切換目前連線的 SQLite 資料庫檔案路徑（以唯讀模式驗證檔案有效性，失敗時保留原連線）"""
    clean_target = (target_db_path or "").strip().strip("'\"")
    if _is_empty_or_null(clean_target):
        _audit_log("sqlite_switch_database", "SWITCH", status="BLOCKED", detail="缺少 target_db_path 參數")
        return "[ERROR] 請提供要切換的 SQLite 資料庫檔案路徑 (target_db_path)。"
    if _is_network_path(clean_target):
        _audit_log(
            "sqlite_switch_database",
            "SWITCH",
            target=clean_target,
            status="BLOCKED",
            detail="拒絕 UNC／網路路徑",
        )
        return "[ERROR] 安全限制：不接受 UNC／網路路徑，請先將資料庫複製到本機。"

    _ensure_creds_ttl()
    prev_db = _RUNTIME_CREDS.get("DB_PATH")
    conn = None
    sql = "SELECT count(*) FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%';"
    try:
        conn = _get_readonly_connection("sqlite_switch_database", sql, db_path=clean_target)
        row = conn.execute(sql).fetchone()
        table_count = int(row[0]) if row else 0
        resolved = _RUNTIME_CREDS.get("DB_PATH", clean_target)
        _audit_log(
            "sqlite_switch_database",
            f"SWITCH TO {resolved}",
            target=resolved,
            status="OK",
            detail=f"tables={table_count}",
        )
        return f"[SUCCESS] 已切換 SQLite 資料庫至：{resolved}（共 {table_count} 個資料表／檢視表）"
    except Exception as e:
        # 驗證失敗（含非 SQLite 檔案於 execute 階段才拋錯）時還原原路徑，避免快取無效路徑
        if prev_db:
            _RUNTIME_CREDS["DB_PATH"] = prev_db
            _touch_creds_cache()
        else:
            _RUNTIME_CREDS.pop("DB_PATH", None)
        _audit_log(
            "sqlite_switch_database",
            f"SWITCH TO {clean_target}",
            target=clean_target,
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] 切換 SQLite 資料庫失敗: {e}"
    finally:
        if conn:
            conn.close()


@mcp.tool()
def sqlite_inspect_health(
    table_name: str = "",
    include_quick_check: bool = True,
    db_path: str = "",
) -> str:
    """檢視 SQLite 資料庫或資料表的頁面空間、碎片率、主鍵／索引、外鍵與快速實體健康狀態"""
    conn = None
    clean_table = (table_name or "").strip().strip("'\"`")
    sql_context = f"HEALTH_CHECK [table={clean_table!r}]"
    try:
        if clean_table:
            try:
                clean_table = _validate_identifier(clean_table, "table_name")
            except ValueError as e:
                _audit_log(
                    "sqlite_inspect_health",
                    sql_context,
                    target=db_path or _RUNTIME_CREDS.get("DB_PATH") or (os.getenv("DB_PATH") or ""),
                    status="BLOCKED",
                    detail=str(e),
                )
                return f"[ERROR] {e}"

        conn = _get_readonly_connection(
            "sqlite_inspect_health",
            sql_context,
            db_path=db_path,
            allow_diagnostic_pragmas=True,
        )
        target = _RUNTIME_CREDS.get("DB_PATH", "")
        cursor = conn.cursor()

        def _scalar_pragma(p_name: str, default: object = 0) -> object:
            try:
                row = cursor.execute(f"PRAGMA {p_name};").fetchone()
                return row[0] if row and row[0] is not None else default
            except Exception:
                return default

        page_size = int(_scalar_pragma("page_size", 4096) or 4096)
        page_count = int(_scalar_pragma("page_count", 0) or 0)
        freelist_count = int(_scalar_pragma("freelist_count", 0) or 0)
        journal_mode = str(_scalar_pragma("journal_mode", "unknown")).upper()
        auto_vacuum_code = int(_scalar_pragma("auto_vacuum", 0) or 0)
        encoding = str(_scalar_pragma("encoding", "UTF-8"))

        auto_vacuum_map = {0: "NONE", 1: "FULL", 2: "INCREMENTAL"}
        auto_vacuum_str = auto_vacuum_map.get(auto_vacuum_code, str(auto_vacuum_code))

        total_bytes = page_size * page_count
        free_bytes = page_size * freelist_count
        used_bytes = max(0, total_bytes - free_bytes)
        try:
            file_bytes = Path(target).stat().st_size if target else total_bytes
        except OSError:
            file_bytes = total_bytes

        if clean_table:
            cursor.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?;",
                (clean_table,),
            )
        else:
            cursor.execute(
                "SELECT name, type FROM sqlite_master "
                "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
                "ORDER BY type, name LIMIT ?;",
                (int(DB_HEALTH_MAX_TABLES),),
            )
        objects = cursor.fetchall()
        if not objects:
            if clean_table:
                _audit_log(
                    "sqlite_inspect_health",
                    sql_context,
                    target=target,
                    status="NOT_FOUND",
                    detail=f"找不到資料表 {clean_table}",
                )
                return f"找不到資料表 {clean_table}。"
            _audit_log("sqlite_inspect_health", sql_context, target=target, status="OK", detail="tables=0")
            return "資料庫中沒有資料表。"

        table_count = 0
        view_count = 0
        overview_lines: list[str] = []
        warnings_list: list[str] = []

        if free_bytes >= DB_FRAGMENTATION_MIN_FREE_BYTES and total_bytes > 0:
            frag_ratio = free_bytes / total_bytes
            if frag_ratio >= DB_FRAGMENTATION_WARN_RATIO:
                warnings_list.append(
                    f"[Fragmentation] 資料庫 Freelist 閒置空間達 {_format_bytes(free_bytes)} "
                    f"({frag_ratio * 100:.1f}%)，建議於維護視窗評估執行 VACUUM"
                )

        for obj_name, obj_type in objects:
            t_name = str(obj_name)
            t_type = str(obj_type or "table").lower()
            if t_type == "view":
                view_count += 1
                overview_lines.append(f"- {t_name}: VIEW")
                continue

            table_count += 1
            cursor.execute("SELECT name, type, [notnull], dflt_value, pk FROM pragma_table_info(?);", (t_name,))
            cols = cursor.fetchall()
            has_pk = any(bool(c[4]) for c in cols)
            col_count = len(cols)

            idx_map: dict[str, tuple[bool, list[str]]] = {}
            try:
                cursor.execute("SELECT name, [unique] FROM pragma_index_list(?);", (t_name,))
                for idx_row in cursor.fetchall():
                    iname = str(idx_row[0])
                    is_uniq = bool(idx_row[1])
                    cursor.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno;", (iname,))
                    icols = [str(r[0]) for r in cursor.fetchall() if r[0]]
                    if icols:
                        idx_map[iname] = (is_uniq, icols)
            except Exception:
                pass

            pk_str = "PK=Yes" if has_pk else "PK=No"
            overview_lines.append(f"- {t_name}: Columns={col_count}, {pk_str}, Indexes={len(idx_map)}")
            if not has_pk:
                warnings_list.append(f"[Missing PK] 資料表 {t_name} 缺少顯式主鍵 (Primary Key)")
            for red_msg in _find_redundant_prefix_indexes(idx_map):
                warnings_list.append(f"[Redundant Index] {t_name}: {red_msg}")

        # 實體頁面與外鍵檢查（受 500 MB 檔案大小保護門檻約束）
        quick_check_summary = "未執行 (include_quick_check=False)"
        if file_bytes > SQLITE_QUICK_CHECK_MAX_BYTES:
            limit_str = _format_bytes(SQLITE_QUICK_CHECK_MAX_BYTES)
            quick_check_summary = (
                f"已略過（資料庫大小 {_format_bytes(file_bytes)} 超過安全門檻 {limit_str}，保護磁碟 I/O）"
            )
        else:
            try:
                if clean_table:
                    cursor.execute(f'PRAGMA foreign_key_check("{clean_table}");')
                else:
                    cursor.execute("PRAGMA foreign_key_check;")
                fk_violations = cursor.fetchmany(50)
                for v in fk_violations:
                    if len(v) >= 4:
                        warnings_list.append(
                            f"[Foreign Key Violation] {v[0]} (rowid={v[1]}) -> "
                            f"參照目標表 {v[2]} (fkid={v[3]}) 存在孤兒紀錄"
                        )
            except Exception:
                pass

            if include_quick_check:
                try:
                    cursor.execute("PRAGMA quick_check(10);")
                    qc_rows = cursor.fetchall()
                    qc_msgs = [str(r[0]) for r in qc_rows if r and r[0] is not None]
                    if qc_msgs == ["ok"] or (len(qc_msgs) == 1 and qc_msgs[0].lower() == "ok"):
                        quick_check_summary = "OK (PRAGMA quick_check 通過)"
                    else:
                        quick_check_summary = f"發現異常 ({len(qc_msgs)} 項)"
                        for msg in qc_msgs[:10]:
                            warnings_list.append(f"[Integrity Issue] PRAGMA quick_check: {msg}")
                except Exception as qc_err:
                    quick_check_summary = f"執行失敗 ({qc_err})"

        _audit_log(
            "sqlite_inspect_health",
            sql_context,
            target=target,
            status="OK",
            detail=f"tables={table_count} views={view_count}",
        )

        report: list[str] = [
            "[SQLite Health Summary]",
            f"檔案路徑: {target}" + (f" / 資料表: {clean_table}" if clean_table else ""),
            f"資料表數: {table_count} 張實體表, {view_count} 個檢視表",
            f"檔案容量: {_format_bytes(total_bytes)} "
            f"(Used={_format_bytes(used_bytes)}, Freelist={_format_bytes(free_bytes)}, "
            f"PageSize={page_size} B, Pages={page_count})",
            f"引擎設定: Journal={journal_mode}, AutoVacuum={auto_vacuum_str}, Encoding={encoding}",
            f"快速實體檢查: {quick_check_summary}",
            "",
            "[Health Warnings]",
        ]
        if warnings_list:
            report.extend(f"⚠️ {w}" for w in warnings_list)
        else:
            report.append("✓ 未發現明顯結構、主鍵、外鍵、碎片或實體頁損毀風險。")

        report.extend(["", "[Table Overview]"])
        report.extend(overview_lines)
        return _truncate_health_report(report, MAX_OUTPUT_CHARS)
    except _ConnAuthError as e:
        _audit_log(
            "sqlite_inspect_health",
            sql_context,
            target=db_path or _RUNTIME_CREDS.get("DB_PATH") or (os.getenv("DB_PATH") or ""),
            status="AUTH_FAIL",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    except Exception as e:
        _audit_log(
            "sqlite_inspect_health",
            sql_context,
            target=db_path or _RUNTIME_CREDS.get("DB_PATH") or (os.getenv("DB_PATH") or ""),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] 健康檢查失敗: {e}"
    finally:
        if conn:
            conn.close()


# 向後相容別名 (Backward compatibility aliases)
db_list_tables = sqlite_list_tables
db_describe_schema = sqlite_describe_schema
db_run_select_query = sqlite_run_select_query
db_switch_database = sqlite_switch_database
db_inspect_health = sqlite_inspect_health


if __name__ == "__main__":
    mcp.run()

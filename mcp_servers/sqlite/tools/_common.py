"""SQLite MCP 工具群組共用輔助與 Template Method 執行包裝器。"""

import sqlite3
from collections.abc import Callable
from typing import Any

from mcp_servers._shared import (
    CONSTANTS,
    DEFAULT_REDACTION_POLICY,
    ConnAuthError,
    t,
)
from mcp_servers.sqlite.adapter import SQLITE_ADAPTER, get_query_timeout_sec

_SHADOW_SUFFIXES = (
    "_data",
    "_idx",
    "_content",
    "_docsize",
    "_config",
    "_node",
    "_rowid",
    "_parent",
    "_stat",
    "_segments",
    "_segdir",
)
_DISALLOWED_NAME_TOKENS = (";", "--", "/*", "*/", "\x00", "\r", "\n", "'", '"', "`")


def validate_sqlite_object_name(raw_name: str, field_name: str = "table_name") -> str:
    """清理並驗證 SQLite 物件名稱（允許 Unicode 表名，阻擋控制字元、分號、註解與內嵌引號）。"""
    clean = (raw_name or "").strip().strip("'\"`")
    if not clean:
        return ""
    if any(ord(ch) < 32 for ch in clean) or any(tok in clean for tok in _DISALLOWED_NAME_TOKENS):
        raise ValueError(t("sqlite_mcp.invalid_object_name", field_name=field_name))
    return clean


def quote_sqlite_ident(name: str) -> str:
    """以雙引號安全跳脫 SQLite 識別字。"""
    return '"' + name.replace('"', '""') + '"'


def is_virtual_table_sql(sql: str | None) -> bool:
    """判斷 sqlite_master.sql 是否為 CREATE VIRTUAL TABLE 定義。"""
    return bool(sql and sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE"))


def collect_virtual_shadow_tables(rows: list[tuple[Any, ...]]) -> tuple[set[str], set[str]]:
    """從 (name, type, sql) 清單辨識所有虛擬表及其 FTS/R-Tree shadow tables。"""
    virtual_tables: set[str] = set()
    all_table_names: set[str] = set()
    for row in rows:
        name = str(row[0])
        obj_type = str(row[1] or "").lower()
        sql = str(row[2] or "") if len(row) > 2 and row[2] is not None else ""
        if obj_type == "table":
            all_table_names.add(name)
            if is_virtual_table_sql(sql):
                virtual_tables.add(name)

    shadow_tables: set[str] = set()
    for vtab in virtual_tables:
        for suffix in _SHADOW_SUFFIXES:
            candidate = f"{vtab}{suffix}"
            if candidate in all_table_names:
                shadow_tables.add(candidate)
    return virtual_tables, shadow_tables


def is_timeout_error(exc: Exception) -> bool:
    """判斷是否為 SQLite progress_handler 逾時中斷錯誤。"""
    return isinstance(exc, sqlite3.OperationalError) and "interrupted" in str(exc).lower()


def format_sqlite_error(
    tool_name: str,
    exc: Exception,
    *,
    sql: str,
    target: str,
    status: str = "ERROR",
    message_key: str = "",
    timeout_sec: float | None = None,
) -> str:
    """記錄集中化稽核日誌並回傳經機密遮罩之 [ERROR] 訊息（逾時自動轉為友善提示）。"""
    safe_err = DEFAULT_REDACTION_POLICY.redact(str(exc))
    if is_timeout_error(exc):
        effective_timeout = int(timeout_sec if timeout_sec is not None else get_query_timeout_sec())
        SQLITE_ADAPTER.audit.log(tool_name, sql, target=target, status="TIMEOUT", detail=safe_err)
        return f"[ERROR] {t('sqlite_mcp.query_timeout', timeout_sec=effective_timeout)}"

    SQLITE_ADAPTER.audit.log(tool_name, sql, target=target, status=status, detail=safe_err)
    if message_key and not isinstance(exc, ConnAuthError):
        return f"[ERROR] {t(message_key, error=safe_err)}"
    if safe_err.startswith("[ERROR]"):
        return safe_err
    return f"[ERROR] {safe_err}"


def truncate_text(
    text: str,
    *,
    max_chars: int = CONSTANTS.max_output_chars,
    hint: str = "",
) -> str:
    """先執行機密遮罩再安全裁切輸出，防止超長結構清單或 DDL 擠爆上下文。"""
    redacted = DEFAULT_REDACTION_POLICY.redact(text)
    clipped, _ = DEFAULT_REDACTION_POLICY.truncate_output(redacted, max_chars=max_chars, hint=hint)
    return clipped


def run_readonly_tool(
    tool_name: str,
    fn: Callable[[sqlite3.Connection, str], str],
    *,
    sql_hint: str,
    db_path: str = "",
    allow_diagnostic_pragmas: bool = False,
    error_key: str = "",
) -> str:
    """SQLite 唯讀工具標準執行樣板：開連線 → 執行回呼 → 遮罩錯誤與稽核 → 安全釋放。"""
    conn = None
    audit_target = SQLITE_ADAPTER.current_target(db_path)
    try:
        conn, resolved = SQLITE_ADAPTER.open_connection(
            tool_name=tool_name,
            sql_context=sql_hint,
            db_path=db_path,
            allow_diagnostic_pragmas=allow_diagnostic_pragmas,
        )
        audit_target = str(resolved)
        return fn(conn, audit_target)
    except ConnAuthError as exc:
        return format_sqlite_error(
            tool_name,
            exc,
            sql=sql_hint,
            target=audit_target,
            status="AUTH_FAIL",
        )
    except Exception as exc:
        return format_sqlite_error(
            tool_name,
            exc,
            sql=sql_hint,
            target=audit_target,
            status="ERROR",
            message_key=error_key,
        )
    finally:
        SQLITE_ADAPTER.close_safely(conn)

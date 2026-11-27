"""MSSQL MCP 工具群組共用輔助與 Template Method 執行包裝器。"""

import os
from collections.abc import Callable
from typing import Any

from mcp_servers._shared import (
    CONSTANTS,
    DEFAULT_REDACTION_POLICY,
    ConnAuthError,
    clean_param,
    t,
    validate_identifier,
)
from mcp_servers.mssql.adapter import MSSQL_ADAPTER


def parse_mssql_target(table_name: str, database: str, *, default_schema: str = "") -> tuple[str, str, str]:
    """解析 1/2/3 段式物件名稱（[db].[schema].[obj]）與 database 參數。"""
    explicit_db = clean_param(database)
    if explicit_db:
        explicit_db = validate_identifier(explicit_db, "database")

    clean_tbl = (table_name or "").strip().strip("'\"`[]")
    if not clean_tbl:
        return explicit_db, default_schema, ""

    parts = [p.strip("[]'\"") for p in clean_tbl.split(".")]
    if len(parts) == 3:
        return (
            validate_identifier(parts[0], "database"),
            validate_identifier(parts[1], "schema_name"),
            validate_identifier(parts[2], "table_name"),
        )
    if len(parts) == 2:
        return (
            explicit_db,
            validate_identifier(parts[0], "schema_name"),
            validate_identifier(parts[1], "table_name"),
        )
    if len(parts) == 1:
        return explicit_db, default_schema, validate_identifier(parts[0], "table_name")
    raise ValueError(t("mssql_mcp.invalid_table_format", table=repr(table_name)))


def resolve_target_db(explicit_db: str, mssql_database: str) -> str:
    """依「顯式參數 → 注入資料庫 → 快取 → 環境變數」解析生效的目標資料庫。"""
    return (
        explicit_db
        or clean_param(mssql_database)
        or MSSQL_ADAPTER.cache.get("MSSQL_DATABASE")
        or clean_param(os.getenv("MSSQL_DATABASE"))
    )


def is_permission_error(exc: Exception) -> bool:
    """判斷例外是否源自 SQL Server 權限不足（如 VIEW DATABASE/SERVER STATE 或 msdb）。"""
    msg = str(exc).lower()
    return (
        "permission" in msg
        or "denied" in msg
        or "view database state" in msg
        or "view server state" in msg
        or " 297" in msg
        or " 300" in msg
        or " 229" in msg
        or " 916" in msg
    )


def format_mssql_error(
    tool_name: str,
    exc: Exception,
    *,
    sql: str,
    target: str,
    status: str = "ERROR",
    message_key: str = "",
) -> str:
    """記錄集中化稽核日誌並回傳經機密遮罩之 [ERROR] 訊息。"""
    safe_err = DEFAULT_REDACTION_POLICY.redact(str(exc))
    MSSQL_ADAPTER.audit.log(tool_name, sql, target=target, status=status, detail=safe_err)
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
    """先執行機密遮罩再安全裁切輸出，防止超長結構清單或物件定義擠爆上下文。"""
    redacted = DEFAULT_REDACTION_POLICY.redact(text)
    clipped, _ = DEFAULT_REDACTION_POLICY.truncate_output(redacted, max_chars=max_chars, hint=hint)
    return clipped


def run_readonly_tool(
    tool_name: str,
    fn: Callable[[Any, str, str], str],
    *,
    sql_hint: str,
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
    explicit_db: str = "",
    error_key: str = "",
) -> str:
    """唯讀工具標準執行樣板：建立連線 → 執行回呼 → 稽核與遮罩錯誤 → 強制 rollback/close。"""
    conn = None
    audit_target = MSSQL_ADAPTER.format_audit_target(
        mssql_host,
        mssql_port,
        mssql_user,
        explicit_db or mssql_database,
    )
    try:
        conn = MSSQL_ADAPTER.connect(mssql_host, mssql_port, mssql_user, mssql_password, mssql_database)
        target_db = resolve_target_db(explicit_db, mssql_database)
        audit_target = MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, target_db)
        return fn(conn, target_db, audit_target)
    except ConnAuthError as exc:
        return format_mssql_error(
            tool_name,
            exc,
            sql=sql_hint,
            target=audit_target,
            status="AUTH_FAIL",
        )
    except Exception as exc:
        return format_mssql_error(
            tool_name,
            exc,
            sql=sql_hint,
            target=audit_target,
            status="ERROR",
            message_key=error_key,
        )
    finally:
        MSSQL_ADAPTER.close_safely(conn)

"""MySQL MCP 工具群組共用輔助與 Template Method 執行包裝器。"""

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
from mcp_servers.mysql.adapter import MYSQL_ADAPTER

SYSTEM_SCHEMAS_SQL = "('information_schema', 'performance_schema', 'mysql', 'sys')"


def parse_mysql_target(table_name: str, database: str) -> tuple[str, str]:
    """解析 table_name（支援 schema.table）與 database 並嚴格驗證識別字。"""
    clean_tbl = (table_name or "").strip().strip("'\"`")
    clean_db = clean_param(database)
    if clean_tbl and "." in clean_tbl:
        schema_part, tbl_part = clean_tbl.split(".", 1)
        return (
            validate_identifier(schema_part.strip("`'\""), "schema_name"),
            validate_identifier(tbl_part.strip("`'\""), "table_name"),
        )
    validated_schema = validate_identifier(clean_db, "database") if clean_db else ""
    validated_tbl = validate_identifier(clean_tbl, "table_name") if clean_tbl else ""
    return validated_schema, validated_tbl


def resolve_target_schema(explicit_schema: str, mysql_database: str) -> str:
    """依「顯式參數 → 注入資料庫 → 快取 → 環境變數」解析生效的目標資料庫。"""
    return (
        explicit_schema
        or clean_param(mysql_database)
        or MYSQL_ADAPTER.cache.get("MYSQL_DATABASE")
        or clean_param(os.getenv("MYSQL_DATABASE"))
    )


def format_mysql_error(
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
    MYSQL_ADAPTER.audit.log(tool_name, sql, target=target, status=status, detail=safe_err)
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
    fn: Callable[[Any, str, str], str],
    *,
    sql_hint: str,
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
    explicit_schema: str = "",
    error_key: str = "",
) -> str:
    """唯讀工具標準執行樣板：建立連線 → 執行回呼 → 稽核與遮罩錯誤 → 強制 rollback/close。"""
    conn = None
    audit_target = MYSQL_ADAPTER.format_audit_target(
        mysql_host,
        mysql_port,
        mysql_user,
        explicit_schema or mysql_database,
    )
    try:
        conn = MYSQL_ADAPTER.connect(mysql_host, mysql_port, mysql_user, mysql_password, mysql_database)
        target_schema = resolve_target_schema(explicit_schema, mysql_database)
        audit_target = MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, target_schema)
        return fn(conn, target_schema, audit_target)
    except ConnAuthError as exc:
        return format_mysql_error(
            tool_name,
            exc,
            sql=sql_hint,
            target=audit_target,
            status="AUTH_FAIL",
        )
    except Exception as exc:
        return format_mysql_error(
            tool_name,
            exc,
            sql=sql_hint,
            target=audit_target,
            status="ERROR",
            message_key=error_key,
        )
    finally:
        MYSQL_ADAPTER.close_safely(conn)

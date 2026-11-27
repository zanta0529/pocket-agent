"""MySQL / MariaDB 唯讀查詢與執行計畫分析工具群組（Command / Registrar Pattern）。"""

from typing import Any

from mcp_servers._shared import (
    CONSTANTS,
    ConnAuthError,
    stream_query_results,
    t,
)
from mcp_servers.mysql.adapter import MYSQL_ADAPTER
from mcp_servers.mysql.sql_guard import MYSQL_SQL_GUARD
from mcp_servers.mysql.tools._common import format_mysql_error, run_readonly_tool, truncate_text

MAX_OUTPUT_CHARS = CONSTANTS.max_output_chars

_VALID_EXPLAIN_FORMATS = frozenset({"TRADITIONAL", "JSON", "TREE"})


def mysql_run_select_query(
    query: str,
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """於遠端 MySQL/MariaDB 執行唯讀的 SELECT 或 WITH 查詢 (具備串流防爆與機密欄位遮罩)"""
    try:
        clean_query = MYSQL_SQL_GUARD.validate_select(query)
    except ValueError as exc:
        return format_mysql_error(
            "mysql_run_select_query",
            exc,
            sql=query,
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database),
            status="BLOCKED",
        )

    conn = None
    cursor = None
    stream_completed = False
    audit_target = MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database)
    try:
        conn = MYSQL_ADAPTER.connect(mysql_host, mysql_port, mysql_user, mysql_password, mysql_database)
        audit_target = MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database)
        cursor = MYSQL_ADAPTER.open_streaming_cursor(conn)
        cursor.execute(clean_query)

        res = stream_query_results(cursor, max_chars=MAX_OUTPUT_CHARS)
        stream_completed = res.stream_completed
        MYSQL_ADAPTER.audit.log(
            "mysql_run_select_query",
            clean_query,
            target=audit_target,
            status="TRUNCATED" if res.truncated else "OK",
            detail=f"rows={res.shown_rows}",
        )
        return res.output
    except ConnAuthError as exc:
        return format_mysql_error(
            "mysql_run_select_query",
            exc,
            sql=clean_query,
            target=audit_target,
            status="AUTH_FAIL",
        )
    except Exception as exc:
        return format_mysql_error(
            "mysql_run_select_query",
            exc,
            sql=clean_query,
            target=audit_target,
            status="ERROR",
            message_key="mysql_mcp.query_failed",
        )
    finally:
        if stream_completed:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            MYSQL_ADAPTER.close_safely(conn)
        else:
            MYSQL_ADAPTER.abort_connection(conn, cursor)


def _analyze_traditional_explain(col_names: list[str], rows: list[tuple[Any, ...]]) -> list[str]:
    lower_cols = [c.lower() for c in col_names]
    idx_table = lower_cols.index("table") if "table" in lower_cols else -1
    idx_type = lower_cols.index("type") if "type" in lower_cols else -1
    idx_key = lower_cols.index("key") if "key" in lower_cols else -1
    idx_extra = lower_cols.index("extra") if "extra" in lower_cols else -1

    warnings: list[str] = []
    for row in rows:
        tbl = str(row[idx_table]) if 0 <= idx_table < len(row) and row[idx_table] is not None else "<derived>"
        scan_type = str(row[idx_type] or "").upper() if 0 <= idx_type < len(row) else ""
        used_key = row[idx_key] if 0 <= idx_key < len(row) else "unknown"
        extra = str(row[idx_extra] or "") if 0 <= idx_extra < len(row) else ""

        if scan_type == "ALL":
            warnings.append(f"⚠️ {t('mysql_mcp.explain_warn_full_scan', table=tbl)}")
        if 0 <= idx_key < len(row) and used_key is None:
            warnings.append(f"⚠️ {t('mysql_mcp.explain_warn_no_key', table=tbl)}")
        if "Using filesort" in extra:
            warnings.append(f"⚠️ {t('mysql_mcp.explain_warn_filesort', table=tbl)}")
        if "Using temporary" in extra:
            warnings.append(f"⚠️ {t('mysql_mcp.explain_warn_temporary', table=tbl)}")
    return warnings


def mysql_explain_query(
    query: str,
    explain_format: str = "TRADITIONAL",
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """分析唯讀 SELECT / WITH 查詢的 MySQL 執行計畫（禁止 EXPLAIN ANALYZE）"""
    fmt_upper = (explain_format or "TRADITIONAL").strip().upper()
    if fmt_upper not in _VALID_EXPLAIN_FORMATS:
        return format_mysql_error(
            "mysql_explain_query",
            ValueError(t("mysql_mcp.explain_invalid_format", fmt=explain_format)),
            sql=f"format={explain_format!r}",
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database),
            status="BLOCKED",
        )

    try:
        clean_query = MYSQL_SQL_GUARD.validate_explainable_select(query)
    except ValueError as exc:
        return format_mysql_error(
            "mysql_explain_query",
            exc,
            sql=query,
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database),
            status="BLOCKED",
        )

    fmt_clause = "" if fmt_upper == "TRADITIONAL" else f"FORMAT={fmt_upper} "
    explain_sql = f"EXPLAIN {fmt_clause}{clean_query}"

    def _run(conn: Any, _target_schema: str, target: str) -> str:
        cursor = conn.cursor()
        try:
            cursor.execute(explain_sql)
            rows = list(cursor.fetchall() or [])
            desc = list(cursor.description or [])
        finally:
            cursor.close()

        MYSQL_ADAPTER.audit.log(
            "mysql_explain_query",
            explain_sql,
            target=target,
            status="OK",
            detail=f"format={fmt_upper} rows={len(rows)}",
        )
        if fmt_upper in ("JSON", "TREE"):
            body = "\n".join(str(r[0]) for r in rows if r)
            return truncate_text(body, max_chars=MAX_OUTPUT_CHARS)

        col_names = [str(d[0]) for d in desc]
        lines = [", ".join(col_names)] if col_names else []
        for row in rows:
            lines.append(", ".join("NULL" if v is None else str(v) for v in row))
        warnings = _analyze_traditional_explain(col_names, rows)
        lines.extend(["", t("mysql_mcp.explain_warnings_header")])
        if warnings:
            lines.extend(warnings)
        else:
            lines.append(t("mysql_mcp.explain_no_warnings"))
        return truncate_text("\n".join(lines), max_chars=MAX_OUTPUT_CHARS)

    return run_readonly_tool(
        "mysql_explain_query",
        _run,
        sql_hint=explain_sql,
        mysql_host=mysql_host,
        mysql_port=mysql_port,
        mysql_user=mysql_user,
        mysql_password=mysql_password,
        mysql_database=mysql_database,
        error_key="mysql_mcp.explain_failed",
    )


def register(mcp: Any) -> None:
    """註冊 MySQL 唯讀查詢與執行計畫工具至 MCPServer。"""
    for fn in (mysql_run_select_query, mysql_explain_query):
        mcp.tool()(fn)

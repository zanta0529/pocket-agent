"""Microsoft SQL Server (MSSQL) 唯讀查詢與執行計畫分析工具群組（Command / Registrar Pattern）。"""

from typing import Any

from mcp_servers._shared import (
    CONSTANTS,
    ConnAuthError,
    stream_query_results,
    t,
)
from mcp_servers.mssql.adapter import MSSQL_ADAPTER
from mcp_servers.mssql.sql_guard import MSSQL_SQL_GUARD
from mcp_servers.mssql.tools._common import format_mssql_error, run_readonly_tool, truncate_text

MAX_OUTPUT_CHARS = CONSTANTS.max_output_chars


def mssql_run_select_query(
    query: str,
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """於遠端 Microsoft SQL Server 執行唯讀的 SELECT 或 WITH 查詢 (具備串流防爆與機密欄位遮罩)"""
    try:
        clean_query = MSSQL_SQL_GUARD.validate_select(query)
    except ValueError as exc:
        return format_mssql_error(
            "mssql_run_select_query",
            exc,
            sql=query,
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database),
            status="BLOCKED",
        )

    conn = None
    cursor = None
    stream_completed = False
    audit_target = MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database)
    try:
        conn = MSSQL_ADAPTER.connect(mssql_host, mssql_port, mssql_user, mssql_password, mssql_database)
        audit_target = MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database)
        cursor = MSSQL_ADAPTER.open_streaming_cursor(conn)
        cursor.execute(clean_query)
        res = stream_query_results(cursor, max_chars=MAX_OUTPUT_CHARS)
        stream_completed = res.stream_completed
        MSSQL_ADAPTER.audit.log(
            "mssql_run_select_query",
            clean_query,
            target=audit_target,
            status="TRUNCATED" if res.truncated else "OK",
            detail=f"rows={res.shown_rows}",
        )
        return res.output
    except ConnAuthError as exc:
        return format_mssql_error(
            "mssql_run_select_query",
            exc,
            sql=clean_query,
            target=audit_target,
            status="AUTH_FAIL",
        )
    except Exception as exc:
        return format_mssql_error(
            "mssql_run_select_query",
            exc,
            sql=clean_query,
            target=audit_target,
            status="ERROR",
            message_key="mssql_mcp.query_failed",
        )
    finally:
        if stream_completed:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            MSSQL_ADAPTER.close_safely(conn)
        else:
            MSSQL_ADAPTER.abort_connection(conn, cursor)


def _analyze_showplan_lines(plan_lines: list[str]) -> list[str]:
    joined = "\n".join(plan_lines)
    warnings: list[str] = []
    if "Table Scan" in joined:
        warnings.append(f"⚠️ {t('mssql_mcp.explain_warn_table_scan')}")
    if "Clustered Index Scan" in joined:
        warnings.append(f"⚠️ {t('mssql_mcp.explain_warn_clustered_scan')}")
    if "Key Lookup" in joined or "RID Lookup" in joined:
        warnings.append(f"⚠️ {t('mssql_mcp.explain_warn_key_lookup')}")
    if "Sort(" in joined or "|--Sort" in joined:
        warnings.append(f"⚠️ {t('mssql_mcp.explain_warn_sort')}")
    if "Hash Match" in joined:
        warnings.append(f"⚠️ {t('mssql_mcp.explain_warn_hash_match')}")
    return warnings


def mssql_explain_query(
    query: str,
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """透過 SET SHOWPLAN_TEXT ON 取得純編譯期預估執行計畫（絕不實際執行查詢）"""
    try:
        clean_query = MSSQL_SQL_GUARD.validate_select(query)
    except ValueError as exc:
        return format_mssql_error(
            "mssql_explain_query",
            exc,
            sql=query,
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database),
            status="BLOCKED",
        )

    def _run(conn: Any, _target_db: str, target: str) -> str:
        cursor = conn.cursor()
        plan_lines: list[str] = []
        try:
            cursor.execute("SET SHOWPLAN_TEXT ON;")
            try:
                cursor.execute(clean_query)
                while True:
                    rows = cursor.fetchall() or []
                    for row in rows:
                        if row:
                            plan_lines.append(str(row[0]))
                    nextset = getattr(cursor, "nextset", None)
                    if not callable(nextset) or not nextset():
                        break
            finally:
                try:
                    cursor.execute("SET SHOWPLAN_TEXT OFF;")
                except Exception:
                    pass
        finally:
            cursor.close()

        MSSQL_ADAPTER.audit.log(
            "mssql_explain_query",
            f"SHOWPLAN_TEXT {clean_query}",
            target=target,
            status="OK",
            detail=f"lines={len(plan_lines)}",
        )
        if not plan_lines:
            return t("mssql_mcp.explain_empty")

        warnings = _analyze_showplan_lines(plan_lines)
        out_lines = ["[MSSQL Execution Plan (SHOWPLAN_TEXT)]", *plan_lines, "", t("mssql_mcp.explain_warnings_header")]
        if warnings:
            out_lines.extend(warnings)
        else:
            out_lines.append(t("mssql_mcp.explain_no_warnings"))
        return truncate_text("\n".join(out_lines), max_chars=MAX_OUTPUT_CHARS)

    return run_readonly_tool(
        "mssql_explain_query",
        _run,
        sql_hint=f"SHOWPLAN_TEXT {clean_query}",
        mssql_host=mssql_host,
        mssql_port=mssql_port,
        mssql_user=mssql_user,
        mssql_password=mssql_password,
        mssql_database=mssql_database,
        error_key="mssql_mcp.explain_failed",
    )


def register(mcp: Any) -> None:
    """註冊 MSSQL 唯讀查詢與執行計畫工具至 MCPServer。"""
    for fn in (mssql_run_select_query, mssql_explain_query):
        mcp.tool()(fn)

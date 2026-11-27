"""SQLite 唯讀查詢、執行計畫分析與資料庫切換工具群組。"""

from typing import Any

from mcp_servers._shared import (
    CONSTANTS,
    ConnAuthError,
    clean_param,
    is_empty_or_null,
    stream_query_results,
    t,
)
from mcp_servers.sqlite.adapter import SQLITE_ADAPTER, get_query_timeout_sec, is_network_path
from mcp_servers.sqlite.sql_guard import SQLITE_SQL_GUARD
from mcp_servers.sqlite.tools._common import format_sqlite_error, run_readonly_tool, truncate_text


def sqlite_run_select_query(query: str, db_path: str = "") -> str:
    """執行唯讀的 SELECT、WITH 或 EXPLAIN 查詢以檢視資料（具備串流防爆、逾時中斷與機密欄位遮罩）"""
    try:
        clean_query = SQLITE_SQL_GUARD.validate_select(query)
    except ValueError as exc:
        SQLITE_ADAPTER.audit.log(
            "sqlite_run_select_query",
            query,
            target=SQLITE_ADAPTER.current_target(db_path),
            status="BLOCKED",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"

    conn = None
    cursor = None
    audit_target = SQLITE_ADAPTER.current_target(db_path)
    timeout_sec = get_query_timeout_sec()
    try:
        conn, resolved = SQLITE_ADAPTER.open_connection(
            "sqlite_run_select_query",
            clean_query,
            db_path=db_path,
            query_timeout_sec=timeout_sec,
        )
        audit_target = str(resolved)
        cursor = SQLITE_ADAPTER.open_streaming_cursor(conn)
        cursor.execute(clean_query)

        res = stream_query_results(cursor, max_chars=CONSTANTS.max_output_chars)
        if res.truncated:
            SQLITE_ADAPTER.abort_connection(conn, cursor)
            conn = None
        SQLITE_ADAPTER.audit.log(
            "sqlite_run_select_query",
            clean_query,
            target=audit_target,
            status="TRUNCATED" if res.truncated else "OK",
            detail=f"rows={res.shown_rows}",
        )
        return res.output
    except ConnAuthError as exc:
        return format_sqlite_error(
            "sqlite_run_select_query",
            exc,
            sql=clean_query,
            target=audit_target,
            status="AUTH_FAIL",
        )
    except Exception as exc:
        return format_sqlite_error(
            "sqlite_run_select_query",
            exc,
            sql=clean_query,
            target=audit_target,
            status="ERROR",
            message_key="sqlite_mcp.query_failed",
            timeout_sec=timeout_sec,
        )
    finally:
        SQLITE_ADAPTER.close_safely(conn)


def sqlite_explain_query(query: str, db_path: str = "") -> str:
    """執行 EXPLAIN QUERY PLAN 分析純 SELECT／WITH 查詢計畫，並自動標記全表掃描與臨時排序警訊"""
    try:
        clean_query = SQLITE_SQL_GUARD.validate_explainable_select(query)
    except ValueError as exc:
        SQLITE_ADAPTER.audit.log(
            "sqlite_explain_query",
            query,
            target=SQLITE_ADAPTER.current_target(db_path),
            status="BLOCKED",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"

    explain_sql = f"EXPLAIN QUERY PLAN {clean_query}"

    def _run(conn: Any, target: str) -> str:
        cursor = conn.cursor()
        cursor.execute(explain_sql)
        rows = cursor.fetchall()
        SQLITE_ADAPTER.audit.log(
            "sqlite_explain_query",
            explain_sql,
            target=target,
            status="OK",
            detail=f"steps={len(rows)}",
        )
        if not rows:
            return t("sqlite_mcp.explain_empty")

        parent_map: dict[int, int] = {}
        for row in rows:
            if len(row) >= 2:
                parent_map[int(row[0])] = int(row[1])

        def _depth(step_id: int) -> int:
            depth = 0
            cur = parent_map.get(step_id, 0)
            seen: set[int] = set()
            while cur and cur not in seen and depth < 16:
                seen.add(cur)
                depth += 1
                cur = parent_map.get(cur, 0)
            return depth

        lines = [t("sqlite_mcp.explain_header")]
        has_scan = False
        has_temp_btree = False
        has_correlated = False
        for row in rows:
            step_id = int(row[0]) if len(row) >= 1 else 0
            detail = str(row[3]) if len(row) >= 4 else str(row[-1])
            indent = "  " * _depth(step_id)
            lines.append(f"{indent}|- {detail}")
            upper_d = detail.upper()
            if "SCAN " in upper_d and "COVERING INDEX" not in upper_d and "USING INDEX" not in upper_d:
                has_scan = True
            if "USE TEMP B-TREE" in upper_d:
                has_temp_btree = True
            if "CORRELATED" in upper_d:
                has_correlated = True

        warnings: list[str] = []
        if has_scan:
            warnings.append(t("sqlite_mcp.explain_warn_scan"))
        if has_temp_btree:
            warnings.append(t("sqlite_mcp.explain_warn_temp_btree"))
        if has_correlated:
            warnings.append(t("sqlite_mcp.explain_warn_correlated"))
        if warnings:
            lines.extend(["", t("sqlite_mcp.explain_warnings")])
            lines.extend(f"⚠️ {w}" for w in warnings)

        return truncate_text("\n".join(lines))

    return run_readonly_tool(
        "sqlite_explain_query",
        _run,
        sql_hint=explain_sql,
        db_path=db_path,
        error_key="sqlite_mcp.explain_failed",
    )


def sqlite_switch_database(target_db_path: str) -> str:
    """切換目前連線的 SQLite 資料庫檔案路徑（以唯讀模式驗證檔案有效性，失敗時保留原連線）"""
    clean_target = clean_param(target_db_path)
    if is_empty_or_null(clean_target):
        err = t("sqlite_mcp.switch_missing_path")
        SQLITE_ADAPTER.audit.log("sqlite_switch_database", "SWITCH", status="BLOCKED", detail=err)
        return f"[ERROR] {err}"
    if is_network_path(clean_target):
        err = t("sqlite_mcp.unc_blocked")
        SQLITE_ADAPTER.audit.log(
            "sqlite_switch_database",
            "SWITCH",
            target=clean_target,
            status="BLOCKED",
            detail=err,
        )
        return f"[ERROR] {err}"

    conn = None
    sql = "SELECT count(*) FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%';"
    try:
        conn, resolved = SQLITE_ADAPTER.open_connection(
            "sqlite_switch_database",
            sql,
            db_path=clean_target,
            persist_cache=False,
        )
        row = conn.execute(sql).fetchone()
        table_count = int(row[0]) if row else 0
        SQLITE_ADAPTER.cache.set("DB_PATH", str(resolved))
        SQLITE_ADAPTER.audit.log(
            "sqlite_switch_database",
            f"SWITCH TO {resolved}",
            target=str(resolved),
            status="OK",
            detail=f"tables={table_count}",
        )
        return f"[SUCCESS] {t('sqlite_mcp.switch_success', path=resolved, count=table_count)}"
    except Exception as exc:
        return format_sqlite_error(
            "sqlite_switch_database",
            exc,
            sql=f"SWITCH TO {clean_target}",
            target=clean_target,
            status="ERROR",
            message_key="sqlite_mcp.switch_failed",
        )
    finally:
        SQLITE_ADAPTER.close_safely(conn)


def register(mcp: Any) -> None:
    """註冊 SQLite 查詢、執行計畫與切換工具至 MCPServer。"""
    for fn in (
        sqlite_run_select_query,
        sqlite_explain_query,
        sqlite_switch_database,
    ):
        mcp.tool()(fn)

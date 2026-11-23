"""SQLite 結構探查、唯讀查詢與資料庫切換工具群組。"""

from typing import Any

from mcp_servers._shared import (
    CONSTANTS,
    ConnAuthError,
    is_empty_or_null,
    stream_query_results,
)
from mcp_servers.sqlite.adapter import SQLITE_ADAPTER, is_network_path
from mcp_servers.sqlite.sql_guard import SQLITE_SQL_GUARD


def sqlite_list_tables(db_path: str = "") -> str:
    """列出 SQLite 資料庫中的所有資料表與檢視表 (VIEW)"""
    conn = None
    sql = (
        "SELECT name, type FROM sqlite_master "
        "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name;"
    )
    try:
        conn = SQLITE_ADAPTER.connect("sqlite_list_tables", sql, db_path=db_path)
        target = SQLITE_ADAPTER.cache.get("DB_PATH")
        cursor = conn.cursor()
        cursor.execute(sql)
        tables = cursor.fetchall()
        SQLITE_ADAPTER.audit.log("sqlite_list_tables", sql, target=target, status="OK", detail=f"tables={len(tables)}")
        if not tables:
            return "資料庫中沒有資料表。"
        lines = [
            f"{row[0]} (VIEW)" if len(row) > 1 and str(row[1]).lower() == "view" else str(row[0]) for row in tables
        ]
        return "\n".join(lines)
    except Exception as exc:
        SQLITE_ADAPTER.audit.log(
            "sqlite_list_tables",
            sql,
            target=SQLITE_ADAPTER.current_target(db_path),
            status="ERROR",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    finally:
        SQLITE_ADAPTER.close_safely(conn)


def sqlite_describe_schema(table_name: str, db_path: str = "") -> str:
    """顯示指定 SQLite 資料表或檢視表的 Schema（含欄位、型別、預設值、主鍵、索引與外鍵）"""
    conn = None
    clean_name = table_name.strip().strip("'\"`")
    sql = "SELECT name, type, [notnull], dflt_value, pk FROM pragma_table_info(?);"
    try:
        if not clean_name:
            SQLITE_ADAPTER.audit.log(
                "sqlite_describe_schema",
                f"{sql} [table_name='']",
                target=SQLITE_ADAPTER.current_target(db_path),
                status="BLOCKED",
                detail="缺少 table_name 參數",
            )
            return "[ERROR] 缺少 table_name 參數。"

        conn = SQLITE_ADAPTER.connect(
            "sqlite_describe_schema",
            f"{sql} [table={clean_name!r}]",
            db_path=db_path,
        )
        target = SQLITE_ADAPTER.cache.get("DB_PATH")
        cursor = conn.cursor()
        cursor.execute(sql, (clean_name,))
        columns = cursor.fetchall()
        if not columns:
            SQLITE_ADAPTER.audit.log(
                "sqlite_describe_schema",
                f"{sql} [table={clean_name!r}]",
                target=target,
                status="NOT_FOUND",
                detail=f"找不到資料表 {clean_name}",
            )
            return f"找不到資料表 {clean_name}。"

        SQLITE_ADAPTER.audit.log(
            "sqlite_describe_schema",
            f"{sql} [table={clean_name!r}]",
            target=target,
            status="OK",
            detail=f"columns={len(columns)}",
        )
        lines: list[str] = []
        for col in columns:
            col_name, col_type, not_null, dflt_val, is_pk = col[0], col[1] or "TEXT", col[2], col[3], col[4]
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
    except Exception as exc:
        SQLITE_ADAPTER.audit.log(
            "sqlite_describe_schema",
            f"{sql} [table={clean_name!r}]",
            target=SQLITE_ADAPTER.current_target(db_path),
            status="ERROR",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    finally:
        SQLITE_ADAPTER.close_safely(conn)


def sqlite_run_select_query(query: str, db_path: str = "") -> str:
    """執行唯讀的 SELECT、WITH 或 EXPLAIN 查詢以檢視資料 (具備串流防爆與機密欄位遮罩)"""
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
    try:
        conn = SQLITE_ADAPTER.connect("sqlite_run_select_query", clean_query, db_path=db_path)
        target = SQLITE_ADAPTER.cache.get("DB_PATH")
        cursor = SQLITE_ADAPTER.open_streaming_cursor(conn)
        cursor.execute(clean_query)

        res = stream_query_results(cursor, max_chars=CONSTANTS.max_output_chars)
        SQLITE_ADAPTER.audit.log(
            "sqlite_run_select_query",
            clean_query,
            target=target,
            status="TRUNCATED" if res.truncated else "OK",
            detail=f"rows={res.shown_rows}",
        )
        return res.output
    except ConnAuthError as exc:
        SQLITE_ADAPTER.audit.log(
            "sqlite_run_select_query",
            clean_query,
            target=SQLITE_ADAPTER.current_target(db_path),
            status="AUTH_FAIL",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    except Exception as exc:
        SQLITE_ADAPTER.audit.log(
            "sqlite_run_select_query",
            clean_query,
            target=SQLITE_ADAPTER.current_target(db_path),
            status="ERROR",
            detail=str(exc),
        )
        return f"[ERROR] 查詢失敗: {exc}"
    finally:
        SQLITE_ADAPTER.close_safely(conn)


def sqlite_switch_database(target_db_path: str) -> str:
    """切換目前連線的 SQLite 資料庫檔案路徑（以唯讀模式驗證檔案有效性，失敗時保留原連線）"""
    clean_target = (target_db_path or "").strip().strip("'\"")
    if is_empty_or_null(clean_target):
        SQLITE_ADAPTER.audit.log(
            "sqlite_switch_database", "SWITCH", status="BLOCKED", detail="缺少 target_db_path 參數"
        )
        return "[ERROR] 請提供要切換的 SQLite 資料庫檔案路徑 (target_db_path)。"
    if is_network_path(clean_target):
        SQLITE_ADAPTER.audit.log(
            "sqlite_switch_database",
            "SWITCH",
            target=clean_target,
            status="BLOCKED",
            detail="拒絕 UNC／網路路徑",
        )
        return "[ERROR] 安全限制：不接受 UNC／網路路徑，請先將資料庫複製到本機。"

    prev_db = SQLITE_ADAPTER.cache.get("DB_PATH")
    conn = None
    sql = "SELECT count(*) FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%';"
    try:
        conn = SQLITE_ADAPTER.connect("sqlite_switch_database", sql, db_path=clean_target)
        row = conn.execute(sql).fetchone()
        table_count = int(row[0]) if row else 0
        resolved = SQLITE_ADAPTER.cache.get("DB_PATH", clean_target)
        SQLITE_ADAPTER.audit.log(
            "sqlite_switch_database",
            f"SWITCH TO {resolved}",
            target=resolved,
            status="OK",
            detail=f"tables={table_count}",
        )
        return f"[SUCCESS] 已切換 SQLite 資料庫至：{resolved}（共 {table_count} 個資料表／檢視表）"
    except Exception as exc:
        if prev_db:
            SQLITE_ADAPTER.cache.set("DB_PATH", prev_db)
        else:
            SQLITE_ADAPTER.cache.pop("DB_PATH", None)
        SQLITE_ADAPTER.audit.log(
            "sqlite_switch_database",
            f"SWITCH TO {clean_target}",
            target=clean_target,
            status="ERROR",
            detail=str(exc),
        )
        return f"[ERROR] 切換 SQLite 資料庫失敗: {exc}"
    finally:
        SQLITE_ADAPTER.close_safely(conn)


def register(mcp: Any) -> None:
    """註冊 SQLite 查詢與切換工具至 MCPServer。"""
    for fn in (
        sqlite_list_tables,
        sqlite_describe_schema,
        sqlite_run_select_query,
        sqlite_switch_database,
    ):
        mcp.tool()(fn)

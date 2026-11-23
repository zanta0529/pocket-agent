"""MySQL / MariaDB 結構探查與唯讀查詢工具群組（Command / Registrar Pattern）。"""

import os
from typing import Any

from mcp_servers._shared import (
    CONSTANTS,
    ConnAuthError,
    clean_param,
    stream_query_results,
    validate_identifier,
)
from mcp_servers.mysql.adapter import MYSQL_ADAPTER
from mcp_servers.mysql.sql_guard import MYSQL_SQL_GUARD

MAX_OUTPUT_CHARS = CONSTANTS.max_output_chars


def mysql_list_databases(
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """列出遠端 MySQL/MariaDB 伺服器上目前帳號可存取的所有資料庫 (Databases/Schemas) 清單"""
    conn = None
    sql = "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA ORDER BY SCHEMA_NAME;"
    try:
        conn = MYSQL_ADAPTER.connect(mysql_host, mysql_port, mysql_user, mysql_password, mysql_database)
        target = MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database)
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            dbs = cursor.fetchall()
        finally:
            cursor.close()

        MYSQL_ADAPTER.audit.log("mysql_list_databases", sql, target=target, status="OK", detail=f"databases={len(dbs)}")
        if not dbs:
            return "查無可見的資料庫。"
        return "\n".join(str(row[0]) for row in dbs)
    except ConnAuthError as exc:
        MYSQL_ADAPTER.audit.log(
            "mysql_list_databases",
            sql,
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database),
            status="AUTH_FAIL",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    except Exception as exc:
        MYSQL_ADAPTER.audit.log(
            "mysql_list_databases",
            sql,
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database),
            status="ERROR",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    finally:
        MYSQL_ADAPTER.close_safely(conn)


def mysql_list_tables(
    database: str = "",
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """列出遠端 MySQL/MariaDB 目標資料庫中的所有非系統資料表（未指定 database 時自動列出所有使用者資料庫之資料表）"""
    conn = None
    sql_executed = "SELECT ... FROM INFORMATION_SCHEMA.TABLES"
    try:
        explicit_db = clean_param(database)
        if explicit_db:
            try:
                explicit_db = validate_identifier(explicit_db, "database")
            except ValueError as exc:
                MYSQL_ADAPTER.audit.log(
                    "mysql_list_tables",
                    f"database={database!r}",
                    target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database),
                    status="BLOCKED",
                    detail=str(exc),
                )
                return f"[ERROR] {exc}"

        conn = MYSQL_ADAPTER.connect(mysql_host, mysql_port, mysql_user, mysql_password, mysql_database)
        target_db = (
            explicit_db
            or clean_param(mysql_database)
            or MYSQL_ADAPTER.cache.get("MYSQL_DATABASE")
            or clean_param(os.getenv("MYSQL_DATABASE"))
        )
        target = MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, target_db)

        cursor = conn.cursor()
        try:
            if target_db:
                sql_executed = (
                    "SELECT TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE IN ('BASE TABLE', 'VIEW') "
                    "ORDER BY TABLE_NAME;"
                )
                cursor.execute(sql_executed, (target_db,))
                tables = cursor.fetchall()
                MYSQL_ADAPTER.audit.log(
                    "mysql_list_tables",
                    f"{sql_executed} [database={target_db!r}]",
                    target=target,
                    status="OK",
                    detail=f"tables={len(tables)}",
                )
                if not tables:
                    return "資料庫中沒有資料表。"
                return "\n".join(
                    f"{row[0]} (VIEW)" if len(row) > 1 and str(row[1]).upper() == "VIEW" else str(row[0])
                    for row in tables
                )

            sql_executed = (
                "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW') "
                "AND TABLE_SCHEMA NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys') "
                "ORDER BY TABLE_SCHEMA, TABLE_NAME;"
            )
            cursor.execute(sql_executed)
            tables = cursor.fetchall()
            MYSQL_ADAPTER.audit.log(
                "mysql_list_tables",
                sql_executed,
                target=target,
                status="OK",
                detail=f"tables={len(tables)}",
            )
        finally:
            cursor.close()

        if not tables:
            return "資料庫中沒有資料表。"
        out_lines = []
        for row in tables:
            base = f"{row[0]}.{row[1]}" if len(row) >= 2 else str(row[0])
            if len(row) >= 3 and str(row[2]).upper() == "VIEW":
                base += " (VIEW)"
            out_lines.append(base)
        return "\n".join(out_lines)
    except ConnAuthError as exc:
        MYSQL_ADAPTER.audit.log(
            "mysql_list_tables",
            sql_executed,
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="AUTH_FAIL",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    except Exception as exc:
        MYSQL_ADAPTER.audit.log(
            "mysql_list_tables",
            sql_executed,
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="ERROR",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    finally:
        MYSQL_ADAPTER.close_safely(conn)


def mysql_describe_schema(
    table_name: str,
    database: str = "",
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """顯示指定 MySQL/MariaDB 資料表的 Schema (欄位、型別、主鍵與 NOT NULL 約束)"""
    conn = None
    sql_executed = f"DESCRIBE {table_name}"
    try:
        clean_name = table_name.strip().strip("'\"`")
        if not clean_name:
            MYSQL_ADAPTER.audit.log(
                "mysql_describe_schema",
                "table_name=''",
                target=MYSQL_ADAPTER.format_audit_target(
                    mysql_host, mysql_port, mysql_user, database or mysql_database
                ),
                status="BLOCKED",
                detail="缺少 table_name 參數",
            )
            return "[ERROR] 缺少 table_name 參數。"

        explicit_db = clean_param(database)
        try:
            if "." in clean_name:
                schema_part, tbl_part = clean_name.split(".", 1)
                explicit_schema = validate_identifier(schema_part.strip("`'\""), "schema_name")
                target_table = validate_identifier(tbl_part.strip("`'\""), "table_name")
            else:
                explicit_schema = validate_identifier(explicit_db, "database") if explicit_db else ""
                target_table = validate_identifier(clean_name, "table_name")
        except ValueError as exc:
            MYSQL_ADAPTER.audit.log(
                "mysql_describe_schema",
                f"table_name={table_name!r} database={database!r}",
                target=MYSQL_ADAPTER.format_audit_target(
                    mysql_host, mysql_port, mysql_user, database or mysql_database
                ),
                status="BLOCKED",
                detail=str(exc),
            )
            return f"[ERROR] {exc}"

        conn = MYSQL_ADAPTER.connect(mysql_host, mysql_port, mysql_user, mysql_password, mysql_database)
        target_schema = (
            explicit_schema
            or clean_param(mysql_database)
            or MYSQL_ADAPTER.cache.get("MYSQL_DATABASE")
            or clean_param(os.getenv("MYSQL_DATABASE"))
        )
        target = MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, target_schema)

        cursor = conn.cursor()
        idx_rows: list[Any] = []
        fk_rows: list[Any] = []
        try:
            if target_schema:
                sql_executed = (
                    "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_KEY "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                    "ORDER BY ORDINAL_POSITION;"
                )
                cursor.execute(sql_executed, (target_schema, target_table))
            else:
                sql_executed = (
                    "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_KEY "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_NAME = %s "
                    "AND TABLE_SCHEMA NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys') "
                    "ORDER BY TABLE_SCHEMA, ORDINAL_POSITION;"
                )
                cursor.execute(sql_executed, (target_table,))
            columns = cursor.fetchall()

            if columns:
                try:
                    if target_schema:
                        idx_sql = (
                            "SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME "
                            "FROM INFORMATION_SCHEMA.STATISTICS "
                            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                            "ORDER BY INDEX_NAME, SEQ_IN_INDEX;"
                        )
                        cursor.execute(idx_sql, (target_schema, target_table))
                    else:
                        idx_sql = (
                            "SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME "
                            "FROM INFORMATION_SCHEMA.STATISTICS "
                            "WHERE TABLE_NAME = %s "
                            "AND TABLE_SCHEMA NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys') "
                            "ORDER BY INDEX_NAME, SEQ_IN_INDEX;"
                        )
                        cursor.execute(idx_sql, (target_table,))
                    fetched_idx = cursor.fetchall()
                    if fetched_idx and fetched_idx != columns:
                        idx_rows = list(fetched_idx)

                    if target_schema:
                        fk_sql = (
                            "SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                            "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                            "AND REFERENCED_TABLE_NAME IS NOT NULL "
                            "ORDER BY ORDINAL_POSITION;"
                        )
                        cursor.execute(fk_sql, (target_schema, target_table))
                    else:
                        fk_sql = (
                            "SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                            "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                            "WHERE TABLE_NAME = %s AND REFERENCED_TABLE_NAME IS NOT NULL "
                            "AND TABLE_SCHEMA NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys') "
                            "ORDER BY ORDINAL_POSITION;"
                        )
                        cursor.execute(fk_sql, (target_table,))
                    fetched_fk = cursor.fetchall()
                    if fetched_fk and fetched_fk != columns:
                        fk_rows = list(fetched_fk)
                except Exception:
                    pass
        finally:
            cursor.close()

        if not columns:
            MYSQL_ADAPTER.audit.log(
                "mysql_describe_schema",
                f"{sql_executed} [table={target_table!r}]",
                target=target,
                status="NOT_FOUND",
                detail=f"找不到資料表 {clean_name}",
            )
            return f"找不到資料表 {clean_name}。"

        MYSQL_ADAPTER.audit.log(
            "mysql_describe_schema",
            f"{sql_executed} [table={target_table!r}]",
            target=target,
            status="OK",
            detail=f"columns={len(columns)}",
        )
        lines: list[str] = []
        for col in columns:
            col_name = str(col[0])
            col_type = str(col[1] or "TEXT").upper()
            is_nullable = str(col[2] or "").upper()
            col_default = col[3]
            col_key = str(col[4] or "").upper()
            pk_tag = " [PK]" if col_key == "PRI" else ""
            null_tag = " NOT NULL" if is_nullable == "NO" else ""
            default_tag = f" DEFAULT {col_default}" if col_default is not None else ""
            lines.append(f"{col_name} ({col_type}{pk_tag}{null_tag}{default_tag})")

        if idx_rows:
            idx_map: dict[str, tuple[bool, list[str]]] = {}
            for row in idx_rows:
                if len(row) >= 3:
                    iname = str(row[0])
                    is_uniq = not bool(int(row[1])) if str(row[1]).isdigit() else not bool(row[1])
                    cname = str(row[2])
                    if iname not in idx_map:
                        idx_map[iname] = (is_uniq, [])
                    idx_map[iname][1].append(cname)
            if idx_map:
                lines.append("\n[Indexes]")
                for iname, (is_uniq, cols) in idx_map.items():
                    u_tag = "UNIQUE " if is_uniq else ""
                    lines.append(f"- {iname}: {u_tag}({', '.join(cols)})")

        if fk_rows:
            fk_lines = [f"- {r[0]} -> {r[1]}.{r[2]}" for r in fk_rows if len(r) >= 3]
            if fk_lines:
                lines.append("\n[Foreign Keys]")
                lines.extend(fk_lines)

        return "\n".join(lines)
    except ConnAuthError as exc:
        MYSQL_ADAPTER.audit.log(
            "mysql_describe_schema",
            sql_executed,
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="AUTH_FAIL",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    except Exception as exc:
        MYSQL_ADAPTER.audit.log(
            "mysql_describe_schema",
            sql_executed,
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="ERROR",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    finally:
        MYSQL_ADAPTER.close_safely(conn)


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
        MYSQL_ADAPTER.audit.log(
            "mysql_run_select_query",
            query,
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database),
            status="BLOCKED",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"

    conn = None
    cursor = None
    stream_completed = False
    try:
        conn = MYSQL_ADAPTER.connect(mysql_host, mysql_port, mysql_user, mysql_password, mysql_database)
        target = MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database)
        cursor = MYSQL_ADAPTER.open_streaming_cursor(conn)
        cursor.execute(clean_query)

        res = stream_query_results(cursor, max_chars=MAX_OUTPUT_CHARS)
        stream_completed = res.stream_completed
        MYSQL_ADAPTER.audit.log(
            "mysql_run_select_query",
            clean_query,
            target=target,
            status="TRUNCATED" if res.truncated else "OK",
            detail=f"rows={res.shown_rows}",
        )
        return res.output
    except ConnAuthError as exc:
        MYSQL_ADAPTER.audit.log(
            "mysql_run_select_query",
            clean_query,
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database),
            status="AUTH_FAIL",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    except Exception as exc:
        MYSQL_ADAPTER.audit.log(
            "mysql_run_select_query",
            clean_query,
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database),
            status="ERROR",
            detail=str(exc),
        )
        return f"[ERROR] 查詢失敗: {exc}"
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


def register(mcp: Any) -> None:
    """註冊 MySQL 查詢與結構工具至 MCPServer。"""
    for fn in (
        mysql_list_databases,
        mysql_list_tables,
        mysql_describe_schema,
        mysql_run_select_query,
    ):
        mcp.tool()(fn)

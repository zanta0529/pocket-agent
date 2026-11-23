"""Microsoft SQL Server (MSSQL) 結構探查與唯讀查詢工具群組（Command / Registrar Pattern）。"""

from typing import Any

from mcp_servers._shared import (
    CONSTANTS,
    ConnAuthError,
    clean_param,
    stream_query_results,
    validate_identifier,
)
from mcp_servers.mssql.adapter import MSSQL_ADAPTER, adapt_placeholders
from mcp_servers.mssql.sql_guard import MSSQL_SQL_GUARD

MAX_OUTPUT_CHARS = CONSTANTS.max_output_chars


def mssql_list_databases(
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """列出遠端 Microsoft SQL Server 伺服器上所有狀態為 ONLINE 的資料庫清單"""
    conn = None
    sql = "SELECT name FROM sys.databases WHERE state_desc = 'ONLINE' ORDER BY name;"
    try:
        conn = MSSQL_ADAPTER.connect(mssql_host, mssql_port, mssql_user, mssql_password, mssql_database)
        target = MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database)
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            dbs = cursor.fetchall()
        finally:
            cursor.close()

        MSSQL_ADAPTER.audit.log("mssql_list_databases", sql, target=target, status="OK", detail=f"databases={len(dbs)}")
        if not dbs:
            return "查無可見的資料庫。"
        return "\n".join(str(row[0]) for row in dbs)
    except ConnAuthError as exc:
        MSSQL_ADAPTER.audit.log(
            "mssql_list_databases",
            sql,
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database),
            status="AUTH_FAIL",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    except Exception as exc:
        MSSQL_ADAPTER.audit.log(
            "mssql_list_databases",
            sql,
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database),
            status="ERROR",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    finally:
        MSSQL_ADAPTER.close_safely(conn)


def mssql_list_tables(
    database: str = "",
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """列出遠端 Microsoft SQL Server 目標資料庫中的所有非系統資料表"""
    conn = None
    sql_executed = "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES"
    try:
        explicit_db = clean_param(database)
        if explicit_db:
            try:
                explicit_db = validate_identifier(explicit_db, "database")
            except ValueError as exc:
                MSSQL_ADAPTER.audit.log(
                    "mssql_list_tables",
                    f"database={database!r}",
                    target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, database),
                    status="BLOCKED",
                    detail=str(exc),
                )
                return f"[ERROR] {exc}"

        conn = MSSQL_ADAPTER.connect(mssql_host, mssql_port, mssql_user, mssql_password, mssql_database)
        target_db = explicit_db or MSSQL_ADAPTER.cache.get("MSSQL_DATABASE")
        target = MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, target_db)
        cursor = conn.cursor()
        try:
            if explicit_db:
                sql_executed = (
                    f"SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM [{explicit_db}].INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW') "
                    "ORDER BY TABLE_SCHEMA, TABLE_NAME;"
                )
            else:
                sql_executed = (
                    "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW') "
                    "ORDER BY TABLE_SCHEMA, TABLE_NAME;"
                )
            cursor.execute(sql_executed)
            tables = cursor.fetchall()
        finally:
            cursor.close()

        MSSQL_ADAPTER.audit.log(
            "mssql_list_tables", sql_executed, target=target, status="OK", detail=f"tables={len(tables)}"
        )
        if not tables:
            return "資料庫中沒有資料表。"
        lines = []
        for row in tables:
            if len(row) >= 2:
                schema_name, tbl_name = str(row[0]), str(row[1])
                base = tbl_name if schema_name.lower() == "dbo" else f"{schema_name}.{tbl_name}"
                if len(row) >= 3 and str(row[2]).upper() == "VIEW":
                    base += " (VIEW)"
                lines.append(base)
            else:
                lines.append(str(row[0]))
        return "\n".join(lines)
    except ConnAuthError as exc:
        MSSQL_ADAPTER.audit.log(
            "mssql_list_tables",
            sql_executed,
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="AUTH_FAIL",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    except Exception as exc:
        MSSQL_ADAPTER.audit.log(
            "mssql_list_tables",
            sql_executed,
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="ERROR",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    finally:
        MSSQL_ADAPTER.close_safely(conn)


def mssql_describe_schema(
    table_name: str,
    database: str = "",
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """顯示指定 Microsoft SQL Server 資料表的 Schema (欄位、型別、主鍵與 NOT NULL 約束)"""
    conn = None
    sql_executed = f"DESCRIBE {table_name}"
    try:
        clean_name = table_name.strip().strip("'\"`[]")
        if not clean_name:
            MSSQL_ADAPTER.audit.log(
                "mssql_describe_schema",
                "table_name=''",
                target=MSSQL_ADAPTER.format_audit_target(
                    mssql_host, mssql_port, mssql_user, database or mssql_database
                ),
                status="BLOCKED",
                detail="缺少 table_name 參數",
            )
            return "[ERROR] 缺少 table_name 參數。"

        explicit_db = clean_param(database)
        try:
            if explicit_db:
                explicit_db = validate_identifier(explicit_db, "database")

            parts = [p.strip("[]'\"") for p in clean_name.split(".")]
            if len(parts) == 3:
                explicit_db = validate_identifier(parts[0], "database")
                target_schema = validate_identifier(parts[1], "schema_name")
                target_table = validate_identifier(parts[2], "table_name")
            elif len(parts) == 2:
                target_schema = validate_identifier(parts[0], "schema_name")
                target_table = validate_identifier(parts[1], "table_name")
            elif len(parts) == 1:
                target_schema = "dbo"
                target_table = validate_identifier(parts[0], "table_name")
            else:
                MSSQL_ADAPTER.audit.log(
                    "mssql_describe_schema",
                    f"table_name={table_name!r}",
                    target=MSSQL_ADAPTER.format_audit_target(
                        mssql_host, mssql_port, mssql_user, database or mssql_database
                    ),
                    status="BLOCKED",
                    detail=f"非法的 table_name 格式：{table_name!r}",
                )
                return f"[ERROR] 非法的 table_name 格式：{table_name!r}"
        except ValueError as exc:
            MSSQL_ADAPTER.audit.log(
                "mssql_describe_schema",
                f"table_name={table_name!r} database={database!r}",
                target=MSSQL_ADAPTER.format_audit_target(
                    mssql_host, mssql_port, mssql_user, database or mssql_database
                ),
                status="BLOCKED",
                detail=str(exc),
            )
            return f"[ERROR] {exc}"

        conn = MSSQL_ADAPTER.connect(mssql_host, mssql_port, mssql_user, mssql_password, mssql_database)
        target_db = explicit_db or MSSQL_ADAPTER.cache.get("MSSQL_DATABASE")
        target = MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, target_db)
        db_prefix = f"[{explicit_db}]." if explicit_db else ""
        raw_sql = (
            "SELECT c.COLUMN_NAME, c.DATA_TYPE, c.IS_NULLABLE, c.COLUMN_DEFAULT, "
            "CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 1 ELSE 0 END AS IS_PK, "
            "c.CHARACTER_MAXIMUM_LENGTH, c.NUMERIC_PRECISION, c.NUMERIC_SCALE "
            f"FROM {db_prefix}INFORMATION_SCHEMA.COLUMNS c "
            "LEFT JOIN ("
            "  SELECT ku.TABLE_SCHEMA, ku.TABLE_NAME, ku.COLUMN_NAME "
            f"  FROM {db_prefix}INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
            f"  JOIN {db_prefix}INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku "
            "    ON tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME "
            "    AND tc.TABLE_SCHEMA = ku.TABLE_SCHEMA "
            "    AND tc.TABLE_NAME = ku.TABLE_NAME "
            "  WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'"
            ") pk "
            "  ON c.TABLE_SCHEMA = pk.TABLE_SCHEMA "
            "  AND c.TABLE_NAME = pk.TABLE_NAME "
            "  AND c.COLUMN_NAME = pk.COLUMN_NAME "
            "WHERE c.TABLE_SCHEMA = %s AND c.TABLE_NAME = %s "
            "ORDER BY c.ORDINAL_POSITION;"
        )
        sql = adapt_placeholders(raw_sql, conn)
        sql_executed = f"{sql} [schema={target_schema!r}, table={target_table!r}]"
        cursor = conn.cursor()
        idx_rows: list[Any] = []
        fk_rows: list[Any] = []
        try:
            cursor.execute(sql, (target_schema, target_table))
            columns = cursor.fetchall()
            if columns and len(columns[0]) >= 8:
                try:
                    idx_raw_sql = (
                        "SELECT i.name AS index_name, i.is_unique, col.name AS column_name "
                        f"FROM {db_prefix}sys.indexes i "
                        f"JOIN {db_prefix}sys.index_columns ic "
                        "  ON i.object_id = ic.object_id AND i.index_id = ic.index_id "
                        f"JOIN {db_prefix}sys.columns col "
                        "  ON ic.object_id = col.object_id AND ic.column_id = col.column_id "
                        f"JOIN {db_prefix}sys.tables t ON i.object_id = t.object_id "
                        f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
                        "WHERE i.name IS NOT NULL AND s.name = %s AND t.name = %s "
                        "ORDER BY i.name, ic.key_ordinal;"
                    )
                    cursor.execute(adapt_placeholders(idx_raw_sql, conn), (target_schema, target_table))
                    fetched_idx = cursor.fetchall()
                    if fetched_idx and fetched_idx != columns:
                        idx_rows = list(fetched_idx)

                    fk_raw_sql = (
                        "SELECT cp.name AS column_name, tr.name AS ref_table, cr.name AS ref_column "
                        f"FROM {db_prefix}sys.foreign_key_columns fkc "
                        f"JOIN {db_prefix}sys.tables tp ON fkc.parent_object_id = tp.object_id "
                        f"JOIN {db_prefix}sys.schemas sp ON tp.schema_id = sp.schema_id "
                        f"JOIN {db_prefix}sys.columns cp "
                        "  ON fkc.parent_object_id = cp.object_id AND fkc.parent_column_id = cp.column_id "
                        f"JOIN {db_prefix}sys.tables tr ON fkc.referenced_object_id = tr.object_id "
                        f"JOIN {db_prefix}sys.columns cr "
                        "  ON fkc.referenced_object_id = cr.object_id AND fkc.referenced_column_id = cr.column_id "
                        "WHERE sp.name = %s AND tp.name = %s "
                        "ORDER BY fkc.constraint_column_id;"
                    )
                    cursor.execute(adapt_placeholders(fk_raw_sql, conn), (target_schema, target_table))
                    fetched_fk = cursor.fetchall()
                    if fetched_fk and fetched_fk != columns:
                        fk_rows = list(fetched_fk)
                except Exception:
                    pass
        finally:
            cursor.close()

        if not columns:
            MSSQL_ADAPTER.audit.log(
                "mssql_describe_schema",
                sql_executed,
                target=target,
                status="NOT_FOUND",
                detail=f"找不到資料表 {clean_name}",
            )
            return f"找不到資料表 {clean_name}。"

        MSSQL_ADAPTER.audit.log(
            "mssql_describe_schema",
            sql_executed,
            target=target,
            status="OK",
            detail=f"columns={len(columns)}",
        )
        lines: list[str] = []
        for col in columns:
            col_name = str(col[0])
            col_type = str(col[1] or "NVARCHAR").upper()
            is_nullable = str(col[2] or "").upper()
            col_default = col[3]
            is_pk = bool(col[4])
            char_len = col[5] if len(col) > 5 else None
            num_prec = col[6] if len(col) > 6 else None
            num_scale = col[7] if len(col) > 7 else None
            if col_type in ("VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "BINARY", "VARBINARY") and char_len is not None:
                len_str = "MAX" if int(char_len) == -1 else str(char_len)
                col_type = f"{col_type}({len_str})"
            elif col_type in ("DECIMAL", "NUMERIC") and num_prec is not None:
                scale_str = f",{num_scale}" if num_scale is not None else ""
                col_type = f"{col_type}({num_prec}{scale_str})"
            pk_tag = " [PK]" if is_pk else ""
            null_tag = " NOT NULL" if is_nullable == "NO" else ""
            default_tag = f" DEFAULT {col_default}" if col_default is not None else ""
            lines.append(f"{col_name} ({col_type}{pk_tag}{null_tag}{default_tag})")

        if idx_rows:
            idx_map: dict[str, tuple[bool, list[str]]] = {}
            for row in idx_rows:
                if len(row) >= 3:
                    iname = str(row[0])
                    is_uniq = bool(row[1])
                    if iname not in idx_map:
                        idx_map[iname] = (is_uniq, [])
                    idx_map[iname][1].append(str(row[2]))
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
        MSSQL_ADAPTER.audit.log(
            "mssql_describe_schema",
            sql_executed,
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="AUTH_FAIL",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    except Exception as exc:
        MSSQL_ADAPTER.audit.log(
            "mssql_describe_schema",
            sql_executed,
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="ERROR",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    finally:
        MSSQL_ADAPTER.close_safely(conn)


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
        MSSQL_ADAPTER.audit.log(
            "mssql_run_select_query",
            query,
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database),
            status="BLOCKED",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"

    conn = None
    try:
        conn = MSSQL_ADAPTER.connect(mssql_host, mssql_port, mssql_user, mssql_password, mssql_database)
        target = MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database)
        cursor = MSSQL_ADAPTER.open_streaming_cursor(conn)
        try:
            cursor.execute(clean_query)
            res = stream_query_results(cursor, max_chars=MAX_OUTPUT_CHARS)
        finally:
            cursor.close()

        MSSQL_ADAPTER.audit.log(
            "mssql_run_select_query",
            clean_query,
            target=target,
            status="TRUNCATED" if res.truncated else "OK",
            detail=f"rows={res.shown_rows}",
        )
        return res.output
    except ConnAuthError as exc:
        MSSQL_ADAPTER.audit.log(
            "mssql_run_select_query",
            clean_query,
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database),
            status="AUTH_FAIL",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    except Exception as exc:
        MSSQL_ADAPTER.audit.log(
            "mssql_run_select_query",
            clean_query,
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database),
            status="ERROR",
            detail=str(exc),
        )
        return f"[ERROR] 查詢失敗: {exc}"
    finally:
        MSSQL_ADAPTER.close_safely(conn)


def register(mcp: Any) -> None:
    """註冊 MSSQL 查詢與結構工具至 MCPServer。"""
    for fn in (
        mssql_list_databases,
        mssql_list_tables,
        mssql_describe_schema,
        mssql_run_select_query,
    ):
        mcp.tool()(fn)

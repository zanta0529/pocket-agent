"""Microsoft SQL Server (MSSQL) 結構探查與物件定義檢視工具群組（Command / Registrar Pattern）。"""

from typing import Any

from mcp_servers._shared import (
    CONSTANTS,
    DEFAULT_REDACTION_POLICY,
    clean_param,
    t,
    validate_identifier,
)
from mcp_servers.mssql.adapter import MSSQL_ADAPTER, adapt_placeholders
from mcp_servers.mssql.tools._common import (
    format_mssql_error,
    parse_mssql_target,
    run_readonly_tool,
    truncate_text,
)


def mssql_list_databases(
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """列出遠端 Microsoft SQL Server 伺服器上所有狀態為 ONLINE 的資料庫清單"""
    sql = "SELECT name FROM sys.databases WHERE state_desc = 'ONLINE' ORDER BY name;"

    def _run(conn: Any, _target_db: str, target: str) -> str:
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            dbs = cursor.fetchall()
        finally:
            cursor.close()
        MSSQL_ADAPTER.audit.log("mssql_list_databases", sql, target=target, status="OK", detail=f"databases={len(dbs)}")
        if not dbs:
            return t("mssql_mcp.no_databases")
        return truncate_text("\n".join(str(row[0]) for row in dbs), max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "mssql_list_databases",
        _run,
        sql_hint=sql,
        mssql_host=mssql_host,
        mssql_port=mssql_port,
        mssql_user=mssql_user,
        mssql_password=mssql_password,
        mssql_database=mssql_database,
    )


def mssql_list_tables(
    database: str = "",
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """列出遠端 Microsoft SQL Server 目標資料庫中的所有非系統資料表與檢視表"""
    explicit_db = clean_param(database)
    if explicit_db:
        try:
            explicit_db = validate_identifier(explicit_db, "database")
        except ValueError as exc:
            return format_mssql_error(
                "mssql_list_tables",
                exc,
                sql=f"database={database!r}",
                target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, database),
                status="BLOCKED",
            )

    def _run(conn: Any, _target_db: str, target: str) -> str:
        db_prefix = f"[{explicit_db}]." if explicit_db else ""
        sql_executed = (
            f"SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM {db_prefix}INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW') "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME;"
        )
        cursor = conn.cursor()
        try:
            cursor.execute(sql_executed)
            tables = cursor.fetchall()
        finally:
            cursor.close()

        MSSQL_ADAPTER.audit.log(
            "mssql_list_tables", sql_executed, target=target, status="OK", detail=f"tables={len(tables)}"
        )
        if not tables:
            return t("mssql_mcp.no_tables")
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
        return truncate_text("\n".join(lines), max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "mssql_list_tables",
        _run,
        sql_hint="SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES",
        mssql_host=mssql_host,
        mssql_port=mssql_port,
        mssql_user=mssql_user,
        mssql_password=mssql_password,
        mssql_database=mssql_database,
        explicit_db=explicit_db,
    )


def _format_mssql_column(col: tuple[Any, ...]) -> str:
    col_name = str(col[0])
    col_type = str(col[1] or "NVARCHAR").upper()
    is_nullable = str(col[2] or "").upper()
    col_default = col[3]
    is_pk = bool(col[4])
    char_len = col[5] if len(col) > 5 else None
    num_prec = col[6] if len(col) > 6 else None
    num_scale = col[7] if len(col) > 7 else None
    is_identity = bool(col[8]) if len(col) > 8 else False
    is_computed = bool(col[9]) if len(col) > 9 else False
    col_desc = str(col[10] or "").strip() if len(col) > 10 else ""

    if col_type in ("VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "BINARY", "VARBINARY") and char_len is not None:
        len_str = "MAX" if int(char_len) == -1 else str(char_len)
        col_type = f"{col_type}({len_str})"
    elif col_type in ("DECIMAL", "NUMERIC") and num_prec is not None:
        scale_str = f",{num_scale}" if num_scale is not None else ""
        col_type = f"{col_type}({num_prec}{scale_str})"

    pk_tag = " [PK]" if is_pk else ""
    null_tag = " NOT NULL" if is_nullable == "NO" else ""
    default_tag = f" DEFAULT {DEFAULT_REDACTION_POLICY.redact(str(col_default))}" if col_default is not None else ""
    ident_tag = " [IDENTITY]" if is_identity else ""
    comp_tag = " [COMPUTED]" if is_computed else ""
    desc_tag = f" -- {DEFAULT_REDACTION_POLICY.redact(col_desc)}" if col_desc else ""
    return f"{col_name} ({col_type}{pk_tag}{null_tag}{default_tag}){ident_tag}{comp_tag}{desc_tag}"


def _format_mssql_indexes_and_fks(
    lines: list[str],
    idx_rows: list[tuple[Any, ...]],
    fk_rows: list[tuple[Any, ...]],
) -> None:
    if idx_rows:
        idx_map: dict[str, dict[str, Any]] = {}
        for row in idx_rows:
            if len(row) < 3:
                continue
            iname = str(row[0])
            is_uniq = bool(row[1])
            cname = str(row[2])
            is_incl = bool(row[3]) if len(row) > 3 else False
            flt = str(row[4] or "").strip() if len(row) > 4 else ""
            entry = idx_map.setdefault(iname, {"unique": is_uniq, "keys": [], "includes": [], "filter": flt})
            if is_incl:
                entry["includes"].append(cname)
            else:
                entry["keys"].append(cname)
        if idx_map:
            lines.append("\n[Indexes]")
            for iname, meta in idx_map.items():
                u_tag = "UNIQUE " if meta["unique"] else ""
                inc_tag = f" INCLUDE ({', '.join(meta['includes'])})" if meta["includes"] else ""
                flt_tag = f" WHERE {meta['filter']}" if meta["filter"] else ""
                lines.append(f"- {iname}: {u_tag}({', '.join(meta['keys'])}){inc_tag}{flt_tag}")

    if fk_rows:
        fk_lines: list[str] = []
        for r in fk_rows:
            if len(r) >= 4:
                ref_schema = str(r[1] or "dbo")
                target_tbl = str(r[2]) if ref_schema.lower() == "dbo" else f"{ref_schema}.{r[2]}"
                fk_lines.append(f"- {r[0]} -> {target_tbl}.{r[3]}")
            elif len(r) >= 3:
                fk_lines.append(f"- {r[0]} -> {r[1]}.{r[2]}")
        if fk_lines:
            lines.append("\n[Foreign Keys]")
            lines.extend(fk_lines)


def mssql_describe_schema(
    table_name: str,
    database: str = "",
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """顯示指定 SQL Server 資料表的 Schema（欄位、型別、主鍵、IDENTITY、註解、含 INCLUDE 之索引與外鍵）"""
    clean_name = (table_name or "").strip().strip("'\"`[]")
    if not clean_name:
        return format_mssql_error(
            "mssql_describe_schema",
            ValueError(t("mssql_mcp.missing_table_name")),
            sql="table_name=''",
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="BLOCKED",
        )

    try:
        explicit_db, target_schema, target_table = parse_mssql_target(clean_name, database, default_schema="dbo")
    except ValueError as exc:
        return format_mssql_error(
            "mssql_describe_schema",
            exc,
            sql=f"table_name={table_name!r} database={database!r}",
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="BLOCKED",
        )

    def _run(conn: Any, _target_db: str, target: str) -> str:
        db_prefix = f"[{explicit_db}]." if explicit_db else ""
        raw_sql = (
            "SELECT c.COLUMN_NAME, c.DATA_TYPE, c.IS_NULLABLE, c.COLUMN_DEFAULT, "
            "CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 1 ELSE 0 END AS IS_PK, "
            "c.CHARACTER_MAXIMUM_LENGTH, c.NUMERIC_PRECISION, c.NUMERIC_SCALE, "
            "COALESCE(sc.is_identity, 0), COALESCE(sc.is_computed, 0), "
            "CAST(ep.value AS NVARCHAR(500)) "
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
            f"LEFT JOIN {db_prefix}sys.schemas ss ON ss.name = c.TABLE_SCHEMA "
            f"LEFT JOIN {db_prefix}sys.tables st ON st.schema_id = ss.schema_id AND st.name = c.TABLE_NAME "
            f"LEFT JOIN {db_prefix}sys.columns sc ON sc.object_id = st.object_id AND sc.name = c.COLUMN_NAME "
            f"LEFT JOIN {db_prefix}sys.extended_properties ep "
            "  ON ep.major_id = st.object_id AND ep.minor_id = sc.column_id "
            "  AND ep.class = 1 AND ep.name = 'MS_Description' "
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
            columns = list(cursor.fetchall() or [])
            if columns:
                try:
                    idx_raw_sql = (
                        "SELECT i.name AS index_name, i.is_unique, col.name AS column_name, "
                        "ic.is_included_column, i.filter_definition "
                        f"FROM {db_prefix}sys.indexes i "
                        f"JOIN {db_prefix}sys.index_columns ic "
                        "  ON i.object_id = ic.object_id AND i.index_id = ic.index_id "
                        f"JOIN {db_prefix}sys.columns col "
                        "  ON ic.object_id = col.object_id AND ic.column_id = col.column_id "
                        f"JOIN {db_prefix}sys.tables t ON i.object_id = t.object_id "
                        f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
                        "WHERE i.name IS NOT NULL AND s.name = %s AND t.name = %s "
                        "ORDER BY i.name, ic.is_included_column, ic.key_ordinal;"
                    )
                    cursor.execute(adapt_placeholders(idx_raw_sql, conn), (target_schema, target_table))
                    idx_rows = list(cursor.fetchall() or [])

                    fk_raw_sql = (
                        "SELECT cp.name, sr.name, tr.name, cr.name "
                        f"FROM {db_prefix}sys.foreign_key_columns fkc "
                        f"JOIN {db_prefix}sys.tables tp ON fkc.parent_object_id = tp.object_id "
                        f"JOIN {db_prefix}sys.schemas sp ON tp.schema_id = sp.schema_id "
                        f"JOIN {db_prefix}sys.columns cp "
                        "  ON fkc.parent_object_id = cp.object_id AND fkc.parent_column_id = cp.column_id "
                        f"JOIN {db_prefix}sys.tables tr ON fkc.referenced_object_id = tr.object_id "
                        f"JOIN {db_prefix}sys.schemas sr ON tr.schema_id = sr.schema_id "
                        f"JOIN {db_prefix}sys.columns cr "
                        "  ON fkc.referenced_object_id = cr.object_id AND fkc.referenced_column_id = cr.column_id "
                        "WHERE sp.name = %s AND tp.name = %s "
                        "ORDER BY fkc.constraint_column_id;"
                    )
                    cursor.execute(adapt_placeholders(fk_raw_sql, conn), (target_schema, target_table))
                    fk_rows = list(cursor.fetchall() or [])
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
                detail=clean_name,
            )
            return t("mssql_mcp.table_not_found", table=clean_name)

        MSSQL_ADAPTER.audit.log(
            "mssql_describe_schema",
            sql_executed,
            target=target,
            status="OK",
            detail=f"columns={len(columns)}",
        )
        lines = [_format_mssql_column(col) for col in columns]
        _format_mssql_indexes_and_fks(lines, idx_rows, fk_rows)
        return truncate_text("\n".join(lines), max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "mssql_describe_schema",
        _run,
        sql_hint=f"DESCRIBE {table_name}",
        mssql_host=mssql_host,
        mssql_port=mssql_port,
        mssql_user=mssql_user,
        mssql_password=mssql_password,
        mssql_database=mssql_database,
        explicit_db=explicit_db,
    )


def mssql_get_object_definition(
    object_name: str,
    database: str = "",
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """唯讀取得 View／Stored Procedure／Function／Trigger 的 T-SQL 定義或資料表完整結構 DDL 摘要"""
    clean_name = (object_name or "").strip().strip("'\"`[]")
    if not clean_name:
        return format_mssql_error(
            "mssql_get_object_definition",
            ValueError(t("mssql_mcp.missing_object_name")),
            sql="object_name=''",
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="BLOCKED",
        )

    try:
        explicit_db, target_schema, target_obj = parse_mssql_target(clean_name, database, default_schema="dbo")
    except ValueError as exc:
        return format_mssql_error(
            "mssql_get_object_definition",
            exc,
            sql=f"object_name={object_name!r} database={database!r}",
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="BLOCKED",
        )

    def _run(conn: Any, _target_db: str, target: str) -> str:
        db_prefix = f"[{explicit_db}]." if explicit_db else ""
        sql = adapt_placeholders(
            "SELECT o.type, o.type_desc, sm.definition "
            f"FROM {db_prefix}sys.objects o "
            f"JOIN {db_prefix}sys.schemas s ON o.schema_id = s.schema_id "
            f"LEFT JOIN {db_prefix}sys.sql_modules sm ON o.object_id = sm.object_id "
            "WHERE s.name = %s AND o.name = %s;",
            conn,
        )
        cursor = conn.cursor()
        try:
            cursor.execute(sql, (target_schema, target_obj))
            row = cursor.fetchone()
        finally:
            cursor.close()

        qualified = f"{target_schema}.{target_obj}"
        if not row:
            MSSQL_ADAPTER.audit.log(
                "mssql_get_object_definition",
                sql,
                target=target,
                status="NOT_FOUND",
                detail=qualified,
            )
            return t("mssql_mcp.object_not_found", obj=qualified)

        obj_type = str(row[0] or "").strip().upper()
        type_desc = str(row[1] or "").strip()
        definition = row[2]

        if obj_type == "U":
            schema_summary = mssql_describe_schema(
                qualified,
                database=explicit_db,
                mssql_host=mssql_host,
                mssql_port=mssql_port,
                mssql_user=mssql_user,
                mssql_password=mssql_password,
                mssql_database=mssql_database,
            )
            MSSQL_ADAPTER.audit.log(
                "mssql_get_object_definition",
                f"TABLE_SUMMARY {qualified}",
                target=target,
                status="OK",
            )
            return f"[{type_desc}: {qualified}]\n{schema_summary}"

        if not definition:
            MSSQL_ADAPTER.audit.log(
                "mssql_get_object_definition",
                sql,
                target=target,
                status="NOT_FOUND",
                detail=f"{qualified} encrypted/restricted",
            )
            return t("mssql_mcp.object_def_unavailable", obj=qualified)

        MSSQL_ADAPTER.audit.log("mssql_get_object_definition", sql, target=target, status="OK", detail=qualified)
        return truncate_text(f"[{type_desc}: {qualified}]\n{definition}", max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "mssql_get_object_definition",
        _run,
        sql_hint=f"OBJECT_DEFINITION {object_name}",
        mssql_host=mssql_host,
        mssql_port=mssql_port,
        mssql_user=mssql_user,
        mssql_password=mssql_password,
        mssql_database=mssql_database,
        explicit_db=explicit_db,
        error_key="mssql_mcp.object_def_failed",
    )


def register(mcp: Any) -> None:
    """註冊 MSSQL 結構探查與物件定義工具至 MCPServer。"""
    for fn in (
        mssql_list_databases,
        mssql_list_tables,
        mssql_describe_schema,
        mssql_get_object_definition,
    ):
        mcp.tool()(fn)

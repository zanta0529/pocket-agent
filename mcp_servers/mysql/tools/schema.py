"""MySQL / MariaDB 結構探查與 DDL 檢視工具群組（Command / Registrar Pattern）。"""

from typing import Any

from mcp_servers._shared import (
    CONSTANTS,
    DEFAULT_REDACTION_POLICY,
    clean_param,
    t,
    validate_identifier,
)
from mcp_servers.mysql.adapter import MYSQL_ADAPTER
from mcp_servers.mysql.tools._common import (
    SYSTEM_SCHEMAS_SQL,
    format_mysql_error,
    parse_mysql_target,
    run_readonly_tool,
    truncate_text,
)


def mysql_list_databases(
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """列出遠端 MySQL/MariaDB 伺服器上目前帳號可存取的所有資料庫 (Databases/Schemas) 清單"""
    sql = "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA ORDER BY SCHEMA_NAME;"

    def _run(conn: Any, _target_schema: str, target: str) -> str:
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            dbs = cursor.fetchall()
        finally:
            cursor.close()
        MYSQL_ADAPTER.audit.log("mysql_list_databases", sql, target=target, status="OK", detail=f"databases={len(dbs)}")
        if not dbs:
            return t("mysql_mcp.no_databases")
        return truncate_text("\n".join(str(row[0]) for row in dbs), max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "mysql_list_databases",
        _run,
        sql_hint=sql,
        mysql_host=mysql_host,
        mysql_port=mysql_port,
        mysql_user=mysql_user,
        mysql_password=mysql_password,
        mysql_database=mysql_database,
    )


def mysql_list_tables(
    database: str = "",
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """列出遠端 MySQL/MariaDB 目標資料庫中的所有非系統資料表（未指定 database 時自動列出所有使用者資料庫之資料表）"""
    explicit_db = clean_param(database)
    if explicit_db:
        try:
            explicit_db = validate_identifier(explicit_db, "database")
        except ValueError as exc:
            return format_mysql_error(
                "mysql_list_tables",
                exc,
                sql=f"database={database!r}",
                target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database),
                status="BLOCKED",
            )

    def _run(conn: Any, target_db: str, target: str) -> str:
        cursor = conn.cursor()
        try:
            if target_db:
                sql = (
                    "SELECT TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE IN ('BASE TABLE', 'VIEW') "
                    "ORDER BY TABLE_NAME;"
                )
                cursor.execute(sql, (target_db,))
                tables = cursor.fetchall()
                MYSQL_ADAPTER.audit.log(
                    "mysql_list_tables",
                    f"{sql} [database={target_db!r}]",
                    target=target,
                    status="OK",
                    detail=f"tables={len(tables)}",
                )
                if not tables:
                    return t("mysql_mcp.no_tables")
                lines = [
                    f"{row[0]} (VIEW)" if len(row) > 1 and str(row[1]).upper() == "VIEW" else str(row[0])
                    for row in tables
                ]
                return truncate_text("\n".join(lines), max_chars=CONSTANTS.max_output_chars)

            sql = (
                "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW') "
                f"AND TABLE_SCHEMA NOT IN {SYSTEM_SCHEMAS_SQL} "
                "ORDER BY TABLE_SCHEMA, TABLE_NAME;"
            )
            cursor.execute(sql)
            tables = cursor.fetchall()
            MYSQL_ADAPTER.audit.log(
                "mysql_list_tables",
                sql,
                target=target,
                status="OK",
                detail=f"tables={len(tables)}",
            )
        finally:
            cursor.close()

        if not tables:
            return t("mysql_mcp.no_tables")
        out_lines = []
        for row in tables:
            base = f"{row[0]}.{row[1]}" if len(row) >= 2 else str(row[0])
            if len(row) >= 3 and str(row[2]).upper() == "VIEW":
                base += " (VIEW)"
            out_lines.append(base)
        return truncate_text("\n".join(out_lines), max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "mysql_list_tables",
        _run,
        sql_hint="SELECT ... FROM INFORMATION_SCHEMA.TABLES",
        mysql_host=mysql_host,
        mysql_port=mysql_port,
        mysql_user=mysql_user,
        mysql_password=mysql_password,
        mysql_database=mysql_database,
        explicit_schema=explicit_db,
    )


def _format_column_line(col: tuple[Any, ...], has_schema_prefix: bool) -> str:
    offset = 1 if has_schema_prefix else 0
    col_name = str(col[offset])
    col_type = str(col[offset + 1] or "TEXT").upper()
    is_nullable = str(col[offset + 2] or "").upper()
    col_default = col[offset + 3]
    col_key = str(col[offset + 4] or "").upper()
    extra = str(col[offset + 5] or "").strip() if len(col) > offset + 5 else ""
    comment = str(col[offset + 6] or "").strip() if len(col) > offset + 6 else ""

    pk_tag = " [PK]" if col_key == "PRI" else ""
    null_tag = " NOT NULL" if is_nullable == "NO" else ""
    default_tag = f" DEFAULT {DEFAULT_REDACTION_POLICY.redact(str(col_default))}" if col_default is not None else ""
    extra_tag = f" [{extra}]" if extra else ""
    comment_tag = f" -- {DEFAULT_REDACTION_POLICY.redact(comment)}" if comment else ""
    return f"{col_name} ({col_type}{pk_tag}{null_tag}{default_tag}){extra_tag}{comment_tag}"


def _append_indexes_and_fks(
    lines: list[str],
    idx_rows: list[tuple[Any, ...]],
    fk_rows: list[tuple[Any, ...]],
) -> None:
    if idx_rows:
        idx_map: dict[str, tuple[bool, str, list[str]]] = {}
        for row in idx_rows:
            if len(row) < 3:
                continue
            iname = str(row[0])
            is_uniq = not bool(int(row[1])) if str(row[1]).isdigit() else not bool(row[1])
            cname = str(row[2])
            itype = str(row[3]).upper() if len(row) >= 4 and row[3] else ""
            if iname not in idx_map:
                idx_map[iname] = (is_uniq, itype, [])
            idx_map[iname][2].append(cname)
        if idx_map:
            lines.append("\n[Indexes]")
            for iname, (is_uniq, itype, cols) in idx_map.items():
                u_tag = "UNIQUE " if is_uniq else ""
                t_tag = f" [{itype}]" if itype and itype != "BTREE" else ""
                lines.append(f"- {iname}: {u_tag}({', '.join(cols)}){t_tag}")

    if fk_rows:
        fk_lines: list[str] = []
        for r in fk_rows:
            if len(r) >= 4 and r[1]:
                fk_lines.append(f"- {r[0]} -> {r[1]}.{r[2]}.{r[3]}")
            elif len(r) >= 3:
                fk_lines.append(f"- {r[0]} -> {r[1]}.{r[2]}")
        if fk_lines:
            lines.append("\n[Foreign Keys]")
            lines.extend(fk_lines)


def mysql_describe_schema(
    table_name: str,
    database: str = "",
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """顯示指定 MySQL/MariaDB 資料表的 Schema（欄位、型別、主鍵、EXTRA、註解、索引與外鍵）"""
    clean_name = (table_name or "").strip().strip("'\"`")
    if not clean_name:
        return format_mysql_error(
            "mysql_describe_schema",
            ValueError(t("mysql_mcp.missing_table_name")),
            sql="table_name=''",
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="BLOCKED",
        )

    try:
        explicit_schema, target_table = parse_mysql_target(clean_name, database)
    except ValueError as exc:
        return format_mysql_error(
            "mysql_describe_schema",
            exc,
            sql=f"table_name={table_name!r} database={database!r}",
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="BLOCKED",
        )

    def _run(conn: Any, target_schema: str, target: str) -> str:
        cursor = conn.cursor()
        idx_rows: list[Any] = []
        fk_rows: list[Any] = []
        try:
            if target_schema:
                sql = (
                    "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, "
                    "COLUMN_KEY, EXTRA, COLUMN_COMMENT "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                    "ORDER BY ORDINAL_POSITION;"
                )
                cursor.execute(sql, (target_schema, target_table))
            else:
                sql = (
                    "SELECT TABLE_SCHEMA, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, "
                    "COLUMN_KEY, EXTRA, COLUMN_COMMENT "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_NAME = %s "
                    f"AND TABLE_SCHEMA NOT IN {SYSTEM_SCHEMAS_SQL} "
                    "ORDER BY TABLE_SCHEMA, ORDINAL_POSITION;"
                )
                cursor.execute(sql, (target_table,))
            columns = cursor.fetchall()

            if columns:
                try:
                    if target_schema:
                        idx_sql = (
                            "SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME, INDEX_TYPE "
                            "FROM INFORMATION_SCHEMA.STATISTICS "
                            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                            "ORDER BY INDEX_NAME, SEQ_IN_INDEX;"
                        )
                        cursor.execute(idx_sql, (target_schema, target_table))
                        idx_rows = list(cursor.fetchall() or [])

                        fk_sql = (
                            "SELECT COLUMN_NAME, REFERENCED_TABLE_SCHEMA, "
                            "REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                            "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                            "AND REFERENCED_TABLE_NAME IS NOT NULL "
                            "ORDER BY ORDINAL_POSITION;"
                        )
                        cursor.execute(fk_sql, (target_schema, target_table))
                        fk_rows = list(cursor.fetchall() or [])
                    else:
                        idx_sql = (
                            "SELECT TABLE_SCHEMA, INDEX_NAME, NON_UNIQUE, COLUMN_NAME, INDEX_TYPE "
                            "FROM INFORMATION_SCHEMA.STATISTICS "
                            "WHERE TABLE_NAME = %s "
                            f"AND TABLE_SCHEMA NOT IN {SYSTEM_SCHEMAS_SQL} "
                            "ORDER BY TABLE_SCHEMA, INDEX_NAME, SEQ_IN_INDEX;"
                        )
                        cursor.execute(idx_sql, (target_table,))
                        idx_rows = list(cursor.fetchall() or [])

                        fk_sql = (
                            "SELECT TABLE_SCHEMA, COLUMN_NAME, REFERENCED_TABLE_SCHEMA, "
                            "REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                            "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                            "WHERE TABLE_NAME = %s AND REFERENCED_TABLE_NAME IS NOT NULL "
                            f"AND TABLE_SCHEMA NOT IN {SYSTEM_SCHEMAS_SQL} "
                            "ORDER BY TABLE_SCHEMA, ORDINAL_POSITION;"
                        )
                        cursor.execute(fk_sql, (target_table,))
                        fk_rows = list(cursor.fetchall() or [])
                except Exception:
                    pass
        finally:
            cursor.close()

        if not columns:
            MYSQL_ADAPTER.audit.log(
                "mysql_describe_schema",
                f"{sql} [table={target_table!r}]",
                target=target,
                status="NOT_FOUND",
                detail=f"找不到資料表 {clean_name}",
            )
            return t("mysql_mcp.table_not_found", table=clean_name)

        MYSQL_ADAPTER.audit.log(
            "mysql_describe_schema",
            f"{sql} [table={target_table!r}]",
            target=target,
            status="OK",
            detail=f"columns={len(columns)}",
        )

        if target_schema:
            lines = [_format_column_line(col, has_schema_prefix=False) for col in columns]
            _append_indexes_and_fks(lines, idx_rows, fk_rows)
            return truncate_text("\n".join(lines), max_chars=CONSTANTS.max_output_chars)

        grouped_cols: dict[str, list[tuple[Any, ...]]] = {}
        for col in columns:
            grouped_cols.setdefault(str(col[0]), []).append(col)
        grouped_idx: dict[str, list[tuple[Any, ...]]] = {}
        for row in idx_rows:
            if len(row) >= 4:
                grouped_idx.setdefault(str(row[0]), []).append(row[1:])
        grouped_fk: dict[str, list[tuple[Any, ...]]] = {}
        for row in fk_rows:
            if len(row) >= 4:
                grouped_fk.setdefault(str(row[0]), []).append(row[1:])

        multi_schema = len(grouped_cols) > 1
        lines = []
        for idx, (sch, sch_cols) in enumerate(grouped_cols.items()):
            if multi_schema:
                if idx > 0:
                    lines.append("")
                lines.append(f"=== {sch}.{target_table} ===")
            lines.extend(_format_column_line(col, has_schema_prefix=True) for col in sch_cols)
            _append_indexes_and_fks(lines, grouped_idx.get(sch, []), grouped_fk.get(sch, []))
        return truncate_text("\n".join(lines), max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "mysql_describe_schema",
        _run,
        sql_hint=f"DESCRIBE {table_name}",
        mysql_host=mysql_host,
        mysql_port=mysql_port,
        mysql_user=mysql_user,
        mysql_password=mysql_password,
        mysql_database=mysql_database,
        explicit_schema=explicit_schema,
    )


def mysql_show_create_table(
    table_name: str,
    database: str = "",
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """唯讀取得指定 MySQL/MariaDB 資料表或檢視表的完整 SHOW CREATE TABLE DDL 定義"""
    clean_name = (table_name or "").strip().strip("'\"`")
    if not clean_name:
        return format_mysql_error(
            "mysql_show_create_table",
            ValueError(t("mysql_mcp.missing_table_name")),
            sql="table_name=''",
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="BLOCKED",
        )

    try:
        explicit_schema, target_table = parse_mysql_target(clean_name, database)
    except ValueError as exc:
        return format_mysql_error(
            "mysql_show_create_table",
            exc,
            sql=f"table_name={table_name!r} database={database!r}",
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="BLOCKED",
        )

    def _run(conn: Any, target_schema: str, target: str) -> str:
        cursor = conn.cursor()
        try:
            resolved_schema = target_schema
            if not resolved_schema:
                lookup_sql = (
                    "SELECT TABLE_SCHEMA FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_NAME = %s "
                    f"AND TABLE_SCHEMA NOT IN {SYSTEM_SCHEMAS_SQL} "
                    "ORDER BY TABLE_SCHEMA;"
                )
                cursor.execute(lookup_sql, (target_table,))
                found_schemas = [str(r[0]) for r in cursor.fetchall()]
                if not found_schemas:
                    MYSQL_ADAPTER.audit.log(
                        "mysql_show_create_table",
                        lookup_sql,
                        target=target,
                        status="NOT_FOUND",
                        detail=target_table,
                    )
                    return t("mysql_mcp.table_not_found", table=clean_name)
                if len(found_schemas) > 1:
                    MYSQL_ADAPTER.audit.log(
                        "mysql_show_create_table",
                        lookup_sql,
                        target=target,
                        status="BLOCKED",
                        detail=f"ambiguous={found_schemas}",
                    )
                    msg = t(
                        "mysql_mcp.table_ambiguous",
                        table=target_table,
                        schemas=", ".join(found_schemas),
                    )
                    return f"[ERROR] {msg}"
                resolved_schema = found_schemas[0]

            ddl_sql = f"SHOW CREATE TABLE `{resolved_schema}`.`{target_table}`;"
            cursor.execute(ddl_sql)
            row = cursor.fetchone()
        finally:
            cursor.close()

        if not row or len(row) < 2:
            return t("mysql_mcp.table_not_found", table=f"{resolved_schema}.{target_table}")

        MYSQL_ADAPTER.audit.log(
            "mysql_show_create_table",
            ddl_sql,
            target=target,
            status="OK",
        )
        return truncate_text(str(row[1]), max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "mysql_show_create_table",
        _run,
        sql_hint=f"SHOW CREATE TABLE {table_name}",
        mysql_host=mysql_host,
        mysql_port=mysql_port,
        mysql_user=mysql_user,
        mysql_password=mysql_password,
        mysql_database=mysql_database,
        explicit_schema=explicit_schema,
        error_key="mysql_mcp.ddl_failed",
    )


def register(mcp: Any) -> None:
    """註冊 MySQL 資料庫結構與 DDL 工具至 MCPServer。"""
    for fn in (
        mysql_list_databases,
        mysql_list_tables,
        mysql_describe_schema,
        mysql_show_create_table,
    ):
        mcp.tool()(fn)

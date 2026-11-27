"""MySQL / MariaDB 結構健康檢查與輕量 CHECK TABLE 工具群組（Command / Registrar Pattern）。"""

from typing import Any

from mcp_servers._shared import (
    CONSTANTS,
    DEFAULT_REDACTION_POLICY,
    find_redundant_prefix_indexes,
    format_bytes,
    t,
    truncate_health_report,
)
from mcp_servers.mysql.adapter import MYSQL_ADAPTER
from mcp_servers.mysql.tools._common import (
    SYSTEM_SCHEMAS_SQL,
    format_mysql_error,
    parse_mysql_target,
    run_readonly_tool,
)

MYSQL_CHECK_TABLE_MAX_BYTES = CONSTANTS.mysql_check_table_max_bytes
DB_HEALTH_MAX_TABLES = CONSTANTS.db_health_max_tables
DB_FRAGMENTATION_WARN_RATIO = CONSTANTS.db_fragmentation_warn_ratio
DB_FRAGMENTATION_MIN_FREE_BYTES = CONSTANTS.db_fragmentation_min_free_bytes
DB_AUTOINC_WARN_RATIO = CONSTANTS.db_autoinc_warn_ratio
_METADATA_BATCH_SIZE = 100


def _mysql_int_type_max(column_type: str) -> int | None:
    col_t = (column_type or "").strip().lower()
    unsigned = "unsigned" in col_t
    if col_t.startswith("tinyint"):
        return 255 if unsigned else 127
    if col_t.startswith("smallint"):
        return 65535 if unsigned else 32767
    if col_t.startswith("mediumint"):
        return 16777215 if unsigned else 8388607
    if col_t.startswith(("int", "integer")):
        return 4294967295 if unsigned else 2147483647
    if col_t.startswith("bigint"):
        return 18446744073709551615 if unsigned else 9223372036854775807
    return None


def _fetch_table_metadata_batches(
    cursor: Any,
    table_pairs: list[tuple[str, str]],
) -> tuple[list[Any], list[Any]]:
    col_rows: list[Any] = []
    idx_rows: list[Any] = []
    for start in range(0, len(table_pairs), _METADATA_BATCH_SIZE):
        batch = table_pairs[start : start + _METADATA_BATCH_SIZE]
        tuple_placeholders = ", ".join("(%s, %s)" for _ in batch)
        flat_params = tuple(item for pair in batch for item in pair)

        col_sql = (
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY, EXTRA "
            f"FROM INFORMATION_SCHEMA.COLUMNS WHERE (TABLE_SCHEMA, TABLE_NAME) IN ({tuple_placeholders});"
        )
        cursor.execute(col_sql, flat_params)
        col_rows.extend(cursor.fetchall() or [])

        idx_sql = (
            "SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, NON_UNIQUE, COLUMN_NAME "
            f"FROM INFORMATION_SCHEMA.STATISTICS WHERE (TABLE_SCHEMA, TABLE_NAME) IN ({tuple_placeholders}) "
            "AND INDEX_TYPE = 'BTREE' "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX;"
        )
        cursor.execute(idx_sql, flat_params)
        idx_rows.extend(cursor.fetchall() or [])
    return col_rows, idx_rows


def mysql_inspect_health(
    database: str = "",
    table_name: str = "",
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """檢視遠端 MySQL/MariaDB 資料庫或指定資料表的結構與空間健康狀態（零鎖表 INFORMATION_SCHEMA 巡檢）"""
    try:
        explicit_schema, target_table = parse_mysql_target(table_name, database)
    except ValueError as exc:
        return format_mysql_error(
            "mysql_inspect_health",
            exc,
            sql=f"database={database!r} table_name={table_name!r}",
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="BLOCKED",
        )

    def _run(conn: Any, target_schema: str, target: str) -> str:
        where_clauses = ["TABLE_TYPE IN ('BASE TABLE', 'VIEW')"]
        params: list[Any] = []
        if target_schema:
            where_clauses.append("TABLE_SCHEMA = %s")
            params.append(target_schema)
        else:
            where_clauses.append(f"TABLE_SCHEMA NOT IN {SYSTEM_SCHEMAS_SQL}")
        if target_table:
            where_clauses.append("TABLE_NAME = %s")
            params.append(target_table)

        where_sql = " AND ".join(where_clauses)
        limit_n = int(DB_HEALTH_MAX_TABLES)
        sql_executed = (
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, ENGINE, ROW_FORMAT, "
            "TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH, DATA_FREE, "
            "AUTO_INCREMENT, TABLE_COLLATION, CHECK_TIME "
            f"FROM INFORMATION_SCHEMA.TABLES WHERE {where_sql} "
            "ORDER BY (COALESCE(DATA_LENGTH, 0) + COALESCE(INDEX_LENGTH, 0)) DESC, "
            f"TABLE_SCHEMA, TABLE_NAME LIMIT {limit_n};"
        )

        cursor = conn.cursor()
        col_rows: list[Any] = []
        idx_rows: list[Any] = []
        try:
            cursor.execute(sql_executed, tuple(params))
            tables = list(cursor.fetchall() or [])
            if tables:
                table_pairs = [(str(tbl[0]), str(tbl[1])) for tbl in tables]
                try:
                    col_rows, idx_rows = _fetch_table_metadata_batches(cursor, table_pairs)
                except Exception:
                    pass
        finally:
            cursor.close()

        if not tables:
            MYSQL_ADAPTER.audit.log(
                "mysql_inspect_health",
                sql_executed,
                target=target,
                status="NOT_FOUND",
                detail="no matching tables",
            )
            return t("mysql_mcp.health_no_tables")

        MYSQL_ADAPTER.audit.log(
            "mysql_inspect_health",
            sql_executed,
            target=target,
            status="OK",
            detail=f"tables={len(tables)}",
        )

        pk_tables: set[tuple[str, str]] = set()
        autoinc_col_types: dict[tuple[str, str], tuple[str, str]] = {}
        for col in col_rows:
            if len(col) >= 6:
                key_pair = (str(col[0]), str(col[1]))
                if str(col[4] or "").upper() == "PRI":
                    pk_tables.add(key_pair)
                if "auto_increment" in str(col[5] or "").lower():
                    autoinc_col_types[key_pair] = (str(col[2]), str(col[3] or ""))

        table_idx_map: dict[tuple[str, str], dict[str, tuple[bool, list[str]]]] = {}
        for row in idx_rows:
            if len(row) >= 5:
                key_pair = (str(row[0]), str(row[1]))
                iname = str(row[2])
                is_uniq = not bool(int(row[3])) if str(row[3]).isdigit() else not bool(row[3])
                t_map = table_idx_map.setdefault(key_pair, {})
                if iname not in t_map:
                    t_map[iname] = (is_uniq, [])
                t_map[iname][1].append(str(row[4]))

        base_count = 0
        view_count = 0
        total_used_bytes = 0
        total_free_bytes = 0
        overview_lines: list[str] = []
        warnings_list: list[str] = []

        for tbl in tables:
            t_schema, t_name = str(tbl[0]), str(tbl[1])
            t_type = str(tbl[2] or "BASE TABLE").upper()
            engine = str(tbl[3] or "-").upper()
            row_fmt = str(tbl[4] or "-")
            est_rows = int(tbl[5] or 0)
            data_len = int(tbl[6] or 0)
            idx_len = int(tbl[7] or 0)
            data_free = int(tbl[8] or 0)
            auto_inc = tbl[9]
            collation = str(tbl[10] or "-")
            key_pair = (t_schema, t_name)
            display_name = t_name if target_schema else f"{t_schema}.{t_name}"

            if t_type == "VIEW":
                view_count += 1
                overview_lines.append(f"- {display_name}: VIEW (Collation={collation})")
                continue

            base_count += 1
            used_b = data_len + idx_len
            total_used_bytes += used_b
            total_free_bytes += data_free
            overview_lines.append(
                f"- {display_name}: Engine={engine}, Format={row_fmt}, "
                f"Rows~{est_rows}, Size={format_bytes(used_b)} "
                f"(Data={format_bytes(data_len)}, Index={format_bytes(idx_len)}), "
                f"Free={format_bytes(data_free)}"
            )

            if engine not in ("INNODB", "-"):
                warnings_list.append(t("mysql_mcp.health_warn_engine", table=display_name, engine=engine))
            if col_rows and key_pair not in pk_tables:
                warnings_list.append(t("mysql_mcp.health_warn_missing_pk", table=display_name))
            if data_free >= DB_FRAGMENTATION_MIN_FREE_BYTES and (used_b + data_free) > 0:
                frag_ratio = data_free / (used_b + data_free)
                if frag_ratio >= DB_FRAGMENTATION_WARN_RATIO:
                    warnings_list.append(
                        t(
                            "mysql_mcp.health_warn_fragmentation",
                            table=display_name,
                            free_size=format_bytes(data_free),
                            ratio=frag_ratio * 100,
                        )
                    )
            if auto_inc is not None and int(auto_inc) > 0:
                col_info = autoinc_col_types.get(key_pair)
                max_val = _mysql_int_type_max(col_info[1]) if col_info else None
                if max_val:
                    ratio = int(auto_inc) / max_val
                    if ratio >= DB_AUTOINC_WARN_RATIO:
                        warnings_list.append(
                            t(
                                "mysql_mcp.health_warn_autoinc",
                                table=display_name,
                                col=col_info[0],
                                col_type=col_info[1],
                                ratio=ratio * 100,
                                current=auto_inc,
                                max_val=max_val,
                            )
                        )
            if key_pair in table_idx_map:
                for red_msg in find_redundant_prefix_indexes(table_idx_map[key_pair]):
                    warnings_list.append(t("mysql_mcp.health_warn_redundant_idx", table=display_name, detail=red_msg))

        scope_label = target_schema or t("mysql_mcp.health_scope_all")
        table_suffix = t("mysql_mcp.health_table_suffix", table=target_table) if target_table else ""
        report: list[str] = [
            "[MySQL Health Summary]",
            t("mysql_mcp.health_summary_scope", scope=scope_label, table_suffix=table_suffix),
            t("mysql_mcp.health_summary_counts", base_count=base_count, view_count=view_count),
            t("mysql_mcp.health_summary_used", size=format_bytes(total_used_bytes)),
            t("mysql_mcp.health_summary_free", size=format_bytes(total_free_bytes)),
            "",
            "[Health Warnings]",
        ]
        if warnings_list:
            report.extend(f"⚠️ {w}" for w in warnings_list)
        else:
            report.append(t("mysql_mcp.health_no_warnings"))

        report.extend(["", "[Table Overview]"])
        report.extend(overview_lines)
        return truncate_health_report(report, max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "mysql_inspect_health",
        _run,
        sql_hint="SELECT ... FROM INFORMATION_SCHEMA.TABLES",
        mysql_host=mysql_host,
        mysql_port=mysql_port,
        mysql_user=mysql_user,
        mysql_password=mysql_password,
        mysql_database=mysql_database,
        explicit_schema=explicit_schema,
        error_key="mysql_mcp.health_failed",
    )


def mysql_check_table(
    table_name: str,
    database: str = "",
    check_mode: str = "QUICK",
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """對指定 MySQL/MariaDB 單一資料表執行輕量級 CHECK TABLE 檢查（僅限 QUICK/FAST，具 2GB 容量保護）"""
    clean_name = (table_name or "").strip().strip("'\"`")
    if not clean_name:
        return format_mysql_error(
            "mysql_check_table",
            ValueError(t("mysql_mcp.missing_table_name")),
            sql="table_name=''",
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="BLOCKED",
        )

    mode_upper = (check_mode or "QUICK").strip().upper()
    if mode_upper not in ("QUICK", "FAST"):
        return format_mysql_error(
            "mysql_check_table",
            ValueError(t("mysql_mcp.check_mode_invalid")),
            sql=f"table_name={table_name!r} check_mode={check_mode!r}",
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="BLOCKED",
        )

    try:
        explicit_schema, target_table = parse_mysql_target(clean_name, database)
    except ValueError as exc:
        return format_mysql_error(
            "mysql_check_table",
            exc,
            sql=f"table_name={table_name!r} database={database!r}",
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="BLOCKED",
        )

    def _run(conn: Any, target_schema: str, target: str) -> str:
        if not target_schema:
            MYSQL_ADAPTER.audit.log(
                "mysql_check_table",
                f"table_name={target_table!r}",
                target=target,
                status="BLOCKED",
                detail="missing database",
            )
            return f"[ERROR] {t('mysql_mcp.check_database_required')}"

        qualified_display = f"{target_schema}.{target_table}"
        qualified_sql = f"`{target_schema}`.`{target_table}`"

        cursor = conn.cursor()
        try:
            meta_sql = (
                "SELECT TABLE_TYPE, COALESCE(DATA_LENGTH, 0) + COALESCE(INDEX_LENGTH, 0) "
                "FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s;"
            )
            cursor.execute(meta_sql, (target_schema, target_table))
            meta_row = cursor.fetchone()
            if not meta_row:
                MYSQL_ADAPTER.audit.log(
                    "mysql_check_table",
                    f"{meta_sql} [table={qualified_display}]",
                    target=target,
                    status="NOT_FOUND",
                    detail=qualified_display,
                )
                return t("mysql_mcp.table_not_found", table=qualified_display)

            tbl_type = str(meta_row[0] or "BASE TABLE").upper()
            total_bytes = int(meta_row[1] or 0)
            if tbl_type == "VIEW":
                MYSQL_ADAPTER.audit.log(
                    "mysql_check_table",
                    f"SKIP VIEW {qualified_display}",
                    target=target,
                    status="SKIPPED",
                    detail="VIEW skipped",
                )
                return t("mysql_mcp.check_view_skipped", table=qualified_display)

            if total_bytes > MYSQL_CHECK_TABLE_MAX_BYTES:
                limit_str = format_bytes(MYSQL_CHECK_TABLE_MAX_BYTES)
                size_str = format_bytes(total_bytes)
                MYSQL_ADAPTER.audit.log(
                    "mysql_check_table",
                    f"SIZE_GUARD {qualified_display} ({size_str} > {limit_str})",
                    target=target,
                    status="BLOCKED",
                    detail=f"size={total_bytes} exceeds limit={MYSQL_CHECK_TABLE_MAX_BYTES}",
                )
                return t("mysql_mcp.check_size_blocked", table=qualified_display, size=size_str, limit=limit_str)

            sql_executed = f"CHECK TABLE {qualified_sql} {mode_upper};"
            cursor.execute(sql_executed)
            check_rows = cursor.fetchall()
        finally:
            cursor.close()

        MYSQL_ADAPTER.audit.log(
            "mysql_check_table",
            sql_executed,
            target=target,
            status="OK",
            detail=f"mode={mode_upper} rows={len(check_rows)}",
        )
        lines = [
            f"[MySQL CHECK TABLE Result ({mode_upper})]",
            t("mysql_mcp.check_result_target", table=qualified_display, size=format_bytes(total_bytes)),
        ]
        for row in check_rows:
            if len(row) >= 4:
                lines.append(f"- [{row[2]}] {row[0]} ({row[1]}): {DEFAULT_REDACTION_POLICY.redact(str(row[3]))}")
            else:
                lines.append("- " + ", ".join(DEFAULT_REDACTION_POLICY.redact(str(c)) for c in row))
        return "\n".join(lines)

    return run_readonly_tool(
        "mysql_check_table",
        _run,
        sql_hint=f"CHECK TABLE {table_name}",
        mysql_host=mysql_host,
        mysql_port=mysql_port,
        mysql_user=mysql_user,
        mysql_password=mysql_password,
        mysql_database=mysql_database,
        explicit_schema=explicit_schema,
        error_key="mysql_mcp.check_failed",
    )


def register(mcp: Any) -> None:
    """註冊 MySQL 健康檢查與 CHECK TABLE 工具至 MCPServer。"""
    for fn in (mysql_inspect_health, mysql_check_table):
        mcp.tool()(fn)

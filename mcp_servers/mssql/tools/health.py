"""Microsoft SQL Server (MSSQL) 零鎖定 DMV 結構健康檢查與缺索引建議工具群組（Command / Registrar Pattern）。"""

from typing import Any

from mcp_servers._shared import (
    CONSTANTS,
    clean_param,
    find_redundant_prefix_indexes,
    format_bytes,
    t,
    truncate_health_report,
    validate_identifier,
    validate_positive_int,
)
from mcp_servers.mssql.adapter import MSSQL_ADAPTER, adapt_placeholders
from mcp_servers.mssql.tools._common import (
    format_mssql_error,
    is_permission_error,
    parse_mssql_target,
    run_readonly_tool,
    truncate_text,
)

DB_HEALTH_MAX_TABLES = CONSTANTS.db_health_max_tables
DB_FRAGMENTATION_WARN_RATIO = CONSTANTS.db_fragmentation_warn_ratio
DB_FRAGMENTATION_MIN_FREE_BYTES = CONSTANTS.db_fragmentation_min_free_bytes
DB_AUTOINC_WARN_RATIO = CONSTANTS.db_autoinc_warn_ratio
_METADATA_BATCH_SIZE = 100


def _mssql_int_type_max(type_name: str) -> int | None:
    col_t = (type_name or "").strip().lower()
    if col_t == "tinyint":
        return 255
    if col_t == "smallint":
        return 32767
    if col_t in ("int", "integer"):
        return 2147483647
    if col_t == "bigint":
        return 9223372036854775807
    return None


def _fetch_health_metadata_batches(
    cursor: Any,
    conn: Any,
    db_prefix: str,
    table_pairs: list[tuple[str, str]],
) -> tuple[list[Any], list[Any], list[Any]]:
    idx_rows: list[Any] = []
    constraint_rows: list[Any] = []
    identity_rows: list[Any] = []

    for start in range(0, len(table_pairs), _METADATA_BATCH_SIZE):
        batch = table_pairs[start : start + _METADATA_BATCH_SIZE]
        pair_clause = " OR ".join("(s.name = %s AND t.name = %s)" for _ in batch)
        flat_params = tuple(item for pair in batch for item in pair)

        try:
            idx_raw = (
                "SELECT s.name, t.name, i.name, i.is_unique, i.is_disabled, col.name, i.type, i.has_filter "
                f"FROM {db_prefix}sys.indexes i "
                f"JOIN {db_prefix}sys.tables t ON i.object_id = t.object_id "
                f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
                f"LEFT JOIN {db_prefix}sys.index_columns ic "
                "  ON i.object_id = ic.object_id AND i.index_id = ic.index_id AND ic.is_included_column = 0 "
                f"LEFT JOIN {db_prefix}sys.columns col "
                "  ON ic.object_id = col.object_id AND ic.column_id = col.column_id "
                f"WHERE ({pair_clause}) AND i.name IS NOT NULL "
                "ORDER BY s.name, t.name, i.name, ic.key_ordinal;"
            )
            cursor.execute(adapt_placeholders(idx_raw, conn), flat_params)
            idx_rows.extend(cursor.fetchall() or [])
        except Exception:
            pass

        try:
            cons_raw = (
                "SELECT s.name, t.name, fk.name, 'FOREIGN KEY', fk.is_disabled, fk.is_not_trusted "
                f"FROM {db_prefix}sys.foreign_keys fk "
                f"JOIN {db_prefix}sys.tables t ON fk.parent_object_id = t.object_id "
                f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
                f"WHERE ({pair_clause}) AND (fk.is_disabled = 1 OR fk.is_not_trusted = 1) "
                "UNION ALL "
                "SELECT s.name, t.name, cc.name, 'CHECK', cc.is_disabled, cc.is_not_trusted "
                f"FROM {db_prefix}sys.check_constraints cc "
                f"JOIN {db_prefix}sys.tables t ON cc.parent_object_id = t.object_id "
                f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
                f"WHERE ({pair_clause}) AND (cc.is_disabled = 1 OR cc.is_not_trusted = 1);"
            )
            cursor.execute(adapt_placeholders(cons_raw, conn), flat_params * 2)
            constraint_rows.extend(cursor.fetchall() or [])
        except Exception:
            pass

        try:
            ident_raw = (
                "SELECT s.name, t.name, ic.name, tp.name, ic.last_value "
                f"FROM {db_prefix}sys.identity_columns ic "
                f"JOIN {db_prefix}sys.tables t ON ic.object_id = t.object_id "
                f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
                f"JOIN {db_prefix}sys.types tp ON ic.system_type_id = tp.user_type_id "
                f"WHERE ({pair_clause}) AND ic.last_value IS NOT NULL;"
            )
            cursor.execute(adapt_placeholders(ident_raw, conn), flat_params)
            identity_rows.extend(cursor.fetchall() or [])
        except Exception:
            pass

    return idx_rows, constraint_rows, identity_rows


def mssql_inspect_health(
    database: str = "",
    table_name: str = "",
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """檢視遠端 SQL Server 資料庫或資料表的結構、空間、索引與約束健康狀態（零鎖定 DMV 巡檢）"""
    try:
        explicit_db, target_schema, target_table = parse_mssql_target(table_name, database)
    except ValueError as exc:
        return format_mssql_error(
            "mssql_inspect_health",
            exc,
            sql=f"database={database!r} table_name={table_name!r}",
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="BLOCKED",
        )

    def _run(conn: Any, target_db: str, target: str) -> str:
        db_prefix = f"[{explicit_db}]." if explicit_db else ""
        where_parts = ["t.is_ms_shipped = 0"]
        params: list[Any] = []
        if target_schema:
            where_parts.append("s.name = %s")
            params.append(target_schema)
        if target_table:
            where_parts.append("t.name = %s")
            params.append(target_table)
        where_sql = " AND ".join(where_parts)
        limit_n = int(DB_HEALTH_MAX_TABLES)

        dmv_sql_raw = (
            f"SELECT TOP {limit_n} "
            "s.name AS schema_name, t.name AS table_name, "
            "CASE WHEN EXISTS ("
            f"  SELECT 1 FROM {db_prefix}sys.indexes i "
            "  WHERE i.object_id = t.object_id AND i.is_primary_key = 1"
            ") THEN 1 ELSE 0 END AS has_pk, "
            "CASE WHEN EXISTS ("
            f"  SELECT 1 FROM {db_prefix}sys.indexes i "
            "  WHERE i.object_id = t.object_id AND i.index_id = 0"
            ") THEN 1 ELSE 0 END AS is_heap, "
            "COALESCE(ps.row_count, 0) AS row_count, "
            "COALESCE(ps.reserved_pages, 0) * 8192 AS reserved_bytes, "
            "COALESCE(ps.used_pages, 0) * 8192 AS used_bytes "
            f"FROM {db_prefix}sys.tables t "
            f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
            "LEFT JOIN ("
            "  SELECT object_id, "
            "    SUM(CASE WHEN index_id IN (0, 1) THEN row_count ELSE 0 END) AS row_count, "
            "    SUM(reserved_page_count) AS reserved_pages, "
            "    SUM(used_page_count) AS used_pages "
            f"  FROM {db_prefix}sys.dm_db_partition_stats GROUP BY object_id"
            ") ps ON t.object_id = ps.object_id "
            f"WHERE {where_sql} "
            "ORDER BY COALESCE(ps.reserved_pages, 0) DESC, s.name, t.name;"
        )
        fallback_sql_raw = (
            f"SELECT TOP {limit_n} "
            "s.name AS schema_name, t.name AS table_name, "
            "CASE WHEN EXISTS ("
            f"  SELECT 1 FROM {db_prefix}sys.indexes i "
            "  WHERE i.object_id = t.object_id AND i.is_primary_key = 1"
            ") THEN 1 ELSE 0 END AS has_pk, "
            "CASE WHEN EXISTS ("
            f"  SELECT 1 FROM {db_prefix}sys.indexes i "
            "  WHERE i.object_id = t.object_id AND i.index_id = 0"
            ") THEN 1 ELSE 0 END AS is_heap, "
            "NULL AS row_count, 0 AS reserved_bytes, 0 AS used_bytes "
            f"FROM {db_prefix}sys.tables t "
            f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
            f"WHERE {where_sql} "
            "ORDER BY s.name, t.name;"
        )

        cursor = conn.cursor()
        dmv_degraded = False
        idx_rows: list[Any] = []
        constraint_rows: list[Any] = []
        identity_rows: list[Any] = []
        try:
            try:
                sql_executed = adapt_placeholders(dmv_sql_raw, conn)
                cursor.execute(sql_executed, tuple(params))
                tables = list(cursor.fetchall() or [])
            except Exception as dmv_exc:
                if not is_permission_error(dmv_exc):
                    raise
                dmv_degraded = True
                sql_executed = adapt_placeholders(fallback_sql_raw, conn)
                cursor.execute(sql_executed, tuple(params))
                tables = list(cursor.fetchall() or [])

            if tables:
                table_pairs = [(str(tbl[0]), str(tbl[1])) for tbl in tables]
                idx_rows, constraint_rows, identity_rows = _fetch_health_metadata_batches(
                    cursor,
                    conn,
                    db_prefix,
                    table_pairs,
                )
        finally:
            cursor.close()

        if not tables:
            MSSQL_ADAPTER.audit.log(
                "mssql_inspect_health",
                sql_executed,
                target=target,
                status="NOT_FOUND",
                detail="no matching tables",
            )
            return t("mssql_mcp.health_no_tables")

        MSSQL_ADAPTER.audit.log(
            "mssql_inspect_health",
            sql_executed,
            target=target,
            status="OK",
            detail=f"tables={len(tables)} degraded={dmv_degraded}",
        )

        table_idx_map: dict[tuple[str, str], dict[str, tuple[bool, list[str]]]] = {}
        disabled_indexes: list[tuple[str, str, str]] = []
        seen_disabled: set[tuple[str, str, str]] = set()
        for row in idx_rows:
            if len(row) < 6:
                continue
            key_pair = (str(row[0]), str(row[1]))
            iname = str(row[2])
            is_uniq, is_dis, cname = bool(row[3]), bool(row[4]), row[5]
            itype = int(row[6]) if len(row) > 6 and row[6] is not None else 2
            has_filter = bool(row[7]) if len(row) > 7 else False

            if is_dis and (key_pair[0], key_pair[1], iname) not in seen_disabled:
                seen_disabled.add((key_pair[0], key_pair[1], iname))
                disabled_indexes.append((key_pair[0], key_pair[1], iname))

            # 僅針對非篩選的 Clustered / Nonclustered B-Tree 索引檢查冗餘前導欄位
            if cname is not None and itype in (1, 2) and not has_filter:
                t_map = table_idx_map.setdefault(key_pair, {})
                if iname not in t_map:
                    t_map[iname] = (is_uniq, [])
                t_map[iname][1].append(str(cname))

        total_reserved_bytes = 0
        total_used_bytes = 0
        overview_lines: list[str] = []
        warnings_list: list[str] = []

        for tbl in tables:
            s_name, t_name = str(tbl[0]), str(tbl[1])
            has_pk = bool(tbl[2]) if len(tbl) > 2 else True
            is_heap = bool(tbl[3]) if len(tbl) > 3 else False
            row_cnt = tbl[4] if len(tbl) > 4 else None
            res_bytes = int(tbl[5] or 0) if len(tbl) > 5 else 0
            used_bytes = int(tbl[6] or 0) if len(tbl) > 6 else 0
            unused_bytes = max(0, res_bytes - used_bytes)

            key_pair = (s_name, t_name)
            display_name = t_name if s_name.lower() == "dbo" else f"{s_name}.{t_name}"
            total_reserved_bytes += res_bytes
            total_used_bytes += used_bytes

            structure_tag = "Heap (無叢集索引)" if is_heap else "Clustered"
            pk_tag = "PK=Yes" if has_pk else "PK=No"
            if dmv_degraded or row_cnt is None:
                overview_lines.append(f"- {display_name}: Structure={structure_tag}, {pk_tag}")
            else:
                overview_lines.append(
                    f"- {display_name}: Structure={structure_tag}, {pk_tag}, "
                    f"Rows={int(row_cnt)}, Reserved={format_bytes(res_bytes)}, "
                    f"Used={format_bytes(used_bytes)}, Unused={format_bytes(unused_bytes)}"
                )

            if not has_pk:
                warnings_list.append(t("mssql_mcp.health_warn_missing_pk", table=display_name))
            if is_heap:
                warnings_list.append(t("mssql_mcp.health_warn_heap", table=display_name))
            if not dmv_degraded and res_bytes > 0 and unused_bytes >= DB_FRAGMENTATION_MIN_FREE_BYTES:
                unused_ratio = unused_bytes / res_bytes
                if unused_ratio >= DB_FRAGMENTATION_WARN_RATIO:
                    warnings_list.append(
                        t(
                            "mssql_mcp.health_warn_unused_space",
                            table=display_name,
                            unused=format_bytes(unused_bytes),
                            ratio=unused_ratio * 100,
                        )
                    )
            if key_pair in table_idx_map:
                for red_msg in find_redundant_prefix_indexes(table_idx_map[key_pair]):
                    warnings_list.append(t("mssql_mcp.health_warn_redundant_idx", table=display_name, detail=red_msg))

        for s_name, t_name, iname in disabled_indexes:
            disp = t_name if s_name.lower() == "dbo" else f"{s_name}.{t_name}"
            warnings_list.append(t("mssql_mcp.health_warn_disabled_idx", table=disp, index=iname))

        for c_row in constraint_rows:
            if len(c_row) >= 6:
                s_name, t_name, cname, ctype = str(c_row[0]), str(c_row[1]), str(c_row[2]), str(c_row[3])
                disp = t_name if s_name.lower() == "dbo" else f"{s_name}.{t_name}"
                state_desc = (
                    t("mssql_mcp.health_constraint_disabled")
                    if bool(c_row[4])
                    else t("mssql_mcp.health_constraint_untrusted")
                )
                warnings_list.append(
                    t("mssql_mcp.health_warn_constraint", table=disp, ctype=ctype, cname=cname, state=state_desc)
                )

        for id_row in identity_rows:
            if len(id_row) >= 5:
                s_name, t_name, col_name, tp_name, last_val = (
                    str(id_row[0]),
                    str(id_row[1]),
                    str(id_row[2]),
                    str(id_row[3]),
                    id_row[4],
                )
                max_val = _mssql_int_type_max(tp_name)
                try:
                    cur_val = int(last_val)
                except TypeError, ValueError:
                    cur_val = 0
                if max_val and cur_val > 0:
                    ratio = cur_val / max_val
                    if ratio >= DB_AUTOINC_WARN_RATIO:
                        disp = t_name if s_name.lower() == "dbo" else f"{s_name}.{t_name}"
                        warnings_list.append(
                            t(
                                "mssql_mcp.health_warn_identity",
                                table=disp,
                                col=col_name,
                                col_type=tp_name.upper(),
                                ratio=ratio * 100,
                                current=cur_val,
                                max_val=max_val,
                            )
                        )

        scope_label = target_db or t("mssql_mcp.health_default_db")
        if target_schema or target_table:
            tbl_filter = f"{target_schema}.{target_table}" if target_schema else target_table
            scope_label += t("mssql_mcp.health_table_suffix", table=tbl_filter)

        report: list[str] = [
            "[MSSQL Health Summary]",
            t("mssql_mcp.health_summary_scope", scope=scope_label),
            t("mssql_mcp.health_summary_counts", count=len(tables)),
        ]
        if dmv_degraded:
            report.append(t("mssql_mcp.health_dmv_degraded"))
        else:
            total_unused = max(0, total_reserved_bytes - total_used_bytes)
            report.append(t("mssql_mcp.health_summary_reserved", size=format_bytes(total_reserved_bytes)))
            report.append(t("mssql_mcp.health_summary_used", size=format_bytes(total_used_bytes)))
            report.append(t("mssql_mcp.health_summary_unused", size=format_bytes(total_unused)))

        report.extend(["", "[Health Warnings]"])
        if warnings_list:
            report.extend(f"⚠️ {w}" for w in warnings_list)
        else:
            report.append(t("mssql_mcp.health_no_warnings"))

        report.extend(["", "[Table Overview]"])
        report.extend(overview_lines)
        return truncate_health_report(report, max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "mssql_inspect_health",
        _run,
        sql_hint="SELECT ... FROM sys.tables",
        mssql_host=mssql_host,
        mssql_port=mssql_port,
        mssql_user=mssql_user,
        mssql_password=mssql_password,
        mssql_database=mssql_database,
        explicit_db=explicit_db,
        error_key="mssql_mcp.health_failed",
    )


def mssql_inspect_missing_indexes(
    database: str = "",
    top_n: int = CONSTANTS.mssql_missing_index_default_top,
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """查詢 SQL Server 缺索引 DMV 建議清單（依預估影響分數排序，零鎖定唯讀）"""
    try:
        valid_top = validate_positive_int(top_n, "top_n", max_val=CONSTANTS.mssql_missing_index_max_top)
    except ValueError:
        return format_mssql_error(
            "mssql_inspect_missing_indexes",
            ValueError(
                t(
                    "mssql_mcp.missing_idx_invalid_top",
                    value=repr(top_n),
                    max_val=CONSTANTS.mssql_missing_index_max_top,
                )
            ),
            sql=f"top_n={top_n!r}",
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="BLOCKED",
        )

    explicit_db = clean_param(database)
    if explicit_db:
        try:
            explicit_db = validate_identifier(explicit_db, "database")
        except ValueError as exc:
            return format_mssql_error(
                "mssql_inspect_missing_indexes",
                exc,
                sql=f"database={database!r}",
                target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, database),
                status="BLOCKED",
            )

    def _run(conn: Any, target_db: str, target: str) -> str:
        db_filter = explicit_db or target_db
        where_sql = "WHERE d.database_id = DB_ID(%s) " if db_filter else ""
        sql = adapt_placeholders(
            f"SELECT TOP {valid_top} "
            "DB_NAME(d.database_id) AS db_name, "
            "d.statement, "
            "d.equality_columns, d.inequality_columns, d.included_columns, "
            "gs.user_seeks, gs.user_scans, gs.avg_total_user_cost, gs.avg_user_impact, "
            "ROUND(gs.avg_total_user_cost * gs.avg_user_impact * (gs.user_seeks + gs.user_scans), 2) AS score "
            "FROM sys.dm_db_missing_index_details d "
            "JOIN sys.dm_db_missing_index_groups g ON d.index_handle = g.index_handle "
            "JOIN sys.dm_db_missing_index_group_stats gs ON g.index_group_handle = gs.group_handle "
            f"{where_sql}"
            "ORDER BY score DESC;",
            conn,
        )
        cursor = conn.cursor()
        try:
            try:
                if db_filter:
                    cursor.execute(sql, (db_filter,))
                else:
                    cursor.execute(sql)
                rows = list(cursor.fetchall() or [])
            except Exception as exc:
                if is_permission_error(exc):
                    MSSQL_ADAPTER.audit.log(
                        "mssql_inspect_missing_indexes",
                        sql,
                        target=target,
                        status="BLOCKED",
                        detail="permission denied",
                    )
                    return f"[ERROR] {t('mssql_mcp.missing_idx_permission_denied')}"
                raise
        finally:
            cursor.close()

        MSSQL_ADAPTER.audit.log(
            "mssql_inspect_missing_indexes",
            sql,
            target=target,
            status="OK",
            detail=f"suggestions={len(rows)}",
        )
        if not rows:
            return t("mssql_mcp.missing_idx_empty")

        lines = [t("mssql_mcp.missing_idx_header", count=len(rows))]
        for idx, row in enumerate(rows, start=1):
            db_n, stmt, eq_c, ineq_c, inc_c, seeks, scans, _cost, impact, score = (list(row) + [None] * 10)[:10]
            lines.append(f"{idx}. [{db_n}] {stmt} | score={score} impact={impact}% seeks={seeks} scans={scans}")
            if eq_c:
                lines.append(f"   equality: {eq_c}")
            if ineq_c:
                lines.append(f"   inequality: {ineq_c}")
            if inc_c:
                lines.append(f"   include: {inc_c}")
        return truncate_text("\n".join(lines), max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "mssql_inspect_missing_indexes",
        _run,
        sql_hint="SELECT ... FROM sys.dm_db_missing_index_details",
        mssql_host=mssql_host,
        mssql_port=mssql_port,
        mssql_user=mssql_user,
        mssql_password=mssql_password,
        mssql_database=mssql_database,
        explicit_db=explicit_db,
        error_key="mssql_mcp.missing_idx_failed",
    )


def register(mcp: Any) -> None:
    """註冊 MSSQL 健康檢查與缺索引建議工具至 MCPServer。"""
    for fn in (mssql_inspect_health, mssql_inspect_missing_indexes):
        mcp.tool()(fn)

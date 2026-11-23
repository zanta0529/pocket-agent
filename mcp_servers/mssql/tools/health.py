"""Microsoft SQL Server (MSSQL) 零鎖定 DMV 結構與索引健康檢查工具群組（Command / Registrar Pattern）。"""

import os
from typing import Any

from mcp_servers._shared import (
    CONSTANTS,
    ConnAuthError,
    clean_param,
    find_redundant_prefix_indexes,
    format_bytes,
    truncate_health_report,
    validate_identifier,
)
from mcp_servers.mssql.adapter import MSSQL_ADAPTER, adapt_placeholders

DB_HEALTH_MAX_TABLES = CONSTANTS.db_health_max_tables
DB_FRAGMENTATION_WARN_RATIO = CONSTANTS.db_fragmentation_warn_ratio
DB_FRAGMENTATION_MIN_FREE_BYTES = CONSTANTS.db_fragmentation_min_free_bytes
DB_AUTOINC_WARN_RATIO = CONSTANTS.db_autoinc_warn_ratio


def _mssql_int_type_max(type_name: str) -> int | None:
    t = (type_name or "").strip().lower()
    if t == "tinyint":
        return 255
    if t == "smallint":
        return 32767
    if t in ("int", "integer"):
        return 2147483647
    if t == "bigint":
        return 9223372036854775807
    return None


def _parse_mssql_target(table_name: str, database: str) -> tuple[str, str, str]:
    explicit_db = clean_param(database)
    if explicit_db:
        explicit_db = validate_identifier(explicit_db, "database")

    clean_tbl = (table_name or "").strip().strip("'\"`[]")
    if not clean_tbl:
        return explicit_db, "", ""

    parts = [p.strip("[]'\"") for p in clean_tbl.split(".")]
    if len(parts) == 3:
        return (
            validate_identifier(parts[0], "database"),
            validate_identifier(parts[1], "schema_name"),
            validate_identifier(parts[2], "table_name"),
        )
    if len(parts) == 2:
        return (
            explicit_db,
            validate_identifier(parts[0], "schema_name"),
            validate_identifier(parts[1], "table_name"),
        )
    if len(parts) == 1:
        return explicit_db, "", validate_identifier(parts[0], "table_name")
    raise ValueError(f"非法的 table_name 格式：{table_name!r}")


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
    conn = None
    sql_executed = "SELECT ... FROM sys.tables"
    try:
        try:
            explicit_db, target_schema, target_table = _parse_mssql_target(table_name, database)
        except ValueError as exc:
            MSSQL_ADAPTER.audit.log(
                "mssql_inspect_health",
                f"database={database!r} table_name={table_name!r}",
                target=MSSQL_ADAPTER.format_audit_target(
                    mssql_host, mssql_port, mssql_user, database or mssql_database
                ),
                status="BLOCKED",
                detail=str(exc),
            )
            return f"[ERROR] {exc}"

        conn = MSSQL_ADAPTER.connect(mssql_host, mssql_port, mssql_user, mssql_password, mssql_database)
        target_db = (
            explicit_db
            or clean_param(mssql_database)
            or MSSQL_ADAPTER.cache.get("MSSQL_DATABASE")
            or clean_param(os.getenv("MSSQL_DATABASE"))
        )
        target = MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, target_db)
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
                tables = cursor.fetchall()
            except Exception:
                dmv_degraded = True
                sql_executed = adapt_placeholders(fallback_sql_raw, conn)
                cursor.execute(sql_executed, tuple(params))
                tables = cursor.fetchall()

            if tables:
                try:
                    idx_raw = (
                        "SELECT s.name, t.name, i.name, i.is_unique, i.is_disabled, col.name "
                        f"FROM {db_prefix}sys.indexes i "
                        f"JOIN {db_prefix}sys.tables t ON i.object_id = t.object_id "
                        f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
                        f"LEFT JOIN {db_prefix}sys.index_columns ic "
                        "  ON i.object_id = ic.object_id AND i.index_id = ic.index_id AND ic.is_included_column = 0 "
                        f"LEFT JOIN {db_prefix}sys.columns col "
                        "  ON ic.object_id = col.object_id AND ic.column_id = col.column_id "
                        f"WHERE {where_sql} AND i.name IS NOT NULL "
                        "ORDER BY s.name, t.name, i.name, ic.key_ordinal;"
                    )
                    cursor.execute(adapt_placeholders(idx_raw, conn), tuple(params))
                    fetched_idx = cursor.fetchall()
                    if fetched_idx and fetched_idx != tables:
                        idx_rows = list(fetched_idx)
                except Exception:
                    pass

                try:
                    cons_raw = (
                        "SELECT s.name, t.name, fk.name, 'FOREIGN KEY', fk.is_disabled, fk.is_not_trusted "
                        f"FROM {db_prefix}sys.foreign_keys fk "
                        f"JOIN {db_prefix}sys.tables t ON fk.parent_object_id = t.object_id "
                        f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
                        f"WHERE {where_sql} AND (fk.is_disabled = 1 OR fk.is_not_trusted = 1) "
                        "UNION ALL "
                        "SELECT s.name, t.name, cc.name, 'CHECK', cc.is_disabled, cc.is_not_trusted "
                        f"FROM {db_prefix}sys.check_constraints cc "
                        f"JOIN {db_prefix}sys.tables t ON cc.parent_object_id = t.object_id "
                        f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
                        f"WHERE {where_sql} AND (cc.is_disabled = 1 OR cc.is_not_trusted = 1);"
                    )
                    cursor.execute(adapt_placeholders(cons_raw, conn), tuple(params) * 2)
                    fetched_cons = cursor.fetchall()
                    if fetched_cons and fetched_cons != tables:
                        constraint_rows = list(fetched_cons)
                except Exception:
                    pass

                try:
                    ident_raw = (
                        "SELECT s.name, t.name, ic.name, tp.name, ic.last_value "
                        f"FROM {db_prefix}sys.identity_columns ic "
                        f"JOIN {db_prefix}sys.tables t ON ic.object_id = t.object_id "
                        f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
                        f"JOIN {db_prefix}sys.types tp ON ic.system_type_id = tp.user_type_id "
                        f"WHERE {where_sql} AND ic.last_value IS NOT NULL;"
                    )
                    cursor.execute(adapt_placeholders(ident_raw, conn), tuple(params))
                    fetched_ident = cursor.fetchall()
                    if fetched_ident and fetched_ident != tables:
                        identity_rows = list(fetched_ident)
                except Exception:
                    pass
        finally:
            cursor.close()

        if not tables:
            MSSQL_ADAPTER.audit.log(
                "mssql_inspect_health",
                sql_executed,
                target=target,
                status="NOT_FOUND",
                detail="查無符合條件的資料表",
            )
            return "查無符合條件的資料表。"

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
            if len(row) >= 6:
                key_pair = (str(row[0]), str(row[1]))
                iname = str(row[2])
                is_uniq, is_dis, cname = bool(row[3]), bool(row[4]), row[5]
                if is_dis and (key_pair[0], key_pair[1], iname) not in seen_disabled:
                    seen_disabled.add((key_pair[0], key_pair[1], iname))
                    disabled_indexes.append((key_pair[0], key_pair[1], iname))
                if cname is not None:
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
                warnings_list.append(f"[Missing PK] {display_name} 缺少主鍵 (Primary Key)")
            if is_heap:
                warnings_list.append(
                    f"[Heap Table] {display_name} 為堆積表 (Heap，無叢集索引)，"
                    "頻繁更新易產生轉發紀錄 (Forwarded Records) 與空間碎片"
                )
            if not dmv_degraded and res_bytes > 0 and unused_bytes >= DB_FRAGMENTATION_MIN_FREE_BYTES:
                unused_ratio = unused_bytes / res_bytes
                if unused_ratio >= DB_FRAGMENTATION_WARN_RATIO:
                    warnings_list.append(
                        f"[Unused Space] {display_name} 未用保留空間達 {format_bytes(unused_bytes)} "
                        f"({unused_ratio * 100:.1f}%)，建議評估索引重組或空間回收"
                    )
            if key_pair in table_idx_map:
                for red_msg in find_redundant_prefix_indexes(table_idx_map[key_pair]):
                    warnings_list.append(f"[Redundant Index] {display_name}: {red_msg}")

        for s_name, t_name, iname in disabled_indexes:
            disp = t_name if s_name.lower() == "dbo" else f"{s_name}.{t_name}"
            warnings_list.append(f"[Disabled Index] {disp}.{iname} 處於停用狀態 (is_disabled=1)")

        for c_row in constraint_rows:
            if len(c_row) >= 6:
                s_name, t_name, cname, ctype = str(c_row[0]), str(c_row[1]), str(c_row[2]), str(c_row[3])
                disp = t_name if s_name.lower() == "dbo" else f"{s_name}.{t_name}"
                state_desc = "已停用 (Disabled)" if bool(c_row[4]) else "未受信任 (Untrusted / WITH NOCHECK)"
                warnings_list.append(f"[Constraint] {disp} 的 {ctype} 約束 {cname} 處於 {state_desc} 狀態")

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
                            f"[Identity] {disp}.{col_name} ({tp_name.upper()}) "
                            f"水位已達 {ratio * 100:.1f}% ({cur_val}/{max_val})，有溢位風險"
                        )

        scope_label = target_db or "預設連線資料庫"
        if target_schema or target_table:
            tbl_filter = f"{target_schema}.{target_table}" if target_schema else target_table
            scope_label += f" / 資料表: {tbl_filter}"

        report: list[str] = [
            "[MSSQL Health Summary]",
            f"巡檢範圍: {scope_label}",
            f"資料表數: {len(tables)} 張使用者資料表",
        ]
        if dmv_degraded:
            report.append("空間統計: ℹ️ 目前帳號無 VIEW DATABASE STATE 權限，已自動降級略過 DMV 分區空間與列數統計")
        else:
            total_unused = max(0, total_reserved_bytes - total_used_bytes)
            report.append(f"總保留空間 (Reserved): {format_bytes(total_reserved_bytes)}")
            report.append(f"總已用空間 (Used): {format_bytes(total_used_bytes)}")
            report.append(f"總未用保留空間 (Unused): {format_bytes(total_unused)}")

        report.extend(["", "[Health Warnings]"])
        if warnings_list:
            report.extend(f"⚠️ {w}" for w in warnings_list)
        else:
            report.append("✓ 未發現明顯結構、主鍵、Heap、停用索引或未受信任約束風險。")

        report.extend(["", "[Table Overview]"])
        report.extend(overview_lines)
        return truncate_health_report(report, max_chars=CONSTANTS.max_output_chars)
    except ConnAuthError as exc:
        MSSQL_ADAPTER.audit.log(
            "mssql_inspect_health",
            sql_executed,
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="AUTH_FAIL",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    except Exception as exc:
        MSSQL_ADAPTER.audit.log(
            "mssql_inspect_health",
            sql_executed,
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="ERROR",
            detail=str(exc),
        )
        return f"[ERROR] 健康檢查失敗: {exc}"
    finally:
        MSSQL_ADAPTER.close_safely(conn)


def register(mcp: Any) -> None:
    """註冊 MSSQL 健康檢查工具至 MCPServer。"""
    mcp.tool()(mssql_inspect_health)

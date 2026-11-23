"""MySQL / MariaDB 結構健康檢查與輕量 CHECK TABLE 工具群組（Command / Registrar Pattern）。"""

import os
from typing import Any

from mcp_servers._shared import (
    CONSTANTS,
    DEFAULT_REDACTION_POLICY,
    ConnAuthError,
    clean_param,
    find_redundant_prefix_indexes,
    format_bytes,
    truncate_health_report,
    validate_identifier,
)
from mcp_servers.mysql.adapter import MYSQL_ADAPTER

MYSQL_CHECK_TABLE_MAX_BYTES = CONSTANTS.mysql_check_table_max_bytes
DB_HEALTH_MAX_TABLES = CONSTANTS.db_health_max_tables
DB_FRAGMENTATION_WARN_RATIO = CONSTANTS.db_fragmentation_warn_ratio
DB_FRAGMENTATION_MIN_FREE_BYTES = CONSTANTS.db_fragmentation_min_free_bytes
DB_AUTOINC_WARN_RATIO = CONSTANTS.db_autoinc_warn_ratio


def _mysql_int_type_max(column_type: str) -> int | None:
    t = (column_type or "").strip().lower()
    unsigned = "unsigned" in t
    if t.startswith("tinyint"):
        return 255 if unsigned else 127
    if t.startswith("smallint"):
        return 65535 if unsigned else 32767
    if t.startswith("mediumint"):
        return 16777215 if unsigned else 8388607
    if t.startswith(("int", "integer")):
        return 4294967295 if unsigned else 2147483647
    if t.startswith("bigint"):
        return 18446744073709551615 if unsigned else 9223372036854775807
    return None


def _parse_mysql_target(table_name: str, database: str) -> tuple[str, str]:
    clean_tbl = (table_name or "").strip().strip("'\"`")
    clean_db = clean_param(database)
    if clean_tbl and "." in clean_tbl:
        schema_part, tbl_part = clean_tbl.split(".", 1)
        return (
            validate_identifier(schema_part.strip("`'\""), "schema_name"),
            validate_identifier(tbl_part.strip("`'\""), "table_name"),
        )
    validated_schema = validate_identifier(clean_db, "database") if clean_db else ""
    validated_tbl = validate_identifier(clean_tbl, "table_name") if clean_tbl else ""
    return validated_schema, validated_tbl


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
    conn = None
    sql_executed = "SELECT ... FROM INFORMATION_SCHEMA.TABLES"
    try:
        try:
            explicit_schema, target_table = _parse_mysql_target(table_name, database)
        except ValueError as exc:
            MYSQL_ADAPTER.audit.log(
                "mysql_inspect_health",
                f"database={database!r} table_name={table_name!r}",
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

        where_clauses = ["TABLE_TYPE IN ('BASE TABLE', 'VIEW')"]
        params: list[Any] = []
        if target_schema:
            where_clauses.append("TABLE_SCHEMA = %s")
            params.append(target_schema)
        else:
            where_clauses.append("TABLE_SCHEMA NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys')")
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
            tables = cursor.fetchall()
            if tables:
                try:
                    col_where = [c for c in where_clauses if not c.startswith("TABLE_TYPE")]
                    col_where_sql = " AND ".join(col_where) if col_where else "1=1"
                    col_sql = (
                        "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY, EXTRA "
                        f"FROM INFORMATION_SCHEMA.COLUMNS WHERE {col_where_sql};"
                    )
                    cursor.execute(col_sql, tuple(params))
                    fetched_cols = cursor.fetchall()
                    if fetched_cols and fetched_cols != tables:
                        col_rows = list(fetched_cols)

                    idx_sql = (
                        "SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, NON_UNIQUE, COLUMN_NAME "
                        f"FROM INFORMATION_SCHEMA.STATISTICS WHERE {col_where_sql} "
                        "ORDER BY TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX;"
                    )
                    cursor.execute(idx_sql, tuple(params))
                    fetched_idx = cursor.fetchall()
                    if fetched_idx and fetched_idx != tables:
                        idx_rows = list(fetched_idx)
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
                detail="查無符合條件的資料表",
            )
            return "查無符合條件的資料表。"

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
                warnings_list.append(f"[Engine] {display_name} 使用 {engine} 引擎（非 InnoDB，留意交易與表級鎖限制）")
            if col_rows and key_pair not in pk_tables:
                warnings_list.append(
                    f"[Missing PK] {display_name} 缺少主鍵 (Primary Key)，可能影響 InnoDB 叢集索引效能與複製穩定度"
                )
            if data_free >= DB_FRAGMENTATION_MIN_FREE_BYTES and (used_b + data_free) > 0:
                frag_ratio = data_free / (used_b + data_free)
                if frag_ratio >= DB_FRAGMENTATION_WARN_RATIO:
                    warnings_list.append(
                        f"[Fragmentation] {display_name} 碎片空間達 {format_bytes(data_free)} "
                        f"({frag_ratio * 100:.1f}%)，建議於離峰評估 OPTIMIZE TABLE"
                    )
            if auto_inc is not None and int(auto_inc) > 0:
                col_info = autoinc_col_types.get(key_pair)
                max_val = _mysql_int_type_max(col_info[1]) if col_info else None
                if max_val:
                    ratio = int(auto_inc) / max_val
                    if ratio >= DB_AUTOINC_WARN_RATIO:
                        warnings_list.append(
                            f"[AutoIncrement] {display_name}.{col_info[0]} ({col_info[1]}) "
                            f"水位已達 {ratio * 100:.1f}% ({auto_inc}/{max_val})，有溢位風險"
                        )
            if key_pair in table_idx_map:
                for red_msg in find_redundant_prefix_indexes(table_idx_map[key_pair]):
                    warnings_list.append(f"[Redundant Index] {display_name}: {red_msg}")

        report: list[str] = [
            "[MySQL Health Summary]",
            f"巡檢範圍: {target_schema or '所有使用者資料庫'}" + (f" / 資料表: {target_table}" if target_table else ""),
            f"資料表數: {base_count} 張實體表, {view_count} 個檢視表",
            f"總占用空間 (Data + Index): {format_bytes(total_used_bytes)}",
            f"總未釋放碎片空間 (Data Free): {format_bytes(total_free_bytes)}",
            "",
            "[Health Warnings]",
        ]
        if warnings_list:
            report.extend(f"⚠️ {w}" for w in warnings_list)
        else:
            report.append("✓ 未發現明顯結構、主鍵、碎片或索引健康風險。")

        report.extend(["", "[Table Overview]"])
        report.extend(overview_lines)
        return truncate_health_report(report, max_chars=CONSTANTS.max_output_chars)
    except ConnAuthError as exc:
        MYSQL_ADAPTER.audit.log(
            "mysql_inspect_health",
            sql_executed,
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="AUTH_FAIL",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    except Exception as exc:
        MYSQL_ADAPTER.audit.log(
            "mysql_inspect_health",
            sql_executed,
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="ERROR",
            detail=str(exc),
        )
        return f"[ERROR] 健康檢查失敗: {exc}"
    finally:
        MYSQL_ADAPTER.close_safely(conn)


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
    conn = None
    sql_executed = f"CHECK TABLE {table_name}"
    try:
        clean_name = (table_name or "").strip().strip("'\"`")
        if not clean_name:
            MYSQL_ADAPTER.audit.log(
                "mysql_check_table",
                "table_name=''",
                target=MYSQL_ADAPTER.format_audit_target(
                    mysql_host, mysql_port, mysql_user, database or mysql_database
                ),
                status="BLOCKED",
                detail="缺少 table_name 參數",
            )
            return "[ERROR] 缺少 table_name 參數。"

        mode_upper = (check_mode or "QUICK").strip().upper()
        if mode_upper not in ("QUICK", "FAST"):
            msg = "基於正式環境穩定度考量，check_mode 僅允許 'QUICK' 或 'FAST'（禁止 EXTENDED、MEDIUM、FOR UPGRADE）。"
            MYSQL_ADAPTER.audit.log(
                "mysql_check_table",
                f"table_name={table_name!r} check_mode={check_mode!r}",
                target=MYSQL_ADAPTER.format_audit_target(
                    mysql_host, mysql_port, mysql_user, database or mysql_database
                ),
                status="BLOCKED",
                detail=msg,
            )
            return f"[ERROR] {msg}"

        try:
            explicit_schema, target_table = _parse_mysql_target(clean_name, database)
        except ValueError as exc:
            MYSQL_ADAPTER.audit.log(
                "mysql_check_table",
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
        if not target_schema:
            MYSQL_ADAPTER.audit.log(
                "mysql_check_table",
                f"table_name={target_table!r}",
                target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, ""),
                status="BLOCKED",
                detail="未指定 database 或 MYSQL_DATABASE",
            )
            return "[ERROR] 執行 CHECK TABLE 必須指定目標資料庫（請傳入 database 參數或設定 schema.table）。"

        target = MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, target_schema)
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
                    detail=f"找不到資料表 {qualified_display}",
                )
                return f"找不到資料表 {qualified_display}。"

            tbl_type = str(meta_row[0] or "BASE TABLE").upper()
            total_bytes = int(meta_row[1] or 0)
            if tbl_type == "VIEW":
                MYSQL_ADAPTER.audit.log(
                    "mysql_check_table",
                    f"SKIP VIEW {qualified_display}",
                    target=target,
                    status="SKIPPED",
                    detail="VIEW 不適用實體頁檢查",
                )
                return f"[SKIPPED] {qualified_display} 為 VIEW（檢視表），不適用實體資料頁 CHECK TABLE 檢查。"

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
                return (
                    f"[BLOCKED] 資料表 {qualified_display} 總容量 ({size_str}) 超過安全檢查上限 ({limit_str})；"
                    "為防止正式環境磁碟 I/O 尖峰與讀取鎖阻塞，已拒絕執行 CHECK TABLE。"
                    "請改用 mysql_inspect_health 進行零鎖表健康巡檢。"
                )

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
            f"目標資料表: {qualified_display} (容量: {format_bytes(total_bytes)})",
        ]
        for row in check_rows:
            if len(row) >= 4:
                lines.append(f"- [{row[2]}] {row[0]} ({row[1]}): {DEFAULT_REDACTION_POLICY.redact(str(row[3]))}")
            else:
                lines.append("- " + ", ".join(DEFAULT_REDACTION_POLICY.redact(str(c)) for c in row))
        return "\n".join(lines)
    except ConnAuthError as exc:
        MYSQL_ADAPTER.audit.log(
            "mysql_check_table",
            sql_executed,
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="AUTH_FAIL",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    except Exception as exc:
        MYSQL_ADAPTER.audit.log(
            "mysql_check_table",
            sql_executed,
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="ERROR",
            detail=str(exc),
        )
        return f"[ERROR] CHECK TABLE 執行失敗: {exc}"
    finally:
        MYSQL_ADAPTER.close_safely(conn)


def register(mcp: Any) -> None:
    """註冊 MySQL 健康檢查與 CHECK TABLE 工具至 MCPServer。"""
    for fn in (mysql_inspect_health, mysql_check_table):
        mcp.tool()(fn)

"""SQLite 資料庫空間、索引、外鍵與實體頁健康檢查工具群組。"""

from pathlib import Path
from typing import Any

from mcp_servers._shared import (
    CONSTANTS,
    ConnAuthError,
    find_redundant_prefix_indexes,
    format_bytes,
    truncate_health_report,
    validate_identifier,
)
from mcp_servers.sqlite.adapter import SQLITE_ADAPTER

SQLITE_QUICK_CHECK_MAX_BYTES = CONSTANTS.sqlite_quick_check_max_bytes
DB_HEALTH_MAX_TABLES = CONSTANTS.db_health_max_tables
DB_FRAGMENTATION_WARN_RATIO = CONSTANTS.db_fragmentation_warn_ratio
DB_FRAGMENTATION_MIN_FREE_BYTES = CONSTANTS.db_fragmentation_min_free_bytes


def sqlite_inspect_health(
    table_name: str = "",
    include_quick_check: bool = True,
    db_path: str = "",
) -> str:
    """檢視 SQLite 資料庫或資料表的頁面空間、碎片率、主鍵／索引、外鍵與快速實體健康狀態"""
    conn = None
    clean_table = (table_name or "").strip().strip("'\"`")
    sql_context = f"HEALTH_CHECK [table={clean_table!r}]"
    try:
        if clean_table:
            try:
                clean_table = validate_identifier(clean_table, "table_name")
            except ValueError as exc:
                SQLITE_ADAPTER.audit.log(
                    "sqlite_inspect_health",
                    sql_context,
                    target=SQLITE_ADAPTER.current_target(db_path),
                    status="BLOCKED",
                    detail=str(exc),
                )
                return f"[ERROR] {exc}"

        conn = SQLITE_ADAPTER.connect(
            "sqlite_inspect_health",
            sql_context,
            db_path=db_path,
            allow_diagnostic_pragmas=True,
        )
        target = SQLITE_ADAPTER.cache.get("DB_PATH")
        cursor = conn.cursor()

        def _scalar_pragma(p_name: str, default: object = 0) -> object:
            try:
                row = cursor.execute(f"PRAGMA {p_name};").fetchone()
                return row[0] if row and row[0] is not None else default
            except Exception:
                return default

        page_size = int(_scalar_pragma("page_size", 4096) or 4096)
        page_count = int(_scalar_pragma("page_count", 0) or 0)
        freelist_count = int(_scalar_pragma("freelist_count", 0) or 0)
        journal_mode = str(_scalar_pragma("journal_mode", "unknown")).upper()
        auto_vacuum_code = int(_scalar_pragma("auto_vacuum", 0) or 0)
        encoding = str(_scalar_pragma("encoding", "UTF-8"))
        auto_vacuum_str = {0: "NONE", 1: "FULL", 2: "INCREMENTAL"}.get(auto_vacuum_code, str(auto_vacuum_code))

        total_bytes = page_size * page_count
        free_bytes = page_size * freelist_count
        used_bytes = max(0, total_bytes - free_bytes)
        try:
            file_bytes = Path(target).stat().st_size if target else total_bytes
        except OSError:
            file_bytes = total_bytes

        if clean_table:
            cursor.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?;",
                (clean_table,),
            )
        else:
            cursor.execute(
                "SELECT name, type FROM sqlite_master "
                "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
                "ORDER BY type, name LIMIT ?;",
                (int(DB_HEALTH_MAX_TABLES),),
            )
        objects = cursor.fetchall()
        if not objects:
            if clean_table:
                SQLITE_ADAPTER.audit.log(
                    "sqlite_inspect_health",
                    sql_context,
                    target=target,
                    status="NOT_FOUND",
                    detail=f"找不到資料表 {clean_table}",
                )
                return f"找不到資料表 {clean_table}。"
            SQLITE_ADAPTER.audit.log(
                "sqlite_inspect_health", sql_context, target=target, status="OK", detail="tables=0"
            )
            return "資料庫中沒有資料表。"

        table_count = 0
        view_count = 0
        overview_lines: list[str] = []
        warnings_list: list[str] = []

        if free_bytes >= DB_FRAGMENTATION_MIN_FREE_BYTES and total_bytes > 0:
            frag_ratio = free_bytes / total_bytes
            if frag_ratio >= DB_FRAGMENTATION_WARN_RATIO:
                warnings_list.append(
                    f"[Fragmentation] 資料庫 Freelist 閒置空間達 {format_bytes(free_bytes)} "
                    f"({frag_ratio * 100:.1f}%)，建議於維護視窗評估執行 VACUUM"
                )

        for obj_name, obj_type in objects:
            t_name = str(obj_name)
            t_type = str(obj_type or "table").lower()
            if t_type == "view":
                view_count += 1
                overview_lines.append(f"- {t_name}: VIEW")
                continue

            table_count += 1
            cursor.execute("SELECT name, type, [notnull], dflt_value, pk FROM pragma_table_info(?);", (t_name,))
            cols = cursor.fetchall()
            has_pk = any(bool(c[4]) for c in cols)
            col_count = len(cols)

            idx_map: dict[str, tuple[bool, list[str]]] = {}
            try:
                cursor.execute("SELECT name, [unique] FROM pragma_index_list(?);", (t_name,))
                for idx_row in cursor.fetchall():
                    iname = str(idx_row[0])
                    is_uniq = bool(idx_row[1])
                    cursor.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno;", (iname,))
                    icols = [str(r[0]) for r in cursor.fetchall() if r[0]]
                    if icols:
                        idx_map[iname] = (is_uniq, icols)
            except Exception:
                pass

            pk_str = "PK=Yes" if has_pk else "PK=No"
            overview_lines.append(f"- {t_name}: Columns={col_count}, {pk_str}, Indexes={len(idx_map)}")
            if not has_pk:
                warnings_list.append(f"[Missing PK] 資料表 {t_name} 缺少顯式主鍵 (Primary Key)")
            for red_msg in find_redundant_prefix_indexes(idx_map):
                warnings_list.append(f"[Redundant Index] {t_name}: {red_msg}")

        quick_check_summary = "未執行 (include_quick_check=False)"
        if file_bytes > SQLITE_QUICK_CHECK_MAX_BYTES:
            limit_str = format_bytes(SQLITE_QUICK_CHECK_MAX_BYTES)
            quick_check_summary = (
                f"已略過（資料庫大小 {format_bytes(file_bytes)} 超過安全門檻 {limit_str}，保護磁碟 I/O）"
            )
        else:
            try:
                if clean_table:
                    cursor.execute(f'PRAGMA foreign_key_check("{clean_table}");')
                else:
                    cursor.execute("PRAGMA foreign_key_check;")
                for violation in cursor.fetchmany(50):
                    if len(violation) >= 4:
                        warnings_list.append(
                            f"[Foreign Key Violation] {violation[0]} (rowid={violation[1]}) -> "
                            f"參照目標表 {violation[2]} (fkid={violation[3]}) 存在孤兒紀錄"
                        )
            except Exception:
                pass

            if include_quick_check:
                try:
                    cursor.execute("PRAGMA quick_check(10);")
                    qc_msgs = [str(r[0]) for r in cursor.fetchall() if r and r[0] is not None]
                    if qc_msgs == ["ok"] or (len(qc_msgs) == 1 and qc_msgs[0].lower() == "ok"):
                        quick_check_summary = "OK (PRAGMA quick_check 通過)"
                    else:
                        quick_check_summary = f"發現異常 ({len(qc_msgs)} 項)"
                        for msg in qc_msgs[:10]:
                            warnings_list.append(f"[Integrity Issue] PRAGMA quick_check: {msg}")
                except Exception as qc_err:
                    quick_check_summary = f"執行失敗 ({qc_err})"

        SQLITE_ADAPTER.audit.log(
            "sqlite_inspect_health",
            sql_context,
            target=target,
            status="OK",
            detail=f"tables={table_count} views={view_count}",
        )

        report: list[str] = [
            "[SQLite Health Summary]",
            f"檔案路徑: {target}" + (f" / 資料表: {clean_table}" if clean_table else ""),
            f"資料表數: {table_count} 張實體表, {view_count} 個檢視表",
            f"檔案容量: {format_bytes(total_bytes)} "
            f"(Used={format_bytes(used_bytes)}, Freelist={format_bytes(free_bytes)}, "
            f"PageSize={page_size} B, Pages={page_count})",
            f"引擎設定: Journal={journal_mode}, AutoVacuum={auto_vacuum_str}, Encoding={encoding}",
            f"快速實體檢查: {quick_check_summary}",
            "",
            "[Health Warnings]",
        ]
        if warnings_list:
            report.extend(f"⚠️ {w}" for w in warnings_list)
        else:
            report.append("✓ 未發現明顯結構、主鍵、外鍵、碎片或實體頁損毀風險。")

        report.extend(["", "[Table Overview]"])
        report.extend(overview_lines)
        return truncate_health_report(report, max_chars=CONSTANTS.max_output_chars)
    except ConnAuthError as exc:
        SQLITE_ADAPTER.audit.log(
            "sqlite_inspect_health",
            sql_context,
            target=SQLITE_ADAPTER.current_target(db_path),
            status="AUTH_FAIL",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"
    except Exception as exc:
        SQLITE_ADAPTER.audit.log(
            "sqlite_inspect_health",
            sql_context,
            target=SQLITE_ADAPTER.current_target(db_path),
            status="ERROR",
            detail=str(exc),
        )
        return f"[ERROR] 健康檢查失敗: {exc}"
    finally:
        SQLITE_ADAPTER.close_safely(conn)


def register(mcp: Any) -> None:
    """註冊 SQLite 健康檢查工具至 MCPServer。"""
    mcp.tool()(sqlite_inspect_health)

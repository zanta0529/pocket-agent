"""SQLite 資料庫空間、索引、外鍵與實體頁健康檢查工具群組。"""

import sqlite3
from pathlib import Path
from typing import Any

from mcp_servers._shared import (
    CONSTANTS,
    clean_param,
    find_redundant_prefix_indexes,
    format_bytes,
    t,
    truncate_health_report,
)
from mcp_servers.sqlite.adapter import SQLITE_ADAPTER
from mcp_servers.sqlite.tools._common import (
    collect_virtual_shadow_tables,
    is_virtual_table_sql,
    quote_sqlite_ident,
    run_readonly_tool,
    validate_sqlite_object_name,
)

SQLITE_QUICK_CHECK_MAX_BYTES = CONSTANTS.sqlite_quick_check_max_bytes
DB_HEALTH_MAX_TABLES = CONSTANTS.db_health_max_tables
DB_FRAGMENTATION_WARN_RATIO = CONSTANTS.db_fragmentation_warn_ratio
DB_FRAGMENTATION_MIN_FREE_BYTES = CONSTANTS.db_fragmentation_min_free_bytes


def _stat_file_bytes(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _sidecar_bytes(target: str) -> tuple[int, int, int]:
    main_path = Path(target)
    main_bytes = _stat_file_bytes(main_path)
    wal_bytes = _stat_file_bytes(Path(f"{target}-wal"))
    shm_bytes = _stat_file_bytes(Path(f"{target}-shm"))
    return main_bytes, wal_bytes, shm_bytes


def _scalar_pragma(cursor: sqlite3.Cursor, pragma_name: str, default: Any = 0) -> Any:
    try:
        row = cursor.execute(f"PRAGMA {pragma_name};").fetchone()
        return row[0] if row and row[0] is not None else default
    except Exception:
        return default


def sqlite_inspect_health(
    table_name: str = "",
    include_quick_check: bool = True,
    db_path: str = "",
) -> str:
    """檢視 SQLite 資料庫或資料表的頁面空間、WAL、碎片率、主鍵／索引、外鍵與快速實體健康狀態"""
    try:
        clean_table = validate_sqlite_object_name(table_name, "table_name")
    except ValueError as exc:
        SQLITE_ADAPTER.audit.log(
            "sqlite_inspect_health",
            f"HEALTH_CHECK [table={table_name!r}]",
            target=SQLITE_ADAPTER.current_target(db_path),
            status="BLOCKED",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"

    sql_context = f"HEALTH_CHECK [table={clean_table!r}]"

    def _run(conn: sqlite3.Connection, target: str) -> str:
        cursor = conn.cursor()
        page_size = int(_scalar_pragma(cursor, "page_size", 4096) or 4096)
        page_count = int(_scalar_pragma(cursor, "page_count", 0) or 0)
        freelist_count = int(_scalar_pragma(cursor, "freelist_count", 0) or 0)
        journal_mode = str(_scalar_pragma(cursor, "journal_mode", "unknown")).upper()
        auto_vacuum_code = int(_scalar_pragma(cursor, "auto_vacuum", 0) or 0)
        encoding = str(_scalar_pragma(cursor, "encoding", "UTF-8"))
        auto_vacuum_str = {0: "NONE", 1: "FULL", 2: "INCREMENTAL"}.get(auto_vacuum_code, str(auto_vacuum_code))

        total_bytes = page_size * page_count
        free_bytes = page_size * freelist_count
        used_bytes = max(0, total_bytes - free_bytes)
        file_bytes, wal_bytes, shm_bytes = _sidecar_bytes(target)
        effective_bytes = (file_bytes or total_bytes) + wal_bytes

        if clean_table:
            cursor.execute(
                "SELECT name, type, sql FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?;",
                (clean_table,),
            )
        else:
            cursor.execute(
                "SELECT name, type, sql FROM sqlite_master "
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
                    detail=f"not_found={clean_table}",
                )
                return t("sqlite_mcp.table_not_found", name=clean_table)
            SQLITE_ADAPTER.audit.log(
                "sqlite_inspect_health", sql_context, target=target, status="OK", detail="tables=0"
            )
            return t("sqlite_mcp.tables_empty")

        virtual_tables, shadow_tables = collect_virtual_shadow_tables(objects)
        table_count = 0
        vtab_count = 0
        view_count = 0
        overview_lines: list[str] = []
        warnings_list: list[str] = []

        if free_bytes >= DB_FRAGMENTATION_MIN_FREE_BYTES and total_bytes > 0:
            frag_ratio = free_bytes / total_bytes
            if frag_ratio >= DB_FRAGMENTATION_WARN_RATIO:
                warnings_list.append(
                    t("sqlite_mcp.health_warn_frag", free=format_bytes(free_bytes), ratio=frag_ratio * 100)
                )
        if wal_bytes >= DB_FRAGMENTATION_MIN_FREE_BYTES:
            warnings_list.append(t("sqlite_mcp.health_warn_wal_large", wal_size=format_bytes(wal_bytes)))

        for obj_name, obj_type, _obj_sql in objects:
            t_name = str(obj_name)
            t_type = str(obj_type or "table").lower()
            if t_type == "view":
                view_count += 1
                overview_lines.append(f"- {t_name}: VIEW")
                continue
            if t_name in virtual_tables:
                vtab_count += 1
                overview_lines.append(f"- {t_name}: VIRTUAL TABLE")
                continue
            if t_name in shadow_tables:
                overview_lines.append(f"- {t_name}: VIRTUAL SHADOW")
                continue

            table_count += 1
            cursor.execute("SELECT name, type, [notnull], dflt_value, pk FROM pragma_table_info(?);", (t_name,))
            cols = cursor.fetchall()
            has_pk = any(bool(c[4]) for c in cols)

            idx_map: dict[str, tuple[bool, list[str]]] = {}
            idx_total = 0
            try:
                cursor.execute("SELECT name, [unique], origin, partial FROM pragma_index_list(?);", (t_name,))
                idx_rows = cursor.fetchall()
                idx_total = len(idx_rows)
                for idx_row in idx_rows:
                    iname = str(idx_row[0])
                    is_uniq = bool(idx_row[1])
                    is_partial = bool(idx_row[3]) if len(idx_row) > 3 else False
                    if is_partial:
                        continue
                    cursor.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno;", (iname,))
                    raw_cols = [r[0] for r in cursor.fetchall()]
                    if raw_cols and all(c is not None for c in raw_cols):
                        idx_map[iname] = (is_uniq, [str(c) for c in raw_cols])
            except Exception:
                pass

            pk_str = "PK=Yes" if has_pk else "PK=No"
            overview_lines.append(f"- {t_name}: Columns={len(cols)}, {pk_str}, Indexes={idx_total}")
            if not has_pk:
                warnings_list.append(t("sqlite_mcp.health_warn_missing_pk", table=t_name))
            for red_msg in find_redundant_prefix_indexes(idx_map):
                warnings_list.append(t("sqlite_mcp.health_warn_redundant_idx", table=t_name, msg=red_msg))

        scan_skipped = effective_bytes > SQLITE_QUICK_CHECK_MAX_BYTES
        limit_str = format_bytes(SQLITE_QUICK_CHECK_MAX_BYTES)
        size_str = format_bytes(effective_bytes)
        fk_summary = t("sqlite_mcp.health_fk_check_ok")
        quick_check_summary = t("sqlite_mcp.health_quick_check_disabled")

        if scan_skipped:
            fk_summary = t("sqlite_mcp.health_fk_check_skipped_size", size=size_str, limit=limit_str)
            quick_check_summary = t("sqlite_mcp.health_quick_check_skipped_size", size=size_str, limit=limit_str)
        else:
            try:
                fk_violations = 0
                if clean_table:
                    real_name = str(objects[0][0])
                    real_type = str(objects[0][1] or "table").lower()
                    if real_type == "table" and real_name not in virtual_tables and real_name not in shadow_tables:
                        cursor.execute(f"PRAGMA foreign_key_check({quote_sqlite_ident(real_name)});")
                        rows = cursor.fetchmany(int(CONSTANTS.sqlite_fk_check_max_violations))
                    else:
                        rows = []
                else:
                    cursor.execute("PRAGMA foreign_key_check;")
                    rows = cursor.fetchmany(int(CONSTANTS.sqlite_fk_check_max_violations))

                for violation in rows:
                    if len(violation) >= 4:
                        fk_violations += 1
                        warnings_list.append(
                            t(
                                "sqlite_mcp.health_warn_fk",
                                table=violation[0],
                                rowid=violation[1],
                                parent=violation[2],
                                fkid=violation[3],
                            )
                        )
                if fk_violations:
                    fk_summary = t("sqlite_mcp.health_fk_check_found", count=fk_violations)
            except Exception as fk_err:
                fk_summary = t("sqlite_mcp.health_fk_check_failed", error=fk_err)

            if include_quick_check:
                try:
                    max_errs = int(CONSTANTS.sqlite_quick_check_max_errors)
                    cursor.execute(f"PRAGMA quick_check({max_errs});")
                    qc_msgs = [str(r[0]) for r in cursor.fetchall() if r and r[0] is not None]
                    if qc_msgs == ["ok"] or (len(qc_msgs) == 1 and qc_msgs[0].lower() == "ok"):
                        quick_check_summary = t("sqlite_mcp.health_quick_check_ok")
                    else:
                        quick_check_summary = t("sqlite_mcp.health_quick_check_issues", count=len(qc_msgs))
                        for msg in qc_msgs[:max_errs]:
                            warnings_list.append(t("sqlite_mcp.health_warn_integrity", mode="quick_check", msg=msg))
                except Exception as qc_err:
                    quick_check_summary = t("sqlite_mcp.health_quick_check_failed", error=qc_err)

        SQLITE_ADAPTER.audit.log(
            "sqlite_inspect_health",
            sql_context,
            target=target,
            status="OK",
            detail=f"tables={table_count} vtabs={vtab_count} views={view_count}",
        )

        path_line = f"{t('sqlite_mcp.health_path_label')}: {target}"
        if clean_table:
            path_line += f" / {t('sqlite_mcp.health_table_label')}: {clean_table}"

        report: list[str] = [
            t("sqlite_mcp.health_header"),
            path_line,
            t("sqlite_mcp.health_counts", tables=table_count, vtabs=vtab_count, views=view_count),
            t(
                "sqlite_mcp.health_capacity",
                total=format_bytes(total_bytes),
                used=format_bytes(used_bytes),
                free=format_bytes(free_bytes),
                page_size=page_size,
                pages=page_count,
            ),
        ]
        if wal_bytes or shm_bytes:
            report.append(
                t("sqlite_mcp.health_sidecar_sizes", wal=format_bytes(wal_bytes), shm=format_bytes(shm_bytes))
            )
        report.extend(
            [
                t(
                    "sqlite_mcp.health_engine",
                    journal=journal_mode,
                    auto_vacuum=auto_vacuum_str,
                    encoding=encoding,
                ),
                f"{t('sqlite_mcp.health_label_fk_check')}: {fk_summary}",
                f"{t('sqlite_mcp.health_label_quick_check')}: {quick_check_summary}",
                "",
                t("sqlite_mcp.health_warnings_header"),
            ]
        )
        if warnings_list:
            report.extend(f"⚠️ {w}" for w in warnings_list)
        elif scan_skipped:
            report.append(t("sqlite_mcp.health_no_warnings_scan_skipped"))
        else:
            report.append(t("sqlite_mcp.health_no_warnings"))

        report.extend(["", t("sqlite_mcp.health_overview_header")])
        report.extend(overview_lines)
        return truncate_health_report(report, max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "sqlite_inspect_health",
        _run,
        sql_hint=sql_context,
        db_path=db_path,
        allow_diagnostic_pragmas=True,
        error_key="sqlite_mcp.health_failed",
    )


def sqlite_check_integrity(
    table_name: str = "",
    check_mode: str = "quick",
    include_fk_check: bool = True,
    db_path: str = "",
) -> str:
    """執行 SQLite 資料庫或單一資料表之實體／索引完整性檢查（支援 quick 與 full 模式，受 500 MB 熔斷保護）"""
    clean_mode = clean_param(check_mode).lower() or "quick"
    if clean_mode not in ("quick", "full"):
        err = t("sqlite_mcp.integrity_invalid_mode", mode=check_mode)
        SQLITE_ADAPTER.audit.log(
            "sqlite_check_integrity",
            f"INTEGRITY_CHECK [mode={check_mode!r}]",
            target=SQLITE_ADAPTER.current_target(db_path),
            status="BLOCKED",
            detail=err,
        )
        return f"[ERROR] {err}"

    try:
        clean_table = validate_sqlite_object_name(table_name, "table_name")
    except ValueError as exc:
        SQLITE_ADAPTER.audit.log(
            "sqlite_check_integrity",
            f"INTEGRITY_CHECK [table={table_name!r}]",
            target=SQLITE_ADAPTER.current_target(db_path),
            status="BLOCKED",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"

    pragma_base = "quick_check" if clean_mode == "quick" else "integrity_check"
    sql_context = f"PRAGMA {pragma_base} [table={clean_table!r}]"

    def _run(conn: sqlite3.Connection, target: str) -> str:
        cursor = conn.cursor()
        file_bytes, wal_bytes, _ = _sidecar_bytes(target)
        effective_bytes = file_bytes + wal_bytes
        if effective_bytes > SQLITE_QUICK_CHECK_MAX_BYTES:
            SQLITE_ADAPTER.audit.log(
                "sqlite_check_integrity",
                sql_context,
                target=target,
                status="SKIPPED",
                detail=f"size={effective_bytes}",
            )
            return "\n".join(
                [
                    t("sqlite_mcp.integrity_header"),
                    f"{t('sqlite_mcp.health_path_label')}: {target}",
                    t(
                        "sqlite_mcp.integrity_skipped_size",
                        size=format_bytes(effective_bytes),
                        limit=format_bytes(SQLITE_QUICK_CHECK_MAX_BYTES),
                    ),
                ]
            )

        real_table = ""
        if clean_table:
            cursor.execute(
                "SELECT name, type, sql FROM sqlite_master WHERE type = 'table' AND name = ?;",
                (clean_table,),
            )
            row = cursor.fetchone()
            if not row or is_virtual_table_sql(row[2]):
                SQLITE_ADAPTER.audit.log(
                    "sqlite_check_integrity",
                    sql_context,
                    target=target,
                    status="NOT_FOUND",
                    detail=f"not_found={clean_table}",
                )
                return t("sqlite_mcp.table_not_found", name=clean_table)
            real_table = str(row[0])

        max_errs = int(CONSTANTS.sqlite_quick_check_max_errors)
        if real_table:
            try:
                cursor.execute(f"PRAGMA {pragma_base}({quote_sqlite_ident(real_table)});")
            except sqlite3.OperationalError:
                return f"[ERROR] {t('sqlite_mcp.integrity_single_table_unsupported', pragma=pragma_base)}"
        else:
            cursor.execute(f"PRAGMA {pragma_base}({max_errs});")

        msgs = [str(r[0]) for r in cursor.fetchall() if r and r[0] is not None]
        is_ok = msgs == ["ok"] or (len(msgs) == 1 and msgs[0].lower() == "ok")

        lines: list[str] = [
            t("sqlite_mcp.integrity_header"),
            f"{t('sqlite_mcp.health_path_label')}: {target}",
            f"{t('sqlite_mcp.integrity_mode_label')}: {clean_mode.upper()} (PRAGMA {pragma_base})",
            t("sqlite_mcp.integrity_scope_table", table=real_table)
            if real_table
            else t("sqlite_mcp.integrity_scope_all"),
        ]
        if is_ok:
            lines.append(t("sqlite_mcp.integrity_ok", pragma=pragma_base))
        else:
            lines.append(t("sqlite_mcp.integrity_issues", pragma=pragma_base, count=len(msgs)))
            lines.extend(f"- {m}" for m in msgs[:max_errs])

        if include_fk_check:
            if real_table:
                cursor.execute(f"PRAGMA foreign_key_check({quote_sqlite_ident(real_table)});")
            else:
                cursor.execute("PRAGMA foreign_key_check;")
            fk_rows = cursor.fetchmany(int(CONSTANTS.sqlite_fk_check_max_violations))
            if fk_rows:
                fk_found = t("sqlite_mcp.health_fk_check_found", count=len(fk_rows))
                lines.append(f"{t('sqlite_mcp.health_label_fk_check')}: {fk_found}")
                for violation in fk_rows:
                    if len(violation) >= 4:
                        warn_msg = t(
                            "sqlite_mcp.health_warn_fk",
                            table=violation[0],
                            rowid=violation[1],
                            parent=violation[2],
                            fkid=violation[3],
                        )
                        lines.append(f"⚠️ {warn_msg}")
            else:
                lines.append(f"{t('sqlite_mcp.health_label_fk_check')}: {t('sqlite_mcp.health_fk_check_ok')}")

        SQLITE_ADAPTER.audit.log(
            "sqlite_check_integrity",
            sql_context,
            target=target,
            status="OK" if is_ok else "ISSUES",
            detail=f"mode={clean_mode} ok={is_ok}",
        )
        return truncate_health_report(lines, max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "sqlite_check_integrity",
        _run,
        sql_hint=sql_context,
        db_path=db_path,
        allow_diagnostic_pragmas=True,
        error_key="sqlite_mcp.integrity_failed",
    )


def register(mcp: Any) -> None:
    """註冊 SQLite 健康與完整性檢查工具至 MCPServer。"""
    for fn in (
        sqlite_inspect_health,
        sqlite_check_integrity,
    ):
        mcp.tool()(fn)

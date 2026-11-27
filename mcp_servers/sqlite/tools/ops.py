"""SQLite 資料庫中繼狀態資訊與空間統計工具群組。"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp_servers._shared import CONSTANTS, format_bytes, t, validate_positive_int
from mcp_servers.sqlite.adapter import SQLITE_ADAPTER
from mcp_servers.sqlite.tools._common import is_virtual_table_sql, run_readonly_tool, truncate_text

_CAPABILITY_PREFIXES = (
    "ENABLE_FTS5",
    "ENABLE_FTS4",
    "ENABLE_JSON1",
    "ENABLE_RTREE",
    "ENABLE_DBSTAT_VTAB",
    "ENABLE_GEOPOLY",
    "THREADSAFE=",
    "MAX_VARIABLE_NUMBER=",
)


def _stat_info(path: Path) -> tuple[int, str]:
    try:
        if not path.is_file():
            return 0, "N/A"
        st = path.stat()
        mtime = datetime.fromtimestamp(st.st_mtime, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        return st.st_size, mtime
    except OSError:
        return 0, "N/A"


def _scalar_pragma(cursor: sqlite3.Cursor, name: str, default: Any = "") -> Any:
    try:
        row = cursor.execute(f"PRAGMA {name};").fetchone()
        return row[0] if row and row[0] is not None else default
    except Exception:
        return default


def sqlite_database_info(db_path: str = "") -> str:
    """查詢 SQLite 引擎版本、編譯能力、主檔與 WAL 容量、PRAGMA 設定與物件統計"""
    sql_hint = "SQLITE_DATABASE_INFO"

    def _run(conn: sqlite3.Connection, target: str) -> str:
        cursor = conn.cursor()
        ver_row = cursor.execute("SELECT sqlite_version();").fetchone()
        version = str(ver_row[0]) if ver_row else sqlite3.sqlite_version

        main_size, mtime = _stat_info(Path(target))
        wal_size, _ = _stat_info(Path(f"{target}-wal"))
        shm_size, _ = _stat_info(Path(f"{target}-shm"))

        journal = str(_scalar_pragma(cursor, "journal_mode", "unknown")).upper()
        auto_vac_code = int(_scalar_pragma(cursor, "auto_vacuum", 0) or 0)
        auto_vacuum = {0: "NONE", 1: "FULL", 2: "INCREMENTAL"}.get(auto_vac_code, str(auto_vac_code))
        encoding = str(_scalar_pragma(cursor, "encoding", "UTF-8"))
        fk_enabled = "ON" if int(_scalar_pragma(cursor, "foreign_keys", 0) or 0) else "OFF"
        page_size = int(_scalar_pragma(cursor, "page_size", 4096) or 4096)
        page_count = int(_scalar_pragma(cursor, "page_count", 0) or 0)
        freelist = int(_scalar_pragma(cursor, "freelist_count", 0) or 0)
        user_ver = int(_scalar_pragma(cursor, "user_version", 0) or 0)
        app_id = int(_scalar_pragma(cursor, "application_id", 0) or 0)
        schema_ver = int(_scalar_pragma(cursor, "schema_version", 0) or 0)

        cursor.execute("SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%';")
        counts = {"table": 0, "vtab": 0, "view": 0, "index": 0, "trigger": 0}
        for _name, obj_type, obj_sql in cursor.fetchall():
            otype = str(obj_type or "").lower()
            if otype == "table":
                if is_virtual_table_sql(obj_sql):
                    counts["vtab"] += 1
                else:
                    counts["table"] += 1
            elif otype in counts:
                counts[otype] += 1

        caps: list[str] = []
        try:
            cursor.execute("PRAGMA compile_options;")
            for (opt,) in cursor.fetchall():
                opt_str = str(opt)
                if any(opt_str.startswith(prefix) for prefix in _CAPABILITY_PREFIXES):
                    caps.append(opt_str)
        except Exception:
            pass

        SQLITE_ADAPTER.audit.log(
            "sqlite_database_info",
            sql_hint,
            target=target,
            status="OK",
            detail=f"version={version} tables={counts['table']}",
        )
        lines = [
            t("sqlite_mcp.info_header"),
            t("sqlite_mcp.info_version", version=version),
            t("sqlite_mcp.info_path", path=target),
            t(
                "sqlite_mcp.info_file_stats",
                main_size=format_bytes(main_size),
                wal_size=format_bytes(wal_size),
                shm_size=format_bytes(shm_size),
                mtime=mtime,
            ),
            t(
                "sqlite_mcp.info_pragmas",
                journal=journal,
                auto_vacuum=auto_vacuum,
                encoding=encoding,
                fk=fk_enabled,
                page_size=page_size,
                pages=page_count,
                freelist=freelist,
            ),
            t(
                "sqlite_mcp.info_versions",
                user_ver=user_ver,
                app_id=f"0x{app_id:08x}",
                schema_ver=schema_ver,
            ),
            t(
                "sqlite_mcp.info_objects",
                tables=counts["table"],
                vtabs=counts["vtab"],
                views=counts["view"],
                indexes=counts["index"],
                triggers=counts["trigger"],
            ),
            t("sqlite_mcp.info_capabilities", caps=", ".join(caps) if caps else "N/A"),
        ]
        return truncate_text("\n".join(lines))

    return run_readonly_tool(
        "sqlite_database_info",
        _run,
        sql_hint=sql_hint,
        db_path=db_path,
        allow_diagnostic_pragmas=True,
        error_key="sqlite_mcp.info_failed",
    )


def sqlite_table_stats(top_n: int = CONSTANTS.sqlite_table_stats_default_top, db_path: str = "") -> str:
    """統計資料表與索引之實體頁數與儲存空間佔比（優先使用 dbstat，不支援時降級至 sqlite_stat1／結構摘要）"""
    try:
        valid_top = validate_positive_int(top_n, "top_n", max_val=CONSTANTS.sqlite_table_stats_max_top)
    except ValueError as exc:
        SQLITE_ADAPTER.audit.log(
            "sqlite_table_stats",
            f"TABLE_STATS [top_n={top_n!r}]",
            target=SQLITE_ADAPTER.current_target(db_path),
            status="BLOCKED",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"

    sql_hint = f"SQLITE_TABLE_STATS [top_n={valid_top}]"

    def _run(conn: sqlite3.Connection, target: str) -> str:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name, type, tbl_name FROM sqlite_master "
            "WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%';"
        )
        meta = {str(r[0]): (str(r[1]).upper(), str(r[2])) for r in cursor.fetchall()}

        try:
            cursor.execute(
                "SELECT name, COUNT(*) AS pages, SUM(pgsize) AS total_bytes, SUM(unused) AS unused_bytes "
                "FROM dbstat GROUP BY name ORDER BY total_bytes DESC, name ASC LIMIT ?;",
                (valid_top,),
            )
            dbstat_rows = cursor.fetchall()
            if not dbstat_rows:
                return t("sqlite_mcp.stats_empty")

            total_row = cursor.execute("SELECT COALESCE(SUM(pgsize), 0) FROM dbstat;").fetchone()
            db_total_bytes = max(1, int(total_row[0] or 1)) if total_row else 1

            lines = [
                t(
                    "sqlite_mcp.stats_header",
                    count=len(dbstat_rows),
                    source=t("sqlite_mcp.stats_source_dbstat"),
                )
            ]
            for name, pages, total_b, unused_b in dbstat_rows:
                obj_name = str(name)
                total_val = int(total_b or 0)
                unused_val = int(unused_b or 0)
                pct = (total_val / db_total_bytes) * 100.0
                otype, tbl = meta.get(obj_name, ("SYSTEM", obj_name))
                kind_str = f"{otype} on {tbl}" if otype == "INDEX" else otype
                lines.append(
                    f"- {obj_name} [{kind_str}]: {format_bytes(total_val)} ({pct:.1f}%), "
                    f"Pages={int(pages or 0)}, Unused={format_bytes(unused_val)}"
                )

            SQLITE_ADAPTER.audit.log(
                "sqlite_table_stats",
                sql_hint,
                target=target,
                status="OK",
                detail=f"source=dbstat rows={len(dbstat_rows)}",
            )
            return truncate_text("\n".join(lines))
        except sqlite3.OperationalError:
            pass

        # 降級路徑：無 dbstat 虛擬表時讀取 sqlite_stat1 與表結構摘要，絕不執行全表 COUNT(*)
        stat1_rows: dict[str, str] = {}
        try:
            cursor.execute("SELECT tbl, stat FROM sqlite_stat1;")
            for tbl_name, stat_str in cursor.fetchall():
                stat1_rows.setdefault(str(tbl_name), str(stat_str or "").split(" ", 1)[0])
        except sqlite3.OperationalError:
            pass

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT ?;",
            (valid_top,),
        )
        tbl_names = [str(r[0]) for r in cursor.fetchall()]
        if not tbl_names:
            return t("sqlite_mcp.stats_empty")

        lines = [
            t(
                "sqlite_mcp.stats_header",
                count=len(tbl_names),
                source=t("sqlite_mcp.stats_source_fallback"),
            )
        ]
        for t_name in tbl_names:
            col_cnt = len(cursor.execute("SELECT name FROM pragma_table_info(?);", (t_name,)).fetchall())
            idx_cnt = len(cursor.execute("SELECT name FROM pragma_index_list(?);", (t_name,)).fetchall())
            est_rows = stat1_rows.get(t_name, "N/A (no sqlite_stat1)")
            lines.append(f"- {t_name}: ApproxRows={est_rows}, Columns={col_cnt}, Indexes={idx_cnt}")

        SQLITE_ADAPTER.audit.log(
            "sqlite_table_stats",
            sql_hint,
            target=target,
            status="OK",
            detail=f"source=fallback rows={len(tbl_names)}",
        )
        return truncate_text("\n".join(lines))

    return run_readonly_tool(
        "sqlite_table_stats",
        _run,
        sql_hint=sql_hint,
        db_path=db_path,
        error_key="sqlite_mcp.stats_failed",
    )


def register(mcp: Any) -> None:
    """註冊 SQLite 中繼狀態與空間統計工具至 MCPServer。"""
    for fn in (
        sqlite_database_info,
        sqlite_table_stats,
    ):
        mcp.tool()(fn)

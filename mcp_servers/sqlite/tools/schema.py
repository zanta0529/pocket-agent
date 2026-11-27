"""SQLite 結構探查與 DDL 定義檢視工具群組。"""

import sqlite3
from typing import Any

from mcp_servers._shared import DEFAULT_REDACTION_POLICY, SENSITIVE_COL_RE, clean_param, t
from mcp_servers.sqlite.adapter import SQLITE_ADAPTER
from mcp_servers.sqlite.tools._common import (
    is_virtual_table_sql,
    run_readonly_tool,
    truncate_text,
    validate_sqlite_object_name,
)

_LIST_VALID_TYPES = frozenset({"", "all", "table", "view", "index", "trigger"})
_DDL_VALID_TYPES = frozenset({"", "table", "view", "index", "trigger"})


def sqlite_list_tables(object_type: str = "", name_pattern: str = "", db_path: str = "") -> str:
    """列出 SQLite 資料庫中的所有資料表、檢視表（或指定索引／觸發器）清單"""
    clean_type = clean_param(object_type).lower()
    clean_pattern = clean_param(name_pattern)
    if clean_type not in _LIST_VALID_TYPES:
        err = t("sqlite_mcp.invalid_object_type", value=object_type)
        SQLITE_ADAPTER.audit.log(
            "sqlite_list_tables",
            f"LIST_OBJECTS [type={clean_type!r}]",
            target=SQLITE_ADAPTER.current_target(db_path),
            status="BLOCKED",
            detail=err,
        )
        return f"[ERROR] {err}"

    params: list[Any] = []
    if not clean_type:
        type_clause = "type IN ('table', 'view')"
    elif clean_type == "all":
        type_clause = "type IN ('table', 'view', 'index', 'trigger')"
    else:
        type_clause = "type = ?"
        params.append(clean_type)

    like_clause = ""
    if clean_pattern:
        escaped = clean_pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_clause = " AND name LIKE ? ESCAPE '\\'"
        params.append(f"%{escaped}%")

    sql = (
        f"SELECT name, type, tbl_name, sql FROM sqlite_master "
        f"WHERE {type_clause}{like_clause} AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name;"
    )

    def _run(conn: sqlite3.Connection, target: str) -> str:
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        SQLITE_ADAPTER.audit.log("sqlite_list_tables", sql, target=target, status="OK", detail=f"objects={len(rows)}")
        if not rows:
            if clean_type or clean_pattern:
                return t("sqlite_mcp.tables_empty_filtered")
            return t("sqlite_mcp.tables_empty")

        lines: list[str] = []
        for name, obj_type, tbl_name, obj_sql in rows:
            otype = str(obj_type or "").lower()
            if otype == "view":
                lines.append(f"{name} (VIEW)")
            elif otype == "table" and is_virtual_table_sql(obj_sql):
                lines.append(f"{name} (VIRTUAL TABLE)")
            elif otype in ("index", "trigger"):
                lines.append(f"{name} ({otype.upper()} on {tbl_name})")
            else:
                lines.append(str(name))
        return truncate_text("\n".join(lines))

    return run_readonly_tool("sqlite_list_tables", _run, sql_hint=sql, db_path=db_path)


def sqlite_describe_schema(table_name: str, db_path: str = "") -> str:
    """顯示指定 SQLite 資料表或檢視表的 Schema（含欄位、產生欄、遮罩預設值、主鍵順位、索引與外鍵）"""
    try:
        clean_name = validate_sqlite_object_name(table_name, "table_name")
    except ValueError as exc:
        SQLITE_ADAPTER.audit.log(
            "sqlite_describe_schema",
            f"DESCRIBE [table_name={table_name!r}]",
            target=SQLITE_ADAPTER.current_target(db_path),
            status="BLOCKED",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"

    if not clean_name:
        err = t("sqlite_mcp.missing_table_name")
        SQLITE_ADAPTER.audit.log(
            "sqlite_describe_schema",
            "DESCRIBE [table_name='']",
            target=SQLITE_ADAPTER.current_target(db_path),
            status="BLOCKED",
            detail=err,
        )
        return f"[ERROR] {err}"

    sql = "SELECT name, type, [notnull], dflt_value, pk, hidden FROM pragma_table_xinfo(?);"
    sql_hint = f"{sql} [table={clean_name!r}]"

    def _run(conn: sqlite3.Connection, target: str) -> str:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT type, sql FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?;",
            (clean_name,),
        )
        meta_row = cursor.fetchone()
        cursor.execute(sql, (clean_name,))
        columns = cursor.fetchall()
        if not columns:
            SQLITE_ADAPTER.audit.log(
                "sqlite_describe_schema",
                sql_hint,
                target=target,
                status="NOT_FOUND",
                detail=f"not_found={clean_name}",
            )
            return t("sqlite_mcp.table_not_found", name=clean_name)

        SQLITE_ADAPTER.audit.log(
            "sqlite_describe_schema",
            sql_hint,
            target=target,
            status="OK",
            detail=f"columns={len(columns)}",
        )

        visible_cols = [c for c in columns if int(c[5] or 0) != 1]
        pk_count = sum(1 for c in visible_cols if int(c[4] or 0) > 0)

        lines: list[str] = []
        obj_type = str(meta_row[0]).lower() if meta_row and meta_row[0] else "table"
        obj_sql = str(meta_row[1] or "") if meta_row and meta_row[1] else ""
        if obj_type == "view":
            lines.append(f"=== {clean_name} (VIEW) ===")
        elif is_virtual_table_sql(obj_sql):
            lines.append(f"=== {clean_name} (VIRTUAL TABLE) ===")

        for col_name, col_type_raw, not_null, dflt_val, pk_order, hidden in visible_cols:
            col_type = col_type_raw or "TEXT"
            pk_int = int(pk_order or 0)
            pk_tag = f" [PK#{pk_int}]" if pk_int > 0 and pk_count > 1 else (" [PK]" if pk_int > 0 else "")
            hidden_int = int(hidden or 0)
            gen_tag = " [VIRTUAL GENERATED]" if hidden_int == 2 else (" [STORED GENERATED]" if hidden_int == 3 else "")
            null_tag = " NOT NULL" if not_null else ""
            default_tag = ""
            if dflt_val is not None:
                if SENSITIVE_COL_RE.search(str(col_name)):
                    safe_dflt = "[REDACTED]"
                else:
                    safe_dflt = DEFAULT_REDACTION_POLICY.redact(str(dflt_val))
                default_tag = f" DEFAULT {safe_dflt}"
            lines.append(f"{col_name} ({col_type}{pk_tag}{gen_tag}{null_tag}{default_tag})")

        if obj_type != "view":
            try:
                cursor.execute("SELECT name, [unique], origin, partial FROM pragma_index_list(?);", (clean_name,))
                indexes = cursor.fetchall()
                if indexes:
                    lines.append("\n[Indexes]")
                    for idx_row in indexes:
                        idx_name, is_unique, _origin, is_partial = idx_row[0], idx_row[1], idx_row[2], idx_row[3]
                        cursor.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno;", (idx_name,))
                        idx_cols = [str(r[0]) if r[0] is not None else "<expr>" for r in cursor.fetchall()]
                        uniq_str = "UNIQUE " if is_unique else ""
                        partial_str = " [PARTIAL]" if is_partial else ""
                        lines.append(f"- {idx_name}: {uniq_str}({', '.join(idx_cols)}){partial_str}")

                cursor.execute(
                    "SELECT [from], [table], [to], on_update, on_delete FROM pragma_foreign_key_list(?);",
                    (clean_name,),
                )
                fks = cursor.fetchall()
                if fks:
                    lines.append("\n[Foreign Keys]")
                    for fk_from, fk_table, fk_to, on_update, on_delete in fks:
                        actions: list[str] = []
                        if on_delete and str(on_delete).upper() != "NO ACTION":
                            actions.append(f"ON DELETE {on_delete}")
                        if on_update and str(on_update).upper() != "NO ACTION":
                            actions.append(f"ON UPDATE {on_update}")
                        action_str = f" ({', '.join(actions)})" if actions else ""
                        lines.append(f"- {fk_from} -> {fk_table}.{fk_to}{action_str}")
            except Exception:
                pass

        return truncate_text("\n".join(lines))

    return run_readonly_tool("sqlite_describe_schema", _run, sql_hint=sql_hint, db_path=db_path)


def sqlite_get_ddl(
    object_name: str,
    object_type: str = "",
    include_related: bool = True,
    db_path: str = "",
) -> str:
    """唯讀取得 SQLite 資料表、檢視表、索引或觸發器的原始 CREATE DDL 定義"""
    clean_type = clean_param(object_type).lower()
    if clean_type not in _DDL_VALID_TYPES:
        err = t("sqlite_mcp.invalid_object_type", value=object_type)
        SQLITE_ADAPTER.audit.log(
            "sqlite_get_ddl",
            f"GET_DDL [object_type={object_type!r}]",
            target=SQLITE_ADAPTER.current_target(db_path),
            status="BLOCKED",
            detail=err,
        )
        return f"[ERROR] {err}"

    try:
        clean_name = validate_sqlite_object_name(object_name, "object_name")
    except ValueError as exc:
        SQLITE_ADAPTER.audit.log(
            "sqlite_get_ddl",
            f"GET_DDL [object_name={object_name!r}]",
            target=SQLITE_ADAPTER.current_target(db_path),
            status="BLOCKED",
            detail=str(exc),
        )
        return f"[ERROR] {exc}"

    if not clean_name:
        err = t("sqlite_mcp.missing_object_name")
        SQLITE_ADAPTER.audit.log(
            "sqlite_get_ddl",
            "GET_DDL [object_name='']",
            target=SQLITE_ADAPTER.current_target(db_path),
            status="BLOCKED",
            detail=err,
        )
        return f"[ERROR] {err}"

    sql_hint = f"SELECT type, name, sql FROM sqlite_master WHERE name={clean_name!r} type={clean_type!r}"

    def _run(conn: sqlite3.Connection, target: str) -> str:
        cursor = conn.cursor()
        if clean_type:
            cursor.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name = ? AND type = ? AND name NOT LIKE 'sqlite_%';",
                (clean_name, clean_type),
            )
        else:
            cursor.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name = ? AND name NOT LIKE 'sqlite_%' ORDER BY type;",
                (clean_name,),
            )
        rows = cursor.fetchall()
        if not rows:
            SQLITE_ADAPTER.audit.log(
                "sqlite_get_ddl",
                sql_hint,
                target=target,
                status="NOT_FOUND",
                detail=f"not_found={clean_name}",
            )
            return t("sqlite_mcp.object_not_found", name=clean_name)

        sections: list[str] = []
        for obj_type, obj_name, _tbl_name, obj_sql in rows:
            sections.append(t("sqlite_mcp.ddl_header", obj_type=str(obj_type).upper(), name=obj_name))
            sections.append(f"{obj_sql};" if obj_sql else t("sqlite_mcp.ddl_unavailable"))

            if include_related and str(obj_type).lower() == "table":
                cursor.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL "
                    "ORDER BY type, name;",
                    (obj_name,),
                )
                rel_rows = cursor.fetchall()
                rel_indexes = [r for r in rel_rows if str(r[0]).lower() == "index"]
                rel_triggers = [r for r in rel_rows if str(r[0]).lower() == "trigger"]
                if rel_indexes:
                    sections.extend(["", t("sqlite_mcp.ddl_related_indexes")])
                    for _t, _iname, isql in rel_indexes:
                        sections.append(f"{isql};")
                if rel_triggers:
                    sections.extend(["", t("sqlite_mcp.ddl_related_triggers")])
                    for _t, _trname, trsql in rel_triggers:
                        sections.append(f"{trsql};")

        SQLITE_ADAPTER.audit.log("sqlite_get_ddl", sql_hint, target=target, status="OK", detail=f"matches={len(rows)}")
        return truncate_text("\n".join(sections))

    return run_readonly_tool("sqlite_get_ddl", _run, sql_hint=sql_hint, db_path=db_path)


def register(mcp: Any) -> None:
    """註冊 SQLite 結構探查與 DDL 檢視工具至 MCPServer。"""
    for fn in (
        sqlite_list_tables,
        sqlite_describe_schema,
        sqlite_get_ddl,
    ):
        mcp.tool()(fn)

"""Microsoft SQL Server (MSSQL) 伺服器狀態、活動請求、備份與 Always On AG 診斷工具群組。"""

from typing import Any

from mcp_servers._shared import CONSTANTS, DEFAULT_REDACTION_POLICY, clean_param, t, validate_identifier
from mcp_servers.mssql.adapter import MSSQL_ADAPTER, adapt_placeholders
from mcp_servers.mssql.tools._common import (
    format_mssql_error,
    is_permission_error,
    run_readonly_tool,
    truncate_text,
)


def mssql_server_status(
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """唯讀查詢 SQL Server 版本、Edition、HADR 狀態、啟動時間、連線數與記憶體配置"""

    def _run(conn: Any, _target_db: str, target: str) -> str:
        cursor = conn.cursor()
        degraded = False
        sys_info: tuple[Any, ...] | None = None
        session_count: int | None = None
        max_mem_mb: int | None = None
        try:
            cursor.execute(
                "SELECT @@VERSION, "
                "SERVERPROPERTY('ProductVersion'), SERVERPROPERTY('ProductLevel'), "
                "SERVERPROPERTY('Edition'), SERVERPROPERTY('Collation'), "
                "SERVERPROPERTY('IsHadrEnabled'), SERVERPROPERTY('IsClustered');"
            )
            prop_row = cursor.fetchone() or ()
            try:
                cursor.execute(
                    "SELECT sqlserver_start_time, cpu_count, physical_memory_kb / 1024 FROM sys.dm_os_sys_info;"
                )
                sys_info = cursor.fetchone()
                cursor.execute("SELECT COUNT(*) FROM sys.dm_exec_sessions WHERE is_user_process = 1;")
                sess_row = cursor.fetchone()
                session_count = int(sess_row[0]) if sess_row else None
            except Exception as dmv_exc:
                if not is_permission_error(dmv_exc):
                    raise
                degraded = True

            try:
                cursor.execute("SELECT value_in_use FROM sys.configurations WHERE name = 'max server memory (MB)';")
                mem_row = cursor.fetchone()
                max_mem_mb = int(mem_row[0]) if mem_row and mem_row[0] is not None else None
            except Exception:
                pass
        finally:
            cursor.close()

        raw_ver = " ".join(str(prop_row[0] or "-").splitlines()).strip() if prop_row else "-"
        ver, lvl, edition, collation, hadr, clustered = (list(prop_row[1:7]) + ["-"] * 6)[:6]
        version_line = DEFAULT_REDACTION_POLICY.clip_text(raw_ver, 160)

        MSSQL_ADAPTER.audit.log(
            "mssql_server_status",
            "SERVERPROPERTY / dm_os_sys_info",
            target=target,
            status="OK",
            detail=f"degraded={degraded}",
        )

        lines = [
            "[MSSQL Server Status]",
            f"Version: {version_line}",
            f"ProductVersion / Level: {ver} / {lvl}",
            f"Edition: {edition}",
            f"Collation: {collation}",
            f"IsHadrEnabled / IsClustered: {hadr} / {clustered}",
        ]
        if max_mem_mb is not None:
            lines.append(f"max server memory (MB): {max_mem_mb}")
        if degraded:
            lines.append(t("mssql_mcp.status_dmv_degraded"))
        elif sys_info:
            start_t, cpu_cnt, phys_mb = (list(sys_info) + ["-"] * 3)[:3]
            lines.append(f"sqlserver_start_time: {start_t}")
            lines.append(f"CPU / Physical Memory (MB): {cpu_cnt} / {phys_mb}")
            if session_count is not None:
                lines.append(f"User Sessions: {session_count}")

        return truncate_text("\n".join(lines), max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "mssql_server_status",
        _run,
        sql_hint="SELECT @@VERSION / SERVERPROPERTY",
        mssql_host=mssql_host,
        mssql_port=mssql_port,
        mssql_user=mssql_user,
        mssql_password=mssql_password,
        mssql_database=mssql_database,
        error_key="mssql_mcp.status_failed",
    )


def mssql_inspect_active_requests(
    min_elapsed_ms: int = 0,
    blocking_only: bool = False,
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """唯讀查詢活動中 SQL Server 請求、等待類型與阻塞鏈 (blocking_session_id)，SQL 經遮罩並限長 200 字元"""
    if isinstance(min_elapsed_ms, bool) or not isinstance(min_elapsed_ms, int) or min_elapsed_ms < 0:
        return format_mssql_error(
            "mssql_inspect_active_requests",
            ValueError(t("mssql_mcp.requests_invalid_min_ms", value=repr(min_elapsed_ms))),
            sql=f"min_elapsed_ms={min_elapsed_ms!r}",
            target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database),
            status="BLOCKED",
        )

    where_clauses = ["r.session_id <> @@SPID", "r.total_elapsed_time >= %s"]
    if blocking_only:
        where_clauses.append(
            "(r.blocking_session_id > 0 OR EXISTS "
            "(SELECT 1 FROM sys.dm_exec_requests r2 WHERE r2.blocking_session_id = r.session_id))"
        )

    raw_sql = (
        f"SELECT TOP {int(CONSTANTS.mssql_active_requests_max_rows)} "
        "r.session_id, s.login_name, DB_NAME(r.database_id), r.status, r.command, "
        "r.blocking_session_id, r.wait_type, r.wait_time, r.total_elapsed_time, r.cpu_time, st.text "
        "FROM sys.dm_exec_requests r "
        "JOIN sys.dm_exec_sessions s ON r.session_id = s.session_id "
        "OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) st "
        f"WHERE {' AND '.join(where_clauses)} "
        "ORDER BY r.total_elapsed_time DESC;"
    )

    def _run(conn: Any, _target_db: str, target: str) -> str:
        sql = adapt_placeholders(raw_sql, conn)
        cursor = conn.cursor()
        try:
            try:
                cursor.execute(sql, (min_elapsed_ms,))
                rows = list(cursor.fetchall() or [])
            except Exception as exc:
                if is_permission_error(exc):
                    MSSQL_ADAPTER.audit.log(
                        "mssql_inspect_active_requests",
                        sql,
                        target=target,
                        status="BLOCKED",
                        detail="permission denied",
                    )
                    return f"[ERROR] {t('mssql_mcp.requests_permission_denied')}"
                raise
        finally:
            cursor.close()

        MSSQL_ADAPTER.audit.log(
            "mssql_inspect_active_requests",
            sql,
            target=target,
            status="OK",
            detail=f"rows={len(rows)}",
        )
        if not rows:
            return t("mssql_mcp.requests_empty")

        lines = [t("mssql_mcp.requests_header", count=len(rows))]
        for row in rows:
            sid, login, dbn, status, cmd, blk_id, wtype, wtime, elapsed, cpu, sql_txt = (list(row) + [None] * 11)[:11]
            blk_tag = f" BLOCKED_BY={blk_id}" if blk_id and int(blk_id) > 0 else ""
            snippet = ""
            if sql_txt is not None:
                single = " ".join(DEFAULT_REDACTION_POLICY.redact(str(sql_txt)).splitlines()).strip()
                snippet = DEFAULT_REDACTION_POLICY.clip_text(single, CONSTANTS.mssql_active_requests_info_max_chars)
            lines.append(
                f"- spid={sid} login={login} db={dbn or '-'} status={status} cmd={cmd}"
                f"{blk_tag} wait={wtype or '-'}({wtime or 0}ms) elapsed={elapsed}ms cpu={cpu}ms"
                + (f" sql={snippet!r}" if snippet else "")
            )
        return truncate_text("\n".join(lines), max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "mssql_inspect_active_requests",
        _run,
        sql_hint=raw_sql,
        mssql_host=mssql_host,
        mssql_port=mssql_port,
        mssql_user=mssql_user,
        mssql_password=mssql_password,
        mssql_database=mssql_database,
        error_key="mssql_mcp.requests_failed",
    )


def mssql_backup_and_ag_status(
    database: str = "",
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """唯讀檢查各線上資料庫最近 FULL／DIFF／LOG 備份時間及 Always On Availability Groups 同步狀態"""
    explicit_db = clean_param(database)
    if explicit_db:
        try:
            explicit_db = validate_identifier(explicit_db, "database")
        except ValueError as exc:
            return format_mssql_error(
                "mssql_backup_and_ag_status",
                exc,
                sql=f"database={database!r}",
                target=MSSQL_ADAPTER.format_audit_target(mssql_host, mssql_port, mssql_user, database),
                status="BLOCKED",
            )

    def _run(conn: Any, target_db: str, target: str) -> str:
        db_filter = explicit_db or target_db
        where_db = "AND d.name = %s " if db_filter else ""
        backup_sql = adapt_placeholders(
            "SELECT d.name, "
            "MAX(CASE WHEN bs.type = 'D' THEN bs.backup_finish_date END) AS last_full, "
            "MAX(CASE WHEN bs.type = 'I' THEN bs.backup_finish_date END) AS last_diff, "
            "MAX(CASE WHEN bs.type = 'L' THEN bs.backup_finish_date END) AS last_log, "
            "DATEDIFF(day, MAX(CASE WHEN bs.type = 'D' THEN bs.backup_finish_date END), GETDATE()) AS full_age_days "
            "FROM sys.databases d "
            "LEFT JOIN msdb.dbo.backupset bs ON bs.database_name = d.name "
            "WHERE d.state_desc = 'ONLINE' AND d.name <> 'tempdb' "
            f"{where_db}"
            "GROUP BY d.name ORDER BY d.name;",
            conn,
        )

        lines = [t("mssql_mcp.backup_header")]
        warnings: list[str] = []
        cursor = conn.cursor()
        try:
            try:
                if db_filter:
                    cursor.execute(backup_sql, (db_filter,))
                else:
                    cursor.execute(backup_sql)
                b_rows = list(cursor.fetchall() or [])
                if not b_rows:
                    lines.append(t("mssql_mcp.backup_none"))
                else:
                    for row in b_rows:
                        db_n, full_dt, diff_dt, log_dt, age_days = (list(row) + [None] * 5)[:5]
                        lines.append(f"- {db_n}: FULL={full_dt or 'NONE'}, DIFF={diff_dt or '-'}, LOG={log_dt or '-'}")
                        if full_dt is None:
                            warnings.append(f"⚠️ {t('mssql_mcp.backup_warn_never', db=db_n)}")
                        elif age_days is not None and int(age_days) > CONSTANTS.mssql_backup_warn_days:
                            warnings.append(f"⚠️ {t('mssql_mcp.backup_warn_stale', db=db_n, days=int(age_days))}")
            except Exception as b_exc:
                if is_permission_error(b_exc):
                    lines.append(t("mssql_mcp.backup_permission_denied"))
                else:
                    raise

            if warnings:
                lines.extend(["", *warnings])

            cursor.execute("SELECT SERVERPROPERTY('IsHadrEnabled');")
            hadr_row = cursor.fetchone()
            hadr_enabled = bool(hadr_row and hadr_row[0])
            lines.append("")
            if not hadr_enabled:
                lines.append(t("mssql_mcp.ag_disabled"))
            else:
                lines.append(t("mssql_mcp.ag_header"))
                ag_where = "WHERE DB_NAME(drs.database_id) = %s " if db_filter else ""
                ag_sql = adapt_placeholders(
                    "SELECT ag.name, DB_NAME(drs.database_id), drs.is_primary_replica, "
                    "drs.synchronization_state_desc, drs.synchronization_health_desc, "
                    "drs.log_send_queue_size, drs.redo_queue_size "
                    "FROM sys.dm_hadr_database_replica_states drs "
                    "JOIN sys.availability_groups ag ON drs.group_id = ag.group_id "
                    f"{ag_where}"
                    "ORDER BY ag.name, DB_NAME(drs.database_id);",
                    conn,
                )
                try:
                    if db_filter:
                        cursor.execute(ag_sql, (db_filter,))
                    else:
                        cursor.execute(ag_sql)
                    ag_rows = list(cursor.fetchall() or [])
                    for arow in ag_rows:
                        ag_n, db_n, is_pri, sync_st, health_st, send_q, redo_q = (list(arow) + [None] * 7)[:7]
                        role = "PRIMARY" if is_pri else "SECONDARY"
                        lines.append(
                            f"- {ag_n} / {db_n} ({role}): state={sync_st}, health={health_st}, "
                            f"send_queue={send_q or 0}KB, redo_queue={redo_q or 0}KB"
                        )
                except Exception as ag_exc:
                    if is_permission_error(ag_exc):
                        lines.append(t("mssql_mcp.ag_permission_denied"))
                    else:
                        raise
        finally:
            cursor.close()

        MSSQL_ADAPTER.audit.log("mssql_backup_and_ag_status", backup_sql, target=target, status="OK")
        return truncate_text("\n".join(lines), max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "mssql_backup_and_ag_status",
        _run,
        sql_hint="msdb.dbo.backupset / dm_hadr_database_replica_states",
        mssql_host=mssql_host,
        mssql_port=mssql_port,
        mssql_user=mssql_user,
        mssql_password=mssql_password,
        mssql_database=mssql_database,
        explicit_db=explicit_db,
        error_key="mssql_mcp.backup_ag_failed",
    )


def register(mcp: Any) -> None:
    """註冊 MSSQL 伺服器狀態、活動請求與備份／AG 診斷工具至 MCPServer。"""
    for fn in (mssql_server_status, mssql_inspect_active_requests, mssql_backup_and_ag_status):
        mcp.tool()(fn)

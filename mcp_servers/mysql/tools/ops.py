"""MySQL / MariaDB 伺服器狀態、活動工作階段與複寫唯讀診斷工具群組（Command / Registrar Pattern）。"""

from typing import Any

from mcp_servers._shared import CONSTANTS, DEFAULT_REDACTION_POLICY, format_bytes, t
from mcp_servers.mysql.adapter import MYSQL_ADAPTER
from mcp_servers.mysql.tools._common import format_mysql_error, run_readonly_tool, truncate_text

_STATUS_ALLOWLIST = (
    "Uptime",
    "Threads_connected",
    "Threads_running",
    "Slow_queries",
    "Questions",
    "Innodb_buffer_pool_reads",
    "Innodb_buffer_pool_read_requests",
)

_VARIABLES_ALLOWLIST = (
    "max_connections",
    "read_only",
    "super_read_only",
    "innodb_buffer_pool_size",
    "sql_mode",
    "character_set_server",
    "transaction_isolation",
)

_REPL_SAFE_FIELDS = (
    "Channel_Name",
    "Replica_IO_Running",
    "Slave_IO_Running",
    "Replica_SQL_Running",
    "Slave_SQL_Running",
    "Seconds_Behind_Source",
    "Seconds_Behind_Master",
    "Last_IO_Errno",
    "Last_IO_Error",
    "Last_SQL_Errno",
    "Last_SQL_Error",
    "Executed_Gtid_Set",
    "Retrieved_Gtid_Set",
    "Source_Log_File",
    "Master_Log_File",
    "Read_Source_Log_Pos",
    "Read_Master_Log_Pos",
    "Relay_Log_File",
    "Relay_Log_Pos",
    "Replica_SQL_Running_State",
    "Slave_SQL_Running_State",
)


def _format_uptime(seconds_raw: str | None) -> str:
    try:
        secs = max(0, int(seconds_raw or 0))
    except ValueError:
        return str(seconds_raw or "-")
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, sec = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {mins}m {sec}s"
    return f"{hours}h {mins}m {sec}s"


def mysql_server_status(
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """唯讀查詢遠端 MySQL/MariaDB 伺服器版本、Uptime、連線數水位、Buffer Pool 命中率與唯讀狀態"""

    def _run(conn: Any, _target_schema: str, target: str) -> str:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT VERSION();")
            ver_row = cursor.fetchone()
            version = str(ver_row[0]) if ver_row else "-"

            status_ph = ", ".join("%s" for _ in _STATUS_ALLOWLIST)
            cursor.execute(f"SHOW GLOBAL STATUS WHERE Variable_name IN ({status_ph});", _STATUS_ALLOWLIST)
            status_map = {str(r[0]): str(r[1]) for r in (cursor.fetchall() or []) if len(r) >= 2}

            vars_ph = ", ".join("%s" for _ in _VARIABLES_ALLOWLIST)
            cursor.execute(f"SHOW GLOBAL VARIABLES WHERE Variable_name IN ({vars_ph});", _VARIABLES_ALLOWLIST)
            vars_map = {str(r[0]): str(r[1]) for r in (cursor.fetchall() or []) if len(r) >= 2}
        finally:
            cursor.close()

        MYSQL_ADAPTER.audit.log("mysql_server_status", "SHOW GLOBAL STATUS/VARIABLES", target=target, status="OK")

        threads_conn = status_map.get("Threads_connected", "0")
        max_conn = vars_map.get("max_connections", "0")
        conn_ratio = ""
        try:
            if int(max_conn) > 0:
                conn_ratio = f" ({int(threads_conn) / int(max_conn) * 100:.1f}%)"
        except ValueError:
            pass

        bp_size = vars_map.get("innodb_buffer_pool_size")
        bp_size_str = format_bytes(int(bp_size)) if bp_size and bp_size.isdigit() else "-"
        hit_ratio_str = "-"
        try:
            reads = int(status_map.get("Innodb_buffer_pool_reads", "0"))
            req = int(status_map.get("Innodb_buffer_pool_read_requests", "0"))
            if req > 0:
                hit_ratio_str = f"{max(0.0, (1.0 - reads / req) * 100):.2f}%"
        except ValueError:
            pass

        lines = [
            "[MySQL Server Status]",
            f"Version: {version}",
            f"Uptime: {_format_uptime(status_map.get('Uptime'))}",
            f"read_only / super_read_only: {vars_map.get('read_only', '-')} / {vars_map.get('super_read_only', '-')}",
            f"Connections: {threads_conn} / {max_conn}{conn_ratio} (Running: {status_map.get('Threads_running', '-')})",
            f"Slow_queries: {status_map.get('Slow_queries', '-')}",
            f"Questions: {status_map.get('Questions', '-')}",
            f"InnoDB Buffer Pool: {bp_size_str} (Hit Ratio: {hit_ratio_str})",
            f"Charset / Isolation: {vars_map.get('character_set_server', '-')} / "
            f"{vars_map.get('transaction_isolation', '-')}",
            f"sql_mode: {vars_map.get('sql_mode', '-')}",
        ]
        return truncate_text("\n".join(lines), max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "mysql_server_status",
        _run,
        sql_hint="SHOW GLOBAL STATUS / VARIABLES",
        mysql_host=mysql_host,
        mysql_port=mysql_port,
        mysql_user=mysql_user,
        mysql_password=mysql_password,
        mysql_database=mysql_database,
        error_key="mysql_mcp.status_failed",
    )


def mysql_inspect_processlist(
    only_active: bool = True,
    min_time_sec: int = 0,
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """唯讀探查 INFORMATION_SCHEMA.PROCESSLIST 長時間查詢與鎖等待（INFO 經機密遮罩且限長 200 字元）"""
    if isinstance(min_time_sec, bool) or not isinstance(min_time_sec, int) or min_time_sec < 0:
        return format_mysql_error(
            "mysql_inspect_processlist",
            ValueError(t("mysql_mcp.processlist_invalid_min_time", value=repr(min_time_sec))),
            sql=f"min_time_sec={min_time_sec!r}",
            target=MYSQL_ADAPTER.format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database),
            status="BLOCKED",
        )

    where_clauses = ["ID != CONNECTION_ID()", "TIME >= %s"]
    if only_active:
        where_clauses.append("COMMAND != 'Sleep'")
    sql = (
        "SELECT ID, USER, HOST, DB, COMMAND, TIME, STATE, INFO "
        "FROM INFORMATION_SCHEMA.PROCESSLIST "
        f"WHERE {' AND '.join(where_clauses)} "
        f"ORDER BY TIME DESC LIMIT {int(CONSTANTS.mysql_processlist_max_rows)};"
    )

    def _run(conn: Any, _target_schema: str, target: str) -> str:
        cursor = conn.cursor()
        try:
            cursor.execute(sql, (min_time_sec,))
            rows = list(cursor.fetchall() or [])
        finally:
            cursor.close()

        MYSQL_ADAPTER.audit.log(
            "mysql_inspect_processlist",
            sql,
            target=target,
            status="OK",
            detail=f"rows={len(rows)}",
        )
        if not rows:
            return t("mysql_mcp.processlist_empty")

        lines = [t("mysql_mcp.processlist_header", count=len(rows))]
        for row in rows:
            pid, puser, phost, pdb, pcmd, ptime, pstate, pinfo = (list(row) + [None] * 8)[:8]
            snippet = ""
            if pinfo is not None:
                single = " ".join(DEFAULT_REDACTION_POLICY.redact(str(pinfo)).splitlines()).strip()
                snippet = DEFAULT_REDACTION_POLICY.clip_text(single, CONSTANTS.mysql_processlist_info_max_chars)
            lines.append(
                f"- id={pid} user={puser}@{phost} db={pdb or '-'} cmd={pcmd} "
                f"time={ptime}s state={pstate or '-'}" + (f" info={snippet!r}" if snippet else "")
            )
        return truncate_text("\n".join(lines), max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "mysql_inspect_processlist",
        _run,
        sql_hint=sql,
        mysql_host=mysql_host,
        mysql_port=mysql_port,
        mysql_user=mysql_user,
        mysql_password=mysql_password,
        mysql_database=mysql_database,
        error_key="mysql_mcp.processlist_failed",
    )


def _is_repl_permission_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "access denied" in msg or "1227" in msg or "replication client" in msg or "replica monitor" in msg


def mysql_replication_status(
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """唯讀查詢主從複寫狀態（僅採白名單健康欄位，嚴格排除來源帳號、密碼與主機憑證）"""

    def _run(conn: Any, _target_schema: str, target: str) -> str:
        cursor = conn.cursor()
        executed_sql = "SHOW REPLICA STATUS;"
        try:
            try:
                cursor.execute(executed_sql)
            except Exception as first_exc:
                if _is_repl_permission_error(first_exc):
                    MYSQL_ADAPTER.audit.log(
                        "mysql_replication_status",
                        executed_sql,
                        target=target,
                        status="BLOCKED",
                        detail="permission denied",
                    )
                    return f"[ERROR] {t('mysql_mcp.repl_permission_denied')}"
                executed_sql = "SHOW SLAVE STATUS;"
                try:
                    cursor.execute(executed_sql)
                except Exception as second_exc:
                    if _is_repl_permission_error(second_exc):
                        MYSQL_ADAPTER.audit.log(
                            "mysql_replication_status",
                            executed_sql,
                            target=target,
                            status="BLOCKED",
                            detail="permission denied",
                        )
                        return f"[ERROR] {t('mysql_mcp.repl_permission_denied')}"
                    raise
            rows = list(cursor.fetchall() or [])
            desc = list(cursor.description or [])
        finally:
            cursor.close()

        MYSQL_ADAPTER.audit.log(
            "mysql_replication_status",
            executed_sql,
            target=target,
            status="OK",
            detail=f"channels={len(rows)}",
        )
        if not rows:
            return t("mysql_mcp.repl_not_configured")

        col_names = [str(d[0]) for d in desc]
        lines = ["[MySQL Replication Status]"]
        for idx, row in enumerate(rows, start=1):
            row_map = {col_names[i]: row[i] for i in range(min(len(col_names), len(row)))}
            channel = row_map.get("Channel_Name") or f"channel-{idx}"
            lines.append(f"--- Channel: {channel} ---")
            for field in _REPL_SAFE_FIELDS:
                if field == "Channel_Name" or field not in row_map:
                    continue
                val = row_map[field]
                if val is None or str(val) == "":
                    continue
                safe_val = DEFAULT_REDACTION_POLICY.redact(str(val))
                lines.append(f"  {field}: {safe_val}")
        return truncate_text("\n".join(lines), max_chars=CONSTANTS.max_output_chars)

    return run_readonly_tool(
        "mysql_replication_status",
        _run,
        sql_hint="SHOW REPLICA STATUS",
        mysql_host=mysql_host,
        mysql_port=mysql_port,
        mysql_user=mysql_user,
        mysql_password=mysql_password,
        mysql_database=mysql_database,
        error_key="mysql_mcp.repl_failed",
    )


def register(mcp: Any) -> None:
    """註冊 MySQL 維運狀態、Processlist 與複寫狀態工具至 MCPServer。"""
    for fn in (mysql_server_status, mysql_inspect_processlist, mysql_replication_status):
        mcp.tool()(fn)

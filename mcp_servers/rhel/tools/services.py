"""RHEL 服務狀態、Journald 摘要與行程探查工具群組（Command / Registrar Pattern）。"""

import json
import re
import shlex
from typing import Any

from mcp_servers.rhel import security, ssh_client


def rhel_service_status(
    service_name: str = "",
    failed_only: bool = False,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """查詢 systemd 服務狀態；未指定服務或 failed_only 時回傳前 50 筆結構化單元狀態與總數。

    指定 service_name 時回傳結構化屬性；支援以逗號或空白分隔一次傳入多個服務名稱（最多 10 個，
    例如 'sshd,chronyd,tuned,auditd'），請優先合併呼叫以減少 SSH 連線次數。
    """
    if failed_only or not service_name.strip():
        state_flag = "--state=failed" if failed_only else "--type=service --state=running,failed"
        cmd = (
            f"systemctl list-units {state_flag} --no-pager --no-legend --plain "
            '| awk \'NR<=50 {u=$1; sub(/@[^.]+/, "@*", u); print u, $2, $3, $4} '
            'END {print "matching_units=" NR}\''
        )
        return ssh_client.run_remote(
            cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_service_status"
        )

    raw_units = [u for u in re.split(r"[\s,]+", service_name.strip()) if u]
    if len(raw_units) > 10:
        ssh_client.AUDIT.log(
            "rhel_service_status",
            f"service_name={service_name!r}",
            target=ssh_client.format_audit_target(ssh_host, ssh_port, ssh_user),
            status="BLOCKED",
            detail="Too many service names (max 10)",
        )
        return "[ERROR] 單次查詢的服務數量不得超過 10 個。"

    quoted_units: list[str] = []
    for unit_item in raw_units:
        try:
            safe_unit = security.validate_identifier(unit_item, "service_name")
        except ValueError as exc:
            ssh_client.AUDIT.log(
                "rhel_service_status",
                f"service_name={service_name!r}",
                target=ssh_client.format_audit_target(ssh_host, ssh_port, ssh_user),
                status="BLOCKED",
                detail=str(exc),
            )
            return security.redact_and_truncate(f"[ERROR] {exc}")
        quoted_units.append(shlex.quote(safe_unit))

    joined_units = " ".join(quoted_units)
    props = (
        "Id,LoadState,ActiveState,SubState,UnitFileState,TriggeredBy,"
        "MainPID,ExecMainCode,ExecMainStatus,Result,NRestarts,"
        "ActiveEnterTimestamp,InactiveEnterTimestamp"
    )
    cmd = f"systemctl show {joined_units} --no-pager --property={props}"
    return ssh_client.run_remote(
        cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_service_status"
    )


def rhel_query_journal(
    unit: str = "",
    priority: str = "err",
    since: str = "1 hour ago",
    grep: str = "",
    lines: int = 50,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """查詢有界 journald 等級計數；不回傳訊息原文。grep 暫不支援，lines 為樣本上限。"""
    if grep.strip():
        ssh_client.AUDIT.log(
            "rhel_query_journal",
            f"unit={unit!r} grep={grep!r}",
            target=ssh_client.format_audit_target(ssh_host, ssh_port, ssh_user),
            status="BLOCKED",
            detail="grep parameter rejected in safe summary mode",
        )
        return "[ERROR] 安全摘要模式不接受訊息關鍵字；請使用 unit、priority 與 since 篩選。"

    parts = ["journalctl", "--no-pager", "--output=json", "--output-fields=PRIORITY", "--quiet"]
    if unit.strip():
        try:
            safe_unit = security.validate_identifier(unit, "unit")
        except ValueError as exc:
            ssh_client.AUDIT.log(
                "rhel_query_journal",
                f"unit={unit!r}",
                target=ssh_client.format_audit_target(ssh_host, ssh_port, ssh_user),
                status="BLOCKED",
                detail=str(exc),
            )
            return security.redact_and_truncate(f"[ERROR] {exc}")
        parts.extend(["-u", shlex.quote(safe_unit)])

    prio_aliases = {"error": "err", "warn": "warning", "critical": "crit"}
    prio = prio_aliases.get(priority.strip().lower(), priority.strip().lower())
    if prio and prio not in ("all", "none"):
        if prio not in security.VALID_PRIORITIES:
            ssh_client.AUDIT.log(
                "rhel_query_journal",
                f"priority={priority!r}",
                target=ssh_client.format_audit_target(ssh_host, ssh_port, ssh_user),
                status="BLOCKED",
                detail="Invalid priority",
            )
            return f"[ERROR] 非法的 priority 參數（允許值：{sorted(security.VALID_PRIORITIES)}）"
        parts.extend(["-p", shlex.quote(prio)])

    since_clean = since.strip()
    if since_clean:
        try:
            since_clean = security.validate_since(since_clean)
        except ValueError:
            ssh_client.AUDIT.log(
                "rhel_query_journal",
                f"since={since!r}",
                target=ssh_client.format_audit_target(ssh_host, ssh_port, ssh_user),
                status="BLOCKED",
                detail="Invalid since format",
            )
            return "[ERROR] 非法的 since 時間格式（僅允許安全字元集且限 40 字元以內）"
        parts.extend(["--since", shlex.quote(since_clean)])

    safe_lines = max(1, min(int(lines or 50), 200))
    parts.extend(["-n", str(safe_lines)])
    result = ssh_client.run_remote(
        " ".join(parts), ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_query_journal"
    )
    if ssh_client.is_transport_or_auth_error(result):
        return result
    if result.startswith("[ERROR]"):
        return "[ERROR] 日誌摘要查詢失敗；可能缺少權限或不支援指定的 journalctl 選項。"

    counts = {str(level): 0 for level in range(8)}
    was_truncated = "\n... (輸出過長已截斷" in result
    clean_result = result.split("\n... (輸出過長已截斷", 1)[0] if was_truncated else result
    raw_lines = [] if clean_result == "(沒有輸出)" else clean_result.splitlines()
    if was_truncated and raw_lines and not raw_lines[-1].strip().endswith("}"):
        raw_lines = raw_lines[:-1]
    try:
        for line in raw_lines:
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                return "[ERROR] 日誌摘要格式不符，已捨棄。"
            level = record.get("PRIORITY")
            if isinstance(level, list) and level:
                level = level[0]
            if isinstance(level, int):
                level = str(level)
            if level is None and any(k in record for k in ("__CURSOR", "_BOOT_ID", "__REALTIME_TIMESTAMP")):
                continue
            if not isinstance(level, str) or level not in counts:
                return "[ERROR] 日誌摘要格式不符，已捨棄。"
            counts[level] += 1
    except ValueError, AttributeError:
        return "[ERROR] 日誌摘要格式不符，已捨棄。"
    return json.dumps({"sample_limit": safe_lines, "sample_count": sum(counts.values()), "priorities": counts})


def rhel_process_inspect(
    pid: str = "",
    search: str = "",
    sort_by: str = "cpu",
    show_selinux: bool = False,
    max_lines: int = 50,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """深度探查 RHEL 行程資訊：可指定 PID 檢視細節、以關鍵字搜尋行程、或依 CPU/Memory 排序（支援 SELinux 標籤）"""
    safe_lines = max(5, min(int(max_lines or 50), 100))

    if pid.strip():
        try:
            safe_pid = security.validate_pid(pid)
        except ValueError as exc:
            ssh_client.AUDIT.log(
                "rhel_process_inspect",
                f"pid={pid!r}",
                target=ssh_client.format_audit_target(ssh_host, ssh_port, ssh_user),
                status="BLOCKED",
                detail=str(exc),
            )
            return security.redact_and_truncate(f"[ERROR] {exc}")
        cmd = (
            f"echo '=== Process Detail (PID: {safe_pid}) ===' && "
            f"ps -p {safe_pid} -o pid,ppid,user,stat,comm 2>/dev/null || echo '行程不存在或已終止'; "
            "echo '=== Process Status Summary ===' && "
            f"(cat /proc/{safe_pid}/status 2>/dev/null | "
            "grep -E '^(Name|State|Tgid|Pid|PPid|Uid|Gid|Threads|VmRSS|VmSize)' || true); "
            "echo '=== Process SELinux Label ===' && "
            f"(ps -q {safe_pid} -o pid,user,label,comm 2>/dev/null || echo '無 SELinux 標籤資訊')"
        )
        return ssh_client.run_remote(
            cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_process_inspect"
        )

    cols = "pid,ppid,user,%cpu,%mem,stat,label,comm" if show_selinux else "pid,ppid,user,%cpu,%mem,stat,comm"

    if search.strip():
        try:
            safe_search = security.validate_search_pattern(search, "search")
        except ValueError as exc:
            ssh_client.AUDIT.log(
                "rhel_process_inspect",
                f"search={search!r}",
                target=ssh_client.format_audit_target(ssh_host, ssh_port, ssh_user),
                status="BLOCKED",
                detail=str(exc),
            )
            return security.redact_and_truncate(f"[ERROR] {exc}")
        quoted_search = shlex.quote(safe_search)
        cmd = (
            f"echo '=== Search Processes matching {quoted_search} ===' && "
            f"(ps -eo {cols} | grep -i -- {quoted_search} | grep -v grep | sed -n '1,{safe_lines}p' "
            "|| echo '查無符合條件的行程')"
        )
        return ssh_client.run_remote(
            cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_process_inspect"
        )

    sort_clean = sort_by.strip().lower()
    if sort_clean not in security.VALID_SORT_KEYS:
        ssh_client.AUDIT.log(
            "rhel_process_inspect",
            f"sort_by={sort_by!r}",
            target=ssh_client.format_audit_target(ssh_host, ssh_port, ssh_user),
            status="BLOCKED",
            detail="Invalid sort_by parameter",
        )
        return "[ERROR] 非法的 sort_by 參數（僅允許 'cpu'、'mem' 或 'memory'）。"

    sort_col = "-%mem" if sort_clean in ("mem", "memory") else "-%cpu"
    cmd = (
        f"echo '=== Top Processes (sorted by {sort_clean}) ===' && "
        f"ps -eo {cols} --sort={sort_col} | sed -n '1,{safe_lines}p'"
    )
    return ssh_client.run_remote(
        cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_process_inspect"
    )


def register(mcp: Any) -> None:
    """註冊 RHEL 服務、日誌與行程工具至 MCPServer。"""
    for fn in (rhel_service_status, rhel_query_journal, rhel_process_inspect):
        mcp.tool()(fn)

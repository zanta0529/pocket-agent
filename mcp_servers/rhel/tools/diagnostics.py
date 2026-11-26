"""RHEL 綜合健檢、核心異常水位與憑證到期巡檢工具群組（Command / Registrar Pattern）。"""

import shlex
from typing import Any

from mcp_servers._shared import CONSTANTS
from mcp_servers.rhel import security, ssh_client

_TAINT_FLAGS: tuple[tuple[int, str], ...] = (
    (0, "proprietary_module"),
    (1, "forced_module_load"),
    (2, "cpu_out_of_spec"),
    (3, "forced_module_unload"),
    (4, "machine_check_exception"),
    (5, "bad_page_table"),
    (6, "user_requested_taint"),
    (7, "kernel_die_oops"),
    (8, "acpi_table_overridden"),
    (9, "kernel_warning"),
    (10, "staging_driver"),
    (11, "firmware_workaround"),
    (12, "out_of_tree_module"),
    (13, "unsigned_module"),
    (14, "soft_lockup"),
    (15, "live_patched"),
    (16, "auxiliary_taint"),
    (17, "struct_randomization"),
    (18, "in_kernel_test"),
)


def decode_kernel_taint(value: int) -> list[str]:
    """將 Linux /proc/sys/kernel/tainted 位元遮罩解碼為可讀旗標清單。"""
    if value <= 0:
        return []
    flags = [name for bit, name in _TAINT_FLAGS if value & (1 << bit)]
    known_mask = sum(1 << bit for bit, _ in _TAINT_FLAGS)
    if value & ~known_mask:
        flags.append("other_taint_bits")
    return flags


def rhel_health_snapshot(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """單次 SSH 往返綜合健檢：彙整 OS/Kernel、記憶體/PSI、磁碟/Inode、失敗服務、資安與近 1 小時錯誤計數。"""
    cmd = (
        "echo '=== OS & Kernel ===' && "
        "(cat /etc/redhat-release 2>/dev/null | sed -n '1,3p' || true) && uname -srm && uptime; "
        "echo '=== Memory & Pressure ===' && free -m && "
        "(head -n 1 /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io 2>/dev/null "
        "|| echo 'PSI 未啟用'); "
        "echo '=== Storage & Inodes ===' && df -hT -x tmpfs -x devtmpfs && df -i -x tmpfs -x devtmpfs; "
        "echo '=== Failed Units ===' && "
        "(systemctl list-units --state=failed --no-pager --no-legend --plain 2>/dev/null "
        '| awk \'NR<=15 {u=$1; sub(/@[^.]+/, "@*", u); print u, $2, $3, $4} '
        'END {print "failed_units=" NR}\'); '
        "echo '=== Security Summary ===' && "
        "echo -n 'selinux=' && (getenforce 2>/dev/null || echo 'unknown') && "
        "echo -n 'firewalld=' && (firewall-cmd --state 2>/dev/null || echo 'inactive'); "
        "echo '=== Recent Error Journal Count (1h) ===' && "
        "(journalctl -p err --since '1 hour ago' --no-pager --output=json "
        "--output-fields=PRIORITY --quiet -n 200 2>/dev/null "
        '| awk \'BEGIN{c=0} /"PRIORITY"/ {c++} END {print "recent_err_sample_count=" c}\' '
        "|| echo 'recent_err_sample_count=unavailable')"
    )
    return ssh_client.run_remote(
        cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_health_snapshot"
    )


def rhel_kernel_health(
    since: str = "24 hours ago",
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """探查核心健康狀態：Kernel Taint 解碼、OOM Kill 計數、FD 開啟水位與核心警告級別計數（無原文）。"""
    since_clean = since.strip() or "24 hours ago"
    try:
        since_clean = security.validate_since(since_clean)
    except ValueError:
        return ssh_client.audit_blocked(
            "rhel_kernel_health",
            f"since={since!r}",
            "非法的 since 時間格式（僅允許安全字元集且限 40 字元以內）",
            ssh_host,
            ssh_port,
            ssh_user,
        )

    quoted_since = shlex.quote(since_clean)
    cmd = (
        "echo 'tainted='$(cat /proc/sys/kernel/tainted 2>/dev/null || echo 'unknown'); "
        "echo 'oom_kill='$(awk '$1==\"oom_kill\"{print $2}' /proc/vmstat 2>/dev/null || echo 'unknown'); "
        "echo 'file_nr='$(cat /proc/sys/fs/file-nr 2>/dev/null || echo 'unknown'); "
        "echo 'threads_max='$(cat /proc/sys/kernel/threads-max 2>/dev/null || echo 'unknown'); "
        "echo '=== Kernel Warning+ Journal Counts ===' && "
        f"(journalctl -k -p warning --since {quoted_since} --no-pager --output=json "
        "--output-fields=PRIORITY --quiet -n 200 2>/dev/null | "
        "awk 'BEGIN{for(i=0;i<=4;i++) c[i]=0} "
        '{for(i=0;i<=4;i++) if(index($0, "\\"PRIORITY\\":\\""i"\\"")>0 '
        '|| index($0, "\\"PRIORITY\\": "i)>0) {c[i]++; break}} '
        'END{for(i=0;i<=4;i++) printf "priority_%d=%d\\n", i, c[i]}\' '
        "|| echo 'kernel_journal=unavailable')"
    )
    result = ssh_client.run_remote(
        cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_kernel_health"
    )
    if ssh_client.is_transport_or_auth_error(result) or result.startswith("[ERROR]"):
        return result

    taint_detail = ""
    for line in result.splitlines():
        if line.startswith("tainted="):
            raw_val = line.split("=", 1)[1].strip()
            if raw_val.isdigit():
                taint_int = int(raw_val)
                flags = decode_kernel_taint(taint_int)
                label = ", ".join(flags) if flags else "clean"
                taint_detail = f"tainted_decoded={label}"
            break
    if taint_detail:
        return f"{result}\n{taint_detail}"
    return result


def rhel_cert_expiry(
    cert_dir: str = "/etc/pki/tls/certs",
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """巡檢白名單公開憑證目錄下之 X.509 憑證到期日（僅輸出 subject/issuer/enddate，嚴格排除私鑰）。"""
    try:
        safe_dir = security.validate_cert_dir(cert_dir)
    except ValueError as exc:
        return ssh_client.audit_blocked(
            "rhel_cert_expiry",
            f"cert_dir={cert_dir!r}",
            str(exc),
            ssh_host,
            ssh_port,
            ssh_user,
            redact=True,
        )

    max_files = CONSTANTS.rhel_max_cert_files
    cmd = (
        security.remote_cert_dir_guard(safe_dir) + f"echo '=== Certificate Expiry Summary ({safe_dir}) ===' && "
        "found=0; "
        f'for f in $(find "$real" -maxdepth 2 -type f '
        f"\\( -name '*.crt' -o -name '*.pem' -o -name '*.cer' \\) 2>/dev/null "
        f"| sort | sed -n '1,{max_files}p'); do "
        "  fl=$(printf '%s' \"$f\" | LC_ALL=C tr '[:upper:]' '[:lower:]'); "
        '  case "$fl" in *private*|*key*|*secret*) continue ;; esac; '
        "  if head -n 5 \"$f\" 2>/dev/null | grep -q 'PRIVATE KEY'; then continue; fi; "
        "  if head -n 5 \"$f\" 2>/dev/null | grep -q 'BEGIN CERTIFICATE'; then "
        "    found=$((found + 1)); "
        '    echo "--- $f ---"; '
        "    openssl x509 -noout -subject -issuer -enddate -in \"$f\" 2>/dev/null || echo '無法解析憑證'; "
        "  fi; "
        "done; "
        'echo "scanned_certs=$found"'
    )
    return ssh_client.run_remote(
        cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_cert_expiry"
    )


def register(mcp: Any) -> None:
    """註冊 RHEL 綜合健檢、核心異常與憑證巡檢工具至 MCPServer。"""
    for fn in (rhel_health_snapshot, rhel_kernel_health, rhel_cert_expiry):
        mcp.tool()(fn)

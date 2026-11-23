"""RHEL 系統、儲存、套件、排程與硬體巡檢工具群組（Command / Registrar Pattern）。"""

import shlex
from typing import Any

from mcp_servers.rhel import security, ssh_client


def rhel_system_overview(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """探查 RHEL 主機系統概況：OS 版本、Kernel、Uptime、時間同步、PSI 壓力、記憶體與高負載行程"""
    cmd = (
        "echo '=== OS Release ===' && cat /etc/redhat-release /etc/os-release 2>/dev/null | sed -n '1,15p'; "
        "echo '=== Kernel & Uptime ===' && uname -srm && uptime && "
        "(rpm -q kernel --last 2>/dev/null | sed -n '1,5p' || true) && "
        "(grubby --default-kernel 2>/dev/null || true) && "
        "(timedatectl show --property=NTPSynchronized,NTP,Timezone 2>/dev/null || true) && "
        "(chronyc tracking 2>/dev/null || true) && "
        "(tuned-adm active 2>/dev/null || true); "
        "echo '=== CPU / IO Wait & Pressure (vmstat / PSI) ===' && "
        "(vmstat -w 2>/dev/null || true) && "
        "(head -n 2 /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io 2>/dev/null "
        "|| echo 'PSI 未啟用 (預設需核心參數 psi=1)'); "
        "echo '=== Memory (MB) ===' && free -m; "
        "echo '=== Top Processes (by CPU) ===' && "
        "ps -eo pid,ppid,user,%cpu,%mem,stat,comm --sort=-%cpu | sed -n '1,11p'"
    )
    return ssh_client.run_remote(
        cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_system_overview"
    )


def rhel_storage_and_fs(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """探查 RHEL 儲存與檔案系統：掛載空間 (df -hT)、Inode、區塊裝置與掛載選項 (findmnt)"""
    cmd = (
        "echo '=== Filesystem Usage ===' && df -hT -x tmpfs -x devtmpfs; "
        "echo '=== Inode Usage ===' && df -i -x tmpfs -x devtmpfs; "
        "echo '=== Block Devices ===' && lsblk -o NAME,FSTYPE,SIZE,MOUNTPOINT,UUID; "
        "echo '=== Mount Options ===' && "
        "(findmnt -no TARGET,SOURCE,FSTYPE,OPTIONS --list -t nodevtmpfs,nosquashfs 2>/dev/null "
        "| grep -E '^(/|/boot|/home|/tmp|/var|/dev/shm|/opt|/srv)([[:space:]]|/|$)' || true)"
    )
    return ssh_client.run_remote(
        cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_storage_and_fs"
    )


def rhel_package_info(
    package_name: str,
    verify_integrity: bool = False,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """查詢已安裝之 RHEL RPM 套件資訊、Changelog 摘要與完整性驗證 (rpm -qi, rpm -V)"""
    try:
        safe_pkg = security.validate_identifier(package_name, "package_name")
    except ValueError as exc:
        ssh_client.AUDIT.log(
            "rhel_package_info",
            f"package_name={package_name!r}",
            target=ssh_client.format_audit_target(ssh_host, ssh_port, ssh_user),
            status="BLOCKED",
            detail=str(exc),
        )
        return security.redact_and_truncate(f"[ERROR] {exc}")

    quoted = shlex.quote(safe_pkg)
    if verify_integrity:
        cmd = (
            f"rpm -q {quoted} >/dev/null 2>&1 || "
            "{ echo '[ERROR] 套件未安裝或資料庫查詢失敗'; exit 1; }; "
            f"if rpm -V --noscripts {quoted} >/dev/null 2>&1; then rc=0; else rc=$?; fi; "
            "case $rc in 0) echo 'verification=unchanged' ;; "
            "1) echo 'verification=differences_or_verification_error' ;; "
            "*) echo '[ERROR] RPM 驗證失敗'; exit 1 ;; esac"
        )
    else:
        cmd = (
            f"echo '=== Package Info (rpm -qi) ===' && "
            f"(rpm -qi {quoted} 2>/dev/null || "
            "echo '[ERROR] 查無套件，不自動透過 DNF 查詢中繼資料'); "
            "echo '=== Changelog Summary (recent) ===' && "
            f"rpm -q --changelog {quoted} 2>/dev/null | sed -n '1,20p'"
        )
    return ssh_client.run_remote(
        cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_package_info"
    )


def rhel_container_status(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """探查 RHEL 容器執行環境；在未證明無副作用前暫停提供。"""
    ssh_client.AUDIT.log(
        "rhel_container_status",
        "container_status",
        target=ssh_client.format_audit_target(ssh_host, ssh_port, ssh_user),
        status="BLOCKED",
        detail="Container status disabled in safe summary mode",
    )
    return "[ERROR] [UNSUPPORTED] 安全摘要模式下暫停提供容器狀態探查（尚未證明命令無副作用）。"


def rhel_cron_and_timers(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """查詢 systemd timers 排程清單（含上次與下次觸發時間、對應服務與啟用狀態）及系統 crontab 項目計數。"""
    cmd = (
        "echo '=== Systemd Timers Schedule (NEXT | LEFT | LAST | PASSED | UNIT | ACTIVATES) ===' && "
        "systemctl list-timers --all --no-pager --no-legend "
        '| awk \'NR <= 100 {gsub(/@[^.[:space:]]+/, "@*"); print} END {print "loaded_timers=" NR}\'; '
        "echo '=== Systemd Timer Unit Files (UNIT FILE | STATE | VENDOR PRESET) ===' && "
        "systemctl list-unit-files --type=timer --no-pager --no-legend "
        '| awk \'NR <= 100 {gsub(/@[^.[:space:]]+/, "@*"); print} END {print "timer_unit_files=" NR}\'; '
        "echo '=== System Crontab Entry Count ===' && "
        "awk '/^[[:space:]]*($|#)/ {next} "
        "/^[[:space:]]*[A-Za-z_][A-Za-z_0-9]*[[:space:]]*=/ {next} "
        '{n++} END {print "system_crontab_entries=" n+0}\' /etc/crontab'
    )
    return ssh_client.run_remote(
        cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_cron_and_timers"
    )


def rhel_hardware_info(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """探查 RHEL 主機硬體規格與虛擬化環境：CPU 架構、記憶體規格與虛擬化平台偵測"""
    cmd = (
        "echo '=== Virtualization Platform ===' && "
        "(systemd-detect-virt || echo '實體機或未偵測到虛擬化層'); "
        "echo '=== CPU Architecture (lscpu) ===' && "
        "(lscpu 2>/dev/null | grep -E '^(Architecture|CPU\\(s\\)|Model name|Thread|Core|Socket|Virtualization|NUMA)' "
        "|| uname -m); "
        "echo '=== Memory & Swap Details ===' && "
        "(free -h && grep -E '^(MemTotal|MemFree|MemAvailable|SwapTotal|SwapFree)' /proc/meminfo)"
    )
    return ssh_client.run_remote(
        cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_hardware_info"
    )


def register(mcp: Any) -> None:
    """註冊 RHEL 系統與硬體巡檢工具至 MCPServer。"""
    for fn in (
        rhel_system_overview,
        rhel_storage_and_fs,
        rhel_package_info,
        rhel_container_status,
        rhel_cron_and_timers,
        rhel_hardware_info,
    ):
        mcp.tool()(fn)

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
    """探查 RHEL 儲存與檔案系統：掛載空間、Inode、區塊裝置、LVM／Thin Pool、RAID 與掛載選項"""
    cmd = (
        "echo '=== Filesystem Usage ===' && df -hT -x tmpfs -x devtmpfs; "
        "echo '=== Inode Usage ===' && df -i -x tmpfs -x devtmpfs; "
        "echo '=== Block Devices ===' && lsblk -e 7 -o NAME,TYPE,FSTYPE,SIZE,ROTA,MOUNTPOINT,UUID; "
        "echo '=== LVM Volume Groups & Logical Volumes ===' && "
        "(vgs --noheadings -o vg_name,vg_size,vg_free 2>/dev/null || echo '無 LVM VG 或權限不足'); "
        "(lvs --noheadings -o lv_name,vg_name,lv_attr,lv_size,data_percent,metadata_percent 2>/dev/null || true); "
        "echo '=== Software RAID (/proc/mdstat) ===' && "
        "([ -r /proc/mdstat ] && cat /proc/mdstat || echo '無 /proc/mdstat'); "
        "echo '=== Mount Options ===' && "
        "(findmnt -no TARGET,SOURCE,FSTYPE,OPTIONS --list -t nodevtmpfs,nosquashfs 2>/dev/null "
        "| grep -E '^(/|/boot|/home|/tmp|/var|/dev/shm|/opt|/srv)([[:space:]]|/|$)' || true)"
    )
    return ssh_client.run_remote(
        cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_storage_and_fs"
    )


def rhel_package_info(
    package_name: str = "",
    verify_integrity: bool = False,
    recent_count: int = 0,
    search: str = "",
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """查詢本機 RPM 資料庫：單一套件資訊與完整性驗證、近期異動套件或關鍵字搜尋（零 DNF 副作用）"""
    if package_name.strip():
        try:
            safe_pkg = security.validate_identifier(package_name, "package_name")
        except ValueError as exc:
            return ssh_client.audit_blocked(
                "rhel_package_info",
                f"package_name={package_name!r}",
                str(exc),
                ssh_host,
                ssh_port,
                ssh_user,
                redact=True,
            )

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

    if recent_count and int(recent_count) > 0:
        safe_n = max(1, min(int(recent_count), 50))
        cmd = (
            f"echo '=== Recently Installed/Updated Packages (top {safe_n}) ===' && "
            f"rpm -qa --last | sed -n '1,{safe_n}p'"
        )
        return ssh_client.run_remote(
            cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_package_info"
        )

    if search.strip():
        try:
            safe_search = security.validate_identifier(search, "search")
        except ValueError as exc:
            return ssh_client.audit_blocked(
                "rhel_package_info",
                f"search={search!r}",
                str(exc),
                ssh_host,
                ssh_port,
                ssh_user,
                redact=True,
            )
        quoted_search = shlex.quote(safe_search)
        cmd = (
            f"echo '=== Installed Packages matching {quoted_search} ===' && "
            "rpm -qa --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\\n' | "
            f"awk -v q={quoted_search} -v lim=50 "
            "'index(tolower($0), tolower(q)) > 0 {if (n < lim) print; n++} "
            'END {print "matching_packages=" n+0}\''
        )
        return ssh_client.run_remote(
            cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_package_info"
        )

    return ssh_client.audit_blocked(
        "rhel_package_info",
        "empty_query",
        "請至少指定 package_name、recent_count（>0）或 search 其中一項查詢條件。",
        ssh_host,
        ssh_port,
        ssh_user,
    )


def rhel_container_status(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """安全唯讀探查容器環境：systemd 服務與單元狀態，以及已啟動 daemon/socket 之最小容器清單。"""
    cmd = (
        "echo '=== Container Runtime Services ===' && "
        "(systemctl show podman.socket podman.service docker.service containerd.service "
        "--no-pager --property=Id,LoadState,ActiveState,SubState 2>/dev/null || true); "
        "echo '=== Active Container Systemd Units ===' && "
        "(systemctl list-units 'libpod-*' 'docker-*' --no-pager --no-legend --plain 2>/dev/null "
        '| awk \'NR<=50 {u=$1; sub(/@[^.]+/, "@*", u); print u, $2, $3, $4} '
        'END {print "container_units=" NR}\'); '
        "echo '=== Docker Containers (only if active) ===' && "
        "(if systemctl is-active --quiet docker.service 2>/dev/null && command -v docker >/dev/null 2>&1; then "
        "docker ps -a --format 'table {{.ID}}\\t{{.Image}}\\t{{.State}}\\t{{.Status}}\\t{{.Ports}}' "
        "2>/dev/null | sed -n '1,51p' || echo '無法查詢 docker（可能權限不足）'; "
        "else echo 'docker.service 未啟用'; fi); "
        "echo '=== Podman Containers (only if system socket active) ===' && "
        "(if [ -S /run/podman/podman.sock ] && systemctl is-active --quiet podman.socket 2>/dev/null "
        "&& command -v podman >/dev/null 2>&1; then "
        "podman --url unix:///run/podman/podman.sock ps -a "
        "--format 'table {{.ID}}\\t{{.Image}}\\t{{.State}}\\t{{.Status}}\\t{{.Ports}}' "
        "2>/dev/null | sed -n '1,51p' || echo '無法查詢 podman socket（可能權限不足）'; "
        "else echo 'podman.socket 未啟用（不自動啟動 rootless storage）'; fi)"
    )
    return ssh_client.run_remote(
        cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_container_status"
    )


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
        "([ -r /etc/crontab ] && awk '/^[[:space:]]*($|#)/ {next} "
        "/^[[:space:]]*[A-Za-z_][A-Za-z_0-9]*[[:space:]]*=/ {next} "
        '{n++} END {print "system_crontab_entries=" n+0}\' /etc/crontab '
        "|| echo 'system_crontab_entries=0')"
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

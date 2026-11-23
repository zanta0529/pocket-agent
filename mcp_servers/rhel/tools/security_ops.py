"""RHEL 資安狀態、SELinux 與受限設定檔探查工具群組（Command / Registrar Pattern）。"""

import shlex
from typing import Any

from mcp_servers.rhel import security, ssh_client


def rhel_security_status(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """探查 RHEL 安全性狀態：SELinux 模式與 firewalld 預設／作用中區域、執行期與永久規則摘要"""
    cmd = (
        "echo '=== SELinux Status ===' && (sestatus || getenforce) 2>/dev/null; "
        "echo '=== Recent SELinux AVC Denials ==='; "
        "echo '[提示] 不回傳 AVC 原文；請使用 rhel_query_journal 取得有界等級摘要'; "
        "echo '=== Firewalld State (Runtime) ===' && "
        "(firewall-cmd --state && firewall-cmd --get-default-zone && "
        "firewall-cmd --get-active-zones && firewall-cmd --list-all) 2>/dev/null "
        "|| echo 'firewalld 未執行或需要權限'; "
        "echo '=== Firewalld State (Permanent) ===' && "
        "(firewall-cmd --permanent --list-all 2>/dev/null || echo '無法讀取 permanent 規則'); "
        "echo '=== Security Updates (dnf) ===' && "
        "echo '[UNSUPPORTED] 安全更新查詢已停用（DNF 快取存取存在潛在副作用）'"
    )
    return ssh_client.run_remote(
        cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_security_status"
    )


def rhel_read_config(
    file_path: str,
    max_lines: int = 120,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """僅提供 /etc/selinux/config 的 SELINUX 列舉值；不提供任意設定檔原文。"""
    clean_path = file_path.strip()
    if clean_path != "/etc/selinux/config":
        ssh_client.AUDIT.log(
            "rhel_read_config",
            f"file_path={file_path!r}",
            target=ssh_client.format_audit_target(ssh_host, ssh_port, ssh_user),
            status="BLOCKED",
            detail="Only /etc/selinux/config is permitted",
        )
        return "[ERROR] 安全摘要模式僅支援 /etc/selinux/config 的 SELINUX 欄位，不讀取其他設定檔。"
    cmd = (
        security.remote_path_guard(clean_path, directory=False) + "awk -F= -- '/^[[:space:]]*SELINUX[[:space:]]*=/ {"
        'v=$2; gsub(/[[:space:]]/,"",v); '
        'if (v == "enforcing" || v == "permissive" || v == "disabled") print "SELINUX=" v'
        '}\' "$real"'
    )
    result = ssh_client.run_remote(
        cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_read_config"
    )
    if ssh_client.is_transport_or_auth_error(result):
        return result
    if result not in {"SELINUX=enforcing", "SELINUX=permissive", "SELINUX=disabled"}:
        return "[ERROR] 無法取得唯一且有效的 SELINUX 設定，已捨棄內容。"
    return result


def rhel_list_directory(
    dir_path: str,
    show_selinux: bool = False,
    max_lines: int = 100,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """目錄列舉暫停提供；保留介面，但不讀取或回傳檔名及符號連結目標。"""
    ssh_client.AUDIT.log(
        "rhel_list_directory",
        f"dir_path={dir_path!r}",
        target=ssh_client.format_audit_target(ssh_host, ssh_port, ssh_user),
        status="BLOCKED",
        detail="Directory listing disabled in safe summary mode",
    )
    return "[ERROR] 安全摘要模式不提供目錄列舉；請使用特定的系統診斷工具。"


def rhel_selinux_context(
    target_path: str,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """查詢指定檔案或目錄的目前 SELinux 安全標籤 (ls -dZ) 與政策預期標籤 (matchpathcon)"""
    try:
        safe_path = security.validate_config_path(target_path)
    except ValueError as exc:
        ssh_client.AUDIT.log(
            "rhel_selinux_context",
            f"target_path={target_path!r}",
            target=ssh_client.format_audit_target(ssh_host, ssh_port, ssh_user),
            status="BLOCKED",
            detail=str(exc),
        )
        return security.redact_and_truncate(f"[ERROR] {exc}")

    quoted_path = shlex.quote(safe_path)
    cmd = (
        f"echo '=== Current SELinux Context (ls -dZ) ===' && ls -dZ -- {quoted_path} 2>&1; "
        "echo '=== Expected Context (matchpathcon) ===' && "
        f"(matchpathcon {quoted_path} 2>/dev/null || echo '無 matchpathcon 紀錄或工具未安裝')"
    )
    return ssh_client.run_remote(
        cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_selinux_context"
    )


def register(mcp: Any) -> None:
    """註冊 RHEL 資安與 SELinux 工具至 MCPServer。"""
    for fn in (rhel_security_status, rhel_read_config, rhel_list_directory, rhel_selinux_context):
        mcp.tool()(fn)

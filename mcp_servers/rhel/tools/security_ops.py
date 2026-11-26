"""RHEL 資安狀態、SELinux 與受限設定檔探查工具群組（Command / Registrar Pattern）。"""

from typing import Any

from mcp_servers.rhel import security, ssh_client
from mcp_servers.rhel.config_parsers import SUPPORTED_CONFIG_PATHS, get_config_strategy


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
    """以白名單結構化策略唯讀摘要指定系統設定檔（絕不回傳任意檔案原文）。"""
    clean_path = file_path.strip()
    strategy = get_config_strategy(clean_path)
    if strategy is None:
        allowed = "、".join(SUPPORTED_CONFIG_PATHS)
        return ssh_client.audit_blocked(
            "rhel_read_config",
            f"file_path={file_path!r}",
            f"安全摘要模式僅支援白名單設定檔（{allowed}），不讀取其他設定檔。",
            ssh_host,
            ssh_port,
            ssh_user,
        )
    safe_lines = max(1, min(int(max_lines or 120), 120))
    return strategy.inspect(safe_lines, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)


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
    return ssh_client.audit_blocked(
        "rhel_list_directory",
        f"dir_path={dir_path!r}",
        "安全摘要模式不提供目錄列舉；請使用特定的系統診斷工具。",
        ssh_host,
        ssh_port,
        ssh_user,
    )


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
        return ssh_client.audit_blocked(
            "rhel_selinux_context",
            f"target_path={target_path!r}",
            str(exc),
            ssh_host,
            ssh_port,
            ssh_user,
            redact=True,
        )

    cmd = (
        security.remote_path_guard(safe_path, directory=None) + "echo '=== Current SELinux Context (ls -dZ) ===' && "
        "(ls -dZ -- \"$real\" 2>/dev/null || echo '無法讀取 SELinux 標籤'); "
        "echo '=== Expected Context (matchpathcon) ===' && "
        "(matchpathcon -- \"$real\" 2>/dev/null || echo '無 matchpathcon 紀錄或工具未安裝')"
    )
    return ssh_client.run_remote(
        cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_selinux_context"
    )


def register(mcp: Any) -> None:
    """註冊 RHEL 資安與 SELinux 工具至 MCPServer。"""
    for fn in (rhel_security_status, rhel_read_config, rhel_selinux_context):
        mcp.tool()(fn)

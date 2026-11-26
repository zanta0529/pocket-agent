"""RHEL 網路介面與連線狀態探查工具群組（Command / Registrar Pattern）。"""

import shlex
from typing import Any

from mcp_servers.rhel import security, ssh_client


def rhel_network_status(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """探查 RHEL 網路狀態：網路介面、RX/TX 錯誤統計、IPv4/IPv6 路由表、DNS 與監聽連接埠"""
    cmd = (
        "echo '=== Network Interfaces ===' && ip -br addr; "
        "echo '=== Interface Link Statistics (RX/TX) ===' && (ip -s -br link 2>/dev/null || true); "
        "echo '=== Routing Table ===' && ip route; "
        "echo '=== IPv6 Routing Table ===' && (ip -6 route 2>/dev/null || echo '無 IPv6 路由'); "
        "echo '=== DNS Resolver ===' && cat /etc/resolv.conf 2>/dev/null; "
        "echo '=== Listening Ports ===' && ss -tulnp"
    )
    return ssh_client.run_remote(
        cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_network_status"
    )


def rhel_network_connections(
    protocol: str = "all",
    state: str = "all",
    port: str = "",
    show_process: bool = True,
    max_lines: int = 60,
    summary_only: bool = False,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """利用 ss 探查網路連線（支援 summary_only 統計摘要或依協議／狀態／Port 過濾明細）"""
    if summary_only:
        return ssh_client.run_remote(
            "echo '=== Socket Statistics Summary (ss -s) ===' && ss -s",
            ssh_host,
            ssh_port,
            ssh_user,
            ssh_password,
            ssh_key_path,
            tool_name="rhel_network_connections",
        )

    proto_clean = protocol.strip().lower()
    if proto_clean not in {"all", "tcp", "udp"}:
        return ssh_client.audit_blocked(
            "rhel_network_connections",
            f"protocol={protocol!r}",
            "非法的 protocol 參數（僅允許 all、tcp、udp）。",
            ssh_host,
            ssh_port,
            ssh_user,
        )
    proto_flag = "-t" if proto_clean == "tcp" else "-u" if proto_clean == "udp" else "-tu"
    proc_flag = " -p" if show_process else ""

    state_clean = state.strip().lower()
    valid_states = {"all", "listening", "established", "time-wait", "close-wait", "syn-sent", "syn-recv"}
    if state_clean not in valid_states:
        return ssh_client.audit_blocked(
            "rhel_network_connections",
            f"state={state!r}",
            f"非法的 state 參數（允許值：{sorted(valid_states)}）",
            ssh_host,
            ssh_port,
            ssh_user,
        )

    if state_clean == "listening":
        base_cmd = f"ss {proto_flag}ln{proc_flag}"
    elif state_clean == "all":
        base_cmd = f"ss {proto_flag}an{proc_flag}"
    else:
        base_cmd = f"ss {proto_flag}an{proc_flag} state {shlex.quote(state_clean)}"

    filter_expr = ""
    if port.strip():
        try:
            safe_port = security.validate_port(port)
        except ValueError as exc:
            return ssh_client.audit_blocked(
                "rhel_network_connections",
                f"port={port!r}",
                str(exc),
                ssh_host,
                ssh_port,
                ssh_user,
                redact=True,
            )
        filter_expr = f" \\( sport = :{safe_port} or dport = :{safe_port} \\)"

    safe_lines = max(5, min(int(max_lines or 60), 150))
    cmd = f"{base_cmd}{filter_expr} | sed -n '1,{safe_lines}p'"
    return ssh_client.run_remote(
        cmd,
        ssh_host,
        ssh_port,
        ssh_user,
        ssh_password,
        ssh_key_path,
        tool_name="rhel_network_connections",
    )


def register(mcp: Any) -> None:
    """註冊 RHEL 網路診斷工具至 MCPServer。"""
    for fn in (rhel_network_status, rhel_network_connections):
        mcp.tool()(fn)

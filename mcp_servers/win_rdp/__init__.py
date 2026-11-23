"""遠端 Windows 主機 RDP 視覺控制與結構化唯讀巡檢 MCP 子套件。"""

from mcp_servers.win_rdp.server import mcp
from mcp_servers.win_rdp.tools import (
    win_network_status,
    win_query_eventlog,
    win_rdp_disconnect,
    win_rdp_keyboard_action,
    win_rdp_mouse_action,
    win_rdp_screenshot,
    win_read_config,
    win_security_status,
    win_service_status,
    win_storage_and_fs,
    win_system_overview,
)

__all__ = [
    "mcp",
    "win_network_status",
    "win_query_eventlog",
    "win_rdp_disconnect",
    "win_rdp_keyboard_action",
    "win_rdp_mouse_action",
    "win_rdp_screenshot",
    "win_read_config",
    "win_security_status",
    "win_service_status",
    "win_storage_and_fs",
    "win_system_overview",
]

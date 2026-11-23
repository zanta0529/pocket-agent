"""Windows RDP MCP 工具群組套件。"""

from mcp_servers.win_rdp.tools.gui import (
    win_rdp_disconnect,
    win_rdp_keyboard_action,
    win_rdp_mouse_action,
    win_rdp_screenshot,
)
from mcp_servers.win_rdp.tools.inspect import (
    win_network_status,
    win_query_eventlog,
    win_read_config,
    win_security_status,
    win_service_status,
    win_storage_and_fs,
    win_system_overview,
)

__all__ = [
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

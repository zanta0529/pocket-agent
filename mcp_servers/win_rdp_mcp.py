"""遠端 Windows 主機 RDP 視覺控制與結構化唯讀巡檢 MCP 伺服器薄入口。"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_servers._shared import CONSTANTS  # noqa: E402
from mcp_servers.win_rdp import (  # noqa: E402
    mcp,
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

MAX_OUTPUT_CHARS = CONSTANTS.max_output_chars

__all__ = [
    "MAX_OUTPUT_CHARS",
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

if __name__ == "__main__":
    mcp.run()

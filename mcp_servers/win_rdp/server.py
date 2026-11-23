"""Windows RDP MCP 伺服器組裝入口（Factory Pattern）。"""

from mcp_servers._shared import create_mcp_server
from mcp_servers.win_rdp.tools import gui as gui_tools
from mcp_servers.win_rdp.tools import inspect as inspect_tools

mcp = create_mcp_server(
    "win-rdp-ops",
    [
        gui_tools.register,
        inspect_tools.register,
    ],
)

"""NetDiag MCP 伺服器組裝入口（Factory Pattern）。"""

from mcp_servers._shared import create_mcp_server
from mcp_servers.netdiag.tools import diagnose as diagnose_tools
from mcp_servers.netdiag.tools import dns as dns_tools

mcp = create_mcp_server(
    "netdiag",
    [
        diagnose_tools.register,
        dns_tools.register,
    ],
)

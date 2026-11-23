"""MySQL MCP 伺服器組裝入口（Factory Pattern）。"""

from mcp_servers._shared import create_mcp_server
from mcp_servers.mysql.tools import health as health_tools
from mcp_servers.mysql.tools import query as query_tools

mcp = create_mcp_server(
    "mysql",
    [
        query_tools.register,
        health_tools.register,
    ],
)

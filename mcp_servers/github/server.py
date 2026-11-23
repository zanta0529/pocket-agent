"""GitHub MCP 伺服器組裝入口（Factory Pattern）。"""

from mcp_servers._shared import create_mcp_server
from mcp_servers.github.client import server_lifespan
from mcp_servers.github.tools import issues as issue_tools
from mcp_servers.github.tools import pull_requests as pr_tools

mcp = create_mcp_server(
    "github",
    [
        pr_tools.register,
        issue_tools.register,
    ],
    lifespan=server_lifespan,
)

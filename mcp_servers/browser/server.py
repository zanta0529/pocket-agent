"""Browser MCP 伺服器組裝入口（Factory Pattern）。"""

from mcp_servers._shared import create_mcp_server
from mcp_servers.browser.session import server_lifespan
from mcp_servers.browser.tools import REGISTRARS

mcp = create_mcp_server("browser", REGISTRARS, lifespan=server_lifespan)

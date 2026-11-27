"""SQLite MCP 伺服器組裝入口（Factory Pattern）。"""

from mcp_servers._shared import create_mcp_server
from mcp_servers.sqlite.tools import REGISTRARS

mcp = create_mcp_server("sqlite", REGISTRARS)

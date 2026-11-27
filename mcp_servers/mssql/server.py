"""MSSQL MCP 伺服器組裝入口（Factory Pattern）。"""

from mcp_servers._shared import create_mcp_server
from mcp_servers.mssql.tools import REGISTRARS

mcp = create_mcp_server("mssql", REGISTRARS)

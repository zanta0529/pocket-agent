"""RHEL MCP 伺服器組裝入口（Factory Pattern）。"""

from mcp_servers._shared import create_mcp_server
from mcp_servers.rhel.tools import network as network_tools
from mcp_servers.rhel.tools import security_ops as security_tools
from mcp_servers.rhel.tools import services as service_tools
from mcp_servers.rhel.tools import system as system_tools

mcp = create_mcp_server(
    "rhel-ops",
    [
        system_tools.register,
        network_tools.register,
        service_tools.register,
        security_tools.register,
    ],
)

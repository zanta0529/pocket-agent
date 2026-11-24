"""NetDiag MCP 工具群組公開介面。"""

from mcp_servers.netdiag.tools.diagnose import netdiag_diagnose
from mcp_servers.netdiag.tools.dns import netdiag_dns_compare

__all__ = [
    "netdiag_diagnose",
    "netdiag_dns_compare",
]

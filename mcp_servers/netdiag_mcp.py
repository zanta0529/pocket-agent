"""網站與網路跨單位舉證診斷 MCP 伺服器薄入口。"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_servers._shared import CONSTANTS  # noqa: E402
from mcp_servers.netdiag import mcp, netdiag_diagnose, netdiag_dns_compare  # noqa: E402

MAX_OUTPUT_CHARS = CONSTANTS.max_output_chars

__all__ = [
    "MAX_OUTPUT_CHARS",
    "mcp",
    "netdiag_diagnose",
    "netdiag_dns_compare",
]

if __name__ == "__main__":
    mcp.run()

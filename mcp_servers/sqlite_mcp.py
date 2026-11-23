"""SQLite 唯讀查詢與結構檢索 MCP 伺服器薄入口。"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_servers._shared import CONSTANTS  # noqa: E402
from mcp_servers.sqlite import (  # noqa: E402
    mcp,
    sqlite_describe_schema,
    sqlite_inspect_health,
    sqlite_list_tables,
    sqlite_run_select_query,
    sqlite_switch_database,
)
from mcp_servers.sqlite.tools.health import SQLITE_QUICK_CHECK_MAX_BYTES  # noqa: E402

MAX_OUTPUT_CHARS = CONSTANTS.max_output_chars

__all__ = [
    "MAX_OUTPUT_CHARS",
    "SQLITE_QUICK_CHECK_MAX_BYTES",
    "mcp",
    "sqlite_describe_schema",
    "sqlite_inspect_health",
    "sqlite_list_tables",
    "sqlite_run_select_query",
    "sqlite_switch_database",
]

if __name__ == "__main__":
    mcp.run()

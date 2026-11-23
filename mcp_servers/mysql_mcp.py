"""MySQL / MariaDB 唯讀查詢與結構檢索 MCP 伺服器薄入口。"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_servers._shared import CONSTANTS  # noqa: E402
from mcp_servers.mysql import (  # noqa: E402
    mcp,
    mysql_check_table,
    mysql_describe_schema,
    mysql_inspect_health,
    mysql_list_databases,
    mysql_list_tables,
    mysql_run_select_query,
)
from mcp_servers.mysql.tools.health import MYSQL_CHECK_TABLE_MAX_BYTES  # noqa: E402

MAX_OUTPUT_CHARS = CONSTANTS.max_output_chars

__all__ = [
    "MAX_OUTPUT_CHARS",
    "MYSQL_CHECK_TABLE_MAX_BYTES",
    "mcp",
    "mysql_check_table",
    "mysql_describe_schema",
    "mysql_inspect_health",
    "mysql_list_databases",
    "mysql_list_tables",
    "mysql_run_select_query",
]

if __name__ == "__main__":
    mcp.run()

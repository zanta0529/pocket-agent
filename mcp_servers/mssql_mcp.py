"""Microsoft SQL Server (MSSQL) 唯讀查詢與結構檢索 MCP 伺服器薄入口。"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_servers._shared import CONSTANTS  # noqa: E402
from mcp_servers.mssql import (  # noqa: E402
    mcp,
    mssql_backup_and_ag_status,
    mssql_describe_schema,
    mssql_explain_query,
    mssql_get_object_definition,
    mssql_inspect_active_requests,
    mssql_inspect_health,
    mssql_inspect_missing_indexes,
    mssql_list_databases,
    mssql_list_tables,
    mssql_run_select_query,
    mssql_server_status,
)

MAX_OUTPUT_CHARS = CONSTANTS.max_output_chars

__all__ = [
    "MAX_OUTPUT_CHARS",
    "mcp",
    "mssql_backup_and_ag_status",
    "mssql_describe_schema",
    "mssql_explain_query",
    "mssql_get_object_definition",
    "mssql_inspect_active_requests",
    "mssql_inspect_health",
    "mssql_inspect_missing_indexes",
    "mssql_list_databases",
    "mssql_list_tables",
    "mssql_run_select_query",
    "mssql_server_status",
]

if __name__ == "__main__":
    mcp.run()

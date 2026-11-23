"""MSSQL MCP 工具群組套件。"""

from mcp_servers.mssql.tools.health import mssql_inspect_health
from mcp_servers.mssql.tools.query import (
    mssql_describe_schema,
    mssql_list_databases,
    mssql_list_tables,
    mssql_run_select_query,
)

__all__ = [
    "mssql_describe_schema",
    "mssql_inspect_health",
    "mssql_list_databases",
    "mssql_list_tables",
    "mssql_run_select_query",
]

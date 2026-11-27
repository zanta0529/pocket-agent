"""MSSQL MCP 工具群組套件。"""

from mcp_servers.mssql.tools import health, ops, query, schema
from mcp_servers.mssql.tools.health import mssql_inspect_health, mssql_inspect_missing_indexes
from mcp_servers.mssql.tools.ops import (
    mssql_backup_and_ag_status,
    mssql_inspect_active_requests,
    mssql_server_status,
)
from mcp_servers.mssql.tools.query import mssql_explain_query, mssql_run_select_query
from mcp_servers.mssql.tools.schema import (
    mssql_describe_schema,
    mssql_get_object_definition,
    mssql_list_databases,
    mssql_list_tables,
)

REGISTRARS = (
    schema.register,
    query.register,
    health.register,
    ops.register,
)

__all__ = [
    "REGISTRARS",
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

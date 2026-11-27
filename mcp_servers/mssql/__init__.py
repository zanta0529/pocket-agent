"""Microsoft SQL Server (MSSQL) 唯讀查詢與結構檢索 MCP 子套件。"""

from mcp_servers.mssql.adapter import MSSQL_ADAPTER, MssqlReadOnlyAdapter
from mcp_servers.mssql.server import mcp
from mcp_servers.mssql.sql_guard import MSSQL_SQL_GUARD, MssqlSqlGuard
from mcp_servers.mssql.tools import (
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

__all__ = [
    "MSSQL_ADAPTER",
    "MSSQL_SQL_GUARD",
    "MssqlReadOnlyAdapter",
    "MssqlSqlGuard",
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

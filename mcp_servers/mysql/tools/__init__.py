"""MySQL MCP 工具群組套件。"""

from mcp_servers.mysql.tools import health, ops, query, schema
from mcp_servers.mysql.tools.health import mysql_check_table, mysql_inspect_health
from mcp_servers.mysql.tools.ops import (
    mysql_inspect_processlist,
    mysql_replication_status,
    mysql_server_status,
)
from mcp_servers.mysql.tools.query import mysql_explain_query, mysql_run_select_query
from mcp_servers.mysql.tools.schema import (
    mysql_describe_schema,
    mysql_list_databases,
    mysql_list_tables,
    mysql_show_create_table,
)

REGISTRARS = (
    schema.register,
    query.register,
    health.register,
    ops.register,
)

__all__ = [
    "REGISTRARS",
    "mysql_check_table",
    "mysql_describe_schema",
    "mysql_explain_query",
    "mysql_inspect_health",
    "mysql_inspect_processlist",
    "mysql_list_databases",
    "mysql_list_tables",
    "mysql_replication_status",
    "mysql_run_select_query",
    "mysql_server_status",
    "mysql_show_create_table",
]

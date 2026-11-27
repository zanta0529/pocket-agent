"""MySQL / MariaDB 唯讀查詢與結構檢索 MCP 子套件。"""

from mcp_servers.mysql.adapter import MYSQL_ADAPTER, MysqlReadOnlyAdapter
from mcp_servers.mysql.server import mcp
from mcp_servers.mysql.sql_guard import MYSQL_SQL_GUARD, MysqlSqlGuard
from mcp_servers.mysql.tools import (
    mysql_check_table,
    mysql_describe_schema,
    mysql_explain_query,
    mysql_inspect_health,
    mysql_inspect_processlist,
    mysql_list_databases,
    mysql_list_tables,
    mysql_replication_status,
    mysql_run_select_query,
    mysql_server_status,
    mysql_show_create_table,
)

__all__ = [
    "MYSQL_ADAPTER",
    "MYSQL_SQL_GUARD",
    "MysqlReadOnlyAdapter",
    "MysqlSqlGuard",
    "mcp",
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

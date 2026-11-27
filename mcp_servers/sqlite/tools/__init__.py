"""SQLite MCP 工具群組套件。"""

from mcp_servers.sqlite.tools import health as health_tools
from mcp_servers.sqlite.tools import ops as ops_tools
from mcp_servers.sqlite.tools import query as query_tools
from mcp_servers.sqlite.tools import schema as schema_tools
from mcp_servers.sqlite.tools.health import sqlite_check_integrity, sqlite_inspect_health
from mcp_servers.sqlite.tools.ops import sqlite_database_info, sqlite_table_stats
from mcp_servers.sqlite.tools.query import (
    sqlite_explain_query,
    sqlite_run_select_query,
    sqlite_switch_database,
)
from mcp_servers.sqlite.tools.schema import (
    sqlite_describe_schema,
    sqlite_get_ddl,
    sqlite_list_tables,
)

REGISTRARS = (
    schema_tools.register,
    query_tools.register,
    health_tools.register,
    ops_tools.register,
)

__all__ = [
    "REGISTRARS",
    "sqlite_check_integrity",
    "sqlite_database_info",
    "sqlite_describe_schema",
    "sqlite_explain_query",
    "sqlite_get_ddl",
    "sqlite_inspect_health",
    "sqlite_list_tables",
    "sqlite_run_select_query",
    "sqlite_switch_database",
    "sqlite_table_stats",
]

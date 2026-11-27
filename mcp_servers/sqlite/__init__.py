"""SQLite 唯讀查詢、結構探查與維運診斷 MCP 子套件。"""

from mcp_servers.sqlite.adapter import SQLITE_ADAPTER, SqliteReadOnlyAdapter
from mcp_servers.sqlite.server import mcp
from mcp_servers.sqlite.sql_guard import SQLITE_SQL_GUARD, SqliteSqlGuard
from mcp_servers.sqlite.tools import (
    sqlite_check_integrity,
    sqlite_database_info,
    sqlite_describe_schema,
    sqlite_explain_query,
    sqlite_get_ddl,
    sqlite_inspect_health,
    sqlite_list_tables,
    sqlite_run_select_query,
    sqlite_switch_database,
    sqlite_table_stats,
)

__all__ = [
    "SQLITE_ADAPTER",
    "SQLITE_SQL_GUARD",
    "SqliteReadOnlyAdapter",
    "SqliteSqlGuard",
    "mcp",
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

"""SQLite 唯讀查詢與結構檢索 MCP 子套件。"""

from mcp_servers.sqlite.adapter import SQLITE_ADAPTER, SqliteReadOnlyAdapter
from mcp_servers.sqlite.server import mcp
from mcp_servers.sqlite.sql_guard import SQLITE_SQL_GUARD, SqliteSqlGuard
from mcp_servers.sqlite.tools import (
    sqlite_describe_schema,
    sqlite_inspect_health,
    sqlite_list_tables,
    sqlite_run_select_query,
    sqlite_switch_database,
)

__all__ = [
    "SQLITE_ADAPTER",
    "SQLITE_SQL_GUARD",
    "SqliteReadOnlyAdapter",
    "SqliteSqlGuard",
    "mcp",
    "sqlite_describe_schema",
    "sqlite_inspect_health",
    "sqlite_list_tables",
    "sqlite_run_select_query",
    "sqlite_switch_database",
]

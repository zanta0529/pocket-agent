"""MCP 伺服器啟動輔助、常數載入與多語系降級模組。"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def ensure_project_paths() -> Path:
    """確保專案根目錄與 src/ 位於 sys.path，支援獨立執行與 --internal-mcp-server 模式。"""
    repo_root = Path(__file__).resolve().parent.parent.parent
    src_dir = repo_root / "src"
    for candidate in (repo_root, src_dir):
        if candidate.is_dir():
            text = str(candidate)
            if text not in sys.path:
                sys.path.insert(0, text)
    return repo_root


ensure_project_paths()


class ConnAuthError(RuntimeError):
    """表示連線參數驗證失敗、憑證缺失或遠端認證失敗（供上層識別 [CONN_AUTH] 並清除快取）。"""


@dataclass(frozen=True)
class McpConstants:
    """集中管理 MCP 伺服器常用上限與門檻常數（優先讀取 pocket_agent.system.constants）。"""

    max_output_chars: int = 12000
    network_timeout_sec: float = 15.0
    max_redirects: int = 5
    max_response_bytes: int = 2 * 1024 * 1024
    max_capture_bytes: int = 1024 * 1024
    db_health_max_tables: int = 500
    mysql_check_table_max_bytes: int = 2 * 1024 * 1024 * 1024
    sqlite_quick_check_max_bytes: int = 500 * 1024 * 1024
    db_fragmentation_warn_ratio: float = 0.20
    db_fragmentation_min_free_bytes: int = 10 * 1024 * 1024
    db_autoinc_warn_ratio: float = 0.75


def load_constants() -> McpConstants:
    """載入系統中央常數；獨立執行無法匯入 pocket_agent 時自動降級為安全預設值。"""
    try:
        from pocket_agent.system import constants as sys_const

        return McpConstants(
            max_output_chars=sys_const.DEFAULT_TOOL_OUTPUT_CHARS,
            network_timeout_sec=sys_const.DEFAULT_NETWORK_TIMEOUT_SEC,
            max_redirects=sys_const.DEFAULT_MAX_REDIRECTS,
            max_response_bytes=sys_const.MAX_RESPONSE_BYTES,
            max_capture_bytes=sys_const.DEFAULT_PROCESS_BUFFER_BYTES,
            db_health_max_tables=sys_const.DB_HEALTH_MAX_TABLES,
            mysql_check_table_max_bytes=sys_const.MYSQL_CHECK_TABLE_MAX_BYTES,
            sqlite_quick_check_max_bytes=sys_const.SQLITE_QUICK_CHECK_MAX_BYTES,
            db_fragmentation_warn_ratio=sys_const.DB_FRAGMENTATION_WARN_RATIO,
            db_fragmentation_min_free_bytes=sys_const.DB_FRAGMENTATION_MIN_FREE_BYTES,
            db_autoinc_warn_ratio=sys_const.DB_AUTOINC_WARN_RATIO,
        )
    except ImportError:
        return McpConstants()


CONSTANTS = load_constants()


def get_mcp_server_class() -> type[Any]:
    """取得官方 MCP 2.x MCPServer 類別；舊環境降級至 FastMCP。"""
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP

        return FastMCP


def t(key: str, /, **kwargs: Any) -> str:
    """多語系翻譯輔助；獨立執行無法載入 pocket_agent.system.i18n 時回傳純鍵值。"""
    try:
        from pocket_agent.system.i18n import t as _pa_t

        return _pa_t(key, **kwargs)
    except ImportError:
        return key

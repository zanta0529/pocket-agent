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
    netdiag_default_timeout_sec: float = 10.0
    netdiag_min_timeout_sec: float = 3.0
    netdiag_max_timeout_sec: float = 15.0
    netdiag_max_samples: int = 3
    netdiag_max_header_value_chars: int = 512
    netdiag_dns_max_nameservers: int = 4
    netdiag_dns_max_cname_hops: int = 10
    netdiag_default_allowed_ports: tuple[int, ...] = (80, 443, 8080, 8443)
    netdiag_cert_expiry_warn_days: int = 30
    rhel_creds_cache_ttl_sec: float = 600.0
    rhel_default_connect_timeout_sec: int = 10
    rhel_min_connect_timeout_sec: int = 3
    rhel_max_connect_timeout_sec: int = 60
    rhel_cmd_extra_timeout_sec: int = 15
    rhel_min_cmd_timeout_sec: int = 5
    rhel_max_cmd_timeout_sec: int = 120
    rhel_max_batch_services: int = 10
    rhel_max_cert_files: int = 30
    browser_default_timeout_ms: int = 15_000
    browser_min_timeout_ms: int = 3_000
    browser_max_timeout_ms: int = 60_000
    browser_wait_default_timeout_sec: float = 10.0
    browser_wait_min_timeout_sec: float = 1.0
    browser_wait_max_timeout_sec: float = 30.0
    browser_scroll_default_px: int = 600
    browser_scroll_min_px: int = 100
    browser_scroll_max_px: int = 3_000
    browser_snapshot_max_label_chars: int = 80
    browser_max_tracked_dialogs: int = 20
    mysql_creds_cache_ttl_sec: float = 600.0
    mysql_default_connect_timeout_sec: int = 10
    mysql_min_connect_timeout_sec: int = 3
    mysql_max_connect_timeout_sec: int = 60
    mysql_default_query_timeout_ms: int = 10_000
    mysql_min_query_timeout_ms: int = 1_000
    mysql_max_query_timeout_ms: int = 60_000
    mysql_processlist_max_rows: int = 50
    mysql_processlist_info_max_chars: int = 200


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
            netdiag_default_timeout_sec=sys_const.NETDIAG_DEFAULT_TIMEOUT_SEC,
            netdiag_min_timeout_sec=sys_const.NETDIAG_MIN_TIMEOUT_SEC,
            netdiag_max_timeout_sec=sys_const.NETDIAG_MAX_TIMEOUT_SEC,
            netdiag_max_samples=sys_const.NETDIAG_MAX_SAMPLES,
            netdiag_max_header_value_chars=sys_const.NETDIAG_MAX_HEADER_VALUE_CHARS,
            netdiag_dns_max_nameservers=sys_const.NETDIAG_DNS_MAX_NAMESERVERS,
            netdiag_dns_max_cname_hops=sys_const.NETDIAG_DNS_MAX_CNAME_HOPS,
            netdiag_default_allowed_ports=sys_const.NETDIAG_DEFAULT_ALLOWED_PORTS,
            netdiag_cert_expiry_warn_days=sys_const.NETDIAG_CERT_EXPIRY_WARN_DAYS,
            rhel_creds_cache_ttl_sec=sys_const.RHEL_CREDS_CACHE_TTL_SEC,
            rhel_default_connect_timeout_sec=sys_const.RHEL_DEFAULT_CONNECT_TIMEOUT_SEC,
            rhel_min_connect_timeout_sec=sys_const.RHEL_MIN_CONNECT_TIMEOUT_SEC,
            rhel_max_connect_timeout_sec=sys_const.RHEL_MAX_CONNECT_TIMEOUT_SEC,
            rhel_cmd_extra_timeout_sec=sys_const.RHEL_CMD_EXTRA_TIMEOUT_SEC,
            rhel_min_cmd_timeout_sec=sys_const.RHEL_MIN_CMD_TIMEOUT_SEC,
            rhel_max_cmd_timeout_sec=sys_const.RHEL_MAX_CMD_TIMEOUT_SEC,
            rhel_max_batch_services=sys_const.RHEL_MAX_BATCH_SERVICES,
            rhel_max_cert_files=sys_const.RHEL_MAX_CERT_FILES,
            browser_default_timeout_ms=sys_const.BROWSER_DEFAULT_TIMEOUT_MS,
            browser_min_timeout_ms=sys_const.BROWSER_MIN_TIMEOUT_MS,
            browser_max_timeout_ms=sys_const.BROWSER_MAX_TIMEOUT_MS,
            browser_wait_default_timeout_sec=sys_const.BROWSER_WAIT_DEFAULT_TIMEOUT_SEC,
            browser_wait_min_timeout_sec=sys_const.BROWSER_WAIT_MIN_TIMEOUT_SEC,
            browser_wait_max_timeout_sec=sys_const.BROWSER_WAIT_MAX_TIMEOUT_SEC,
            browser_scroll_default_px=sys_const.BROWSER_SCROLL_DEFAULT_PX,
            browser_scroll_min_px=sys_const.BROWSER_SCROLL_MIN_PX,
            browser_scroll_max_px=sys_const.BROWSER_SCROLL_MAX_PX,
            browser_snapshot_max_label_chars=sys_const.BROWSER_SNAPSHOT_MAX_LABEL_CHARS,
            browser_max_tracked_dialogs=sys_const.BROWSER_MAX_TRACKED_DIALOGS,
            mysql_creds_cache_ttl_sec=sys_const.MYSQL_CREDS_CACHE_TTL_SEC,
            mysql_default_connect_timeout_sec=sys_const.MYSQL_DEFAULT_CONNECT_TIMEOUT_SEC,
            mysql_min_connect_timeout_sec=sys_const.MYSQL_MIN_CONNECT_TIMEOUT_SEC,
            mysql_max_connect_timeout_sec=sys_const.MYSQL_MAX_CONNECT_TIMEOUT_SEC,
            mysql_default_query_timeout_ms=sys_const.MYSQL_DEFAULT_QUERY_TIMEOUT_MS,
            mysql_min_query_timeout_ms=sys_const.MYSQL_MIN_QUERY_TIMEOUT_MS,
            mysql_max_query_timeout_ms=sys_const.MYSQL_MAX_QUERY_TIMEOUT_MS,
            mysql_processlist_max_rows=sys_const.MYSQL_PROCESSLIST_MAX_ROWS,
            mysql_processlist_info_max_chars=sys_const.MYSQL_PROCESSLIST_INFO_MAX_CHARS,
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

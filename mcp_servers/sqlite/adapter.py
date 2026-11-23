"""SQLite 唯讀連線轉接器（Adapter Pattern）：URI mode=ro、Authorizer 與 UNC 路徑阻斷。"""

import os
import sqlite3
from pathlib import Path

from mcp_servers._shared import (
    AuditLogger,
    ConnAuthError,
    CredentialCache,
    ReadOnlyDatabaseAdapter,
    is_empty_or_null,
    tty_prompt,
)

_UNC_PREFIXES = ("\\\\", "//")
_BASE_ALLOWED_PRAGMAS = frozenset({"table_info", "table_xinfo", "index_list", "index_info", "foreign_key_list"})
_DIAGNOSTIC_PRAGMAS = frozenset(
    {
        "page_count",
        "page_size",
        "freelist_count",
        "journal_mode",
        "auto_vacuum",
        "encoding",
        "user_version",
        "application_id",
        "quick_check",
        "foreign_key_check",
    }
)
_ALLOWED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
)


def is_network_path(raw: str | None) -> bool:
    """判斷是否為 UNC／網路路徑（須在任何檔案系統操作前呼叫，避免 SMB 認證雜湊外洩）。"""
    if not raw:
        return False
    return raw.strip().strip("'\"").startswith(_UNC_PREFIXES)


class SqliteReadOnlyAdapter(ReadOnlyDatabaseAdapter):
    """封裝 SQLite 路徑解析、TTL 快取、URI 唯讀開啟與 SQLite Authorizer 隔離。"""

    def __init__(
        self,
        *,
        cache: CredentialCache | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self.cache = cache or CredentialCache(ttl_seconds=600.0)
        self.audit = audit or AuditLogger("sqlite")

    def clear_cache(self) -> None:
        self.cache.clear()

    def current_target(self, db_path: str = "") -> str:
        return db_path or self.cache.get("DB_PATH") or (os.getenv("DB_PATH") or "")

    def get_db_path(self, custom_path: str = "") -> Path:
        """解析並驗證 DB_PATH 目標檔案路徑（先於檔案系統操作攔截 UNC 路徑）。"""
        with self.cache.lock:
            raw_path = custom_path.strip().strip("'\"") if custom_path else ""

            if is_empty_or_null(raw_path):
                cached_path = self.cache.get("DB_PATH")
                env_val = os.getenv("DB_PATH")
                if not is_empty_or_null(cached_path):
                    raw_path = cached_path
                elif not is_empty_or_null(env_val):
                    raw_path = (env_val or "").strip().strip("'\"")
                else:
                    prompt_text = "請輸入要連線的 SQLite 資料庫完整路徑與檔名 (DB_PATH): "
                    raw_path = tty_prompt(prompt_text, hint="DB_PATH").strip().strip("'\"")

            if is_empty_or_null(raw_path):
                self.cache.clear()
                raise ValueError("未提供 SQLite 資料庫路徑 (DB_PATH)。")

            if is_network_path(raw_path):
                raise ValueError("安全限制：不接受 UNC／網路路徑，請先將資料庫複製到本機。")

            resolved = Path(raw_path).expanduser().resolve()
            if not resolved.exists():
                self.cache.clear()
                raise FileNotFoundError(f"找不到指定的資料庫檔案：{resolved}")
            if not resolved.is_file():
                self.cache.clear()
                raise ValueError(f"指定的資料庫路徑並非檔案：{resolved}")

            self.cache.set("DB_PATH", str(resolved))
            return resolved

    def connect(
        self,
        tool_name: str = "sqlite",
        sql_context: str = "",
        db_path: str = "",
        *,
        allow_diagnostic_pragmas: bool = False,
    ) -> sqlite3.Connection:
        """以唯讀 URI 模式開啟 SQLite 連線，並註冊 authorizer 阻止寫入與非白名單 PRAGMA。"""
        try:
            resolved_path = self.get_db_path(db_path)
        except Exception as exc:
            raise ConnAuthError(f"[CONN_AUTH] {exc}") from exc

        uri_path = resolved_path.as_uri() + "?mode=ro"
        try:
            conn = sqlite3.connect(uri_path, uri=True)
        except Exception as exc:
            self.cache.clear()
            raise ConnAuthError(f"[CONN_AUTH] 無法開啟 SQLite 唯讀連線：{exc}") from exc

        allowed_pragmas = _BASE_ALLOWED_PRAGMAS | (_DIAGNOSTIC_PRAGMAS if allow_diagnostic_pragmas else frozenset())

        def _authorizer(action_code, arg1, arg2, db_name, trigger_name):
            if action_code == sqlite3.SQLITE_PRAGMA:
                pragma_name = str(arg1 or "").lower()
                if pragma_name in allowed_pragmas:
                    return sqlite3.SQLITE_OK
                self.audit.log(
                    tool_name,
                    sql_context or f"PRAGMA {arg1}",
                    target=str(resolved_path),
                    status="SECURITY_BLOCK",
                    detail=f"Authorizer blocked PRAGMA {arg1!r}",
                )
                return sqlite3.SQLITE_DENY

            if action_code in _ALLOWED_ACTIONS:
                return sqlite3.SQLITE_OK
            self.audit.log(
                tool_name,
                sql_context or f"action={action_code}",
                target=str(resolved_path),
                status="SECURITY_BLOCK",
                detail=f"Authorizer blocked action_code={action_code} arg1={arg1!r} arg2={arg2!r}",
            )
            return sqlite3.SQLITE_DENY

        conn.set_authorizer(_authorizer)
        return conn


SQLITE_ADAPTER = SqliteReadOnlyAdapter()

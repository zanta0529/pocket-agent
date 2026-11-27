"""SQLite 唯讀連線轉接器（Adapter Pattern）：URI mode=ro、Authorizer、逾時中斷與 UNC/Symlink 阻斷。"""

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from mcp_servers._shared import (
    CONSTANTS,
    DEFAULT_REDACTION_POLICY,
    AuditLogger,
    ConnAuthError,
    CredentialCache,
    ReadOnlyDatabaseAdapter,
    clean_param,
    is_empty_or_null,
    t,
    tty_prompt_text,
)

_BASE_ALLOWED_PRAGMAS = frozenset(
    {
        "table_info",
        "table_xinfo",
        "index_list",
        "index_info",
        "index_xinfo",
        "foreign_key_list",
    }
)
_NO_ARG_DIAGNOSTIC_PRAGMAS = frozenset(
    {
        "page_count",
        "page_size",
        "freelist_count",
        "journal_mode",
        "auto_vacuum",
        "encoding",
        "user_version",
        "application_id",
        "schema_version",
        "foreign_keys",
        "compile_options",
    }
)
_ARG_ALLOWED_DIAGNOSTIC_PRAGMAS = frozenset(
    {
        "quick_check",
        "integrity_check",
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
_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "load_extension",
        "readfile",
        "writefile",
        "edit",
        "fts3_tokenizer",
        "zeroblob",
        "randomblob",
    }
)


def is_network_path(raw: str | None) -> bool:
    """判斷是否為 UNC／網路路徑、Windows 裝置路徑或含 NUL 字元（先於檔案系統操作攔截）。"""
    if not raw:
        return False
    if "\x00" in raw:
        return True
    normalized = raw.strip().strip("'\"").replace("/", "\\")
    return normalized.startswith("\\\\")


def _parse_clamped_float(env_key: str, default_val: float, min_val: float, max_val: float) -> float:
    raw = clean_param(os.getenv(env_key))
    if not raw:
        return default_val
    try:
        parsed = float(raw)
    except ValueError:
        return default_val
    return max(min_val, min(max_val, parsed))


def get_query_timeout_sec() -> float:
    """讀取並夾緊 SQLite progress_handler 單次查詢逾時秒數。"""
    return _parse_clamped_float(
        "SQLITE_QUERY_TIMEOUT_SEC",
        float(CONSTANTS.sqlite_default_query_timeout_sec),
        float(CONSTANTS.sqlite_min_query_timeout_sec),
        float(CONSTANTS.sqlite_max_query_timeout_sec),
    )


def get_busy_timeout_sec() -> float:
    """讀取並夾緊 SQLite 唯讀連線 busy_timeout 秒數。"""
    return _parse_clamped_float(
        "SQLITE_BUSY_TIMEOUT_SEC",
        float(CONSTANTS.sqlite_default_busy_timeout_sec),
        float(CONSTANTS.sqlite_min_busy_timeout_sec),
        float(CONSTANTS.sqlite_max_busy_timeout_sec),
    )


class SqliteReadOnlyAdapter(ReadOnlyDatabaseAdapter):
    """封裝 SQLite 路徑解析、TTL 快取、URI 唯讀開啟、查詢逾時與 SQLite Authorizer 隔離。"""

    def __init__(
        self,
        *,
        cache: CredentialCache | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self.cache = cache or CredentialCache(ttl_seconds=CONSTANTS.sqlite_creds_cache_ttl_sec)
        self.audit = audit or AuditLogger("sqlite")

    def clear_cache(self) -> None:
        self.cache.clear()

    def current_target(self, db_path: str = "") -> str:
        return clean_param(db_path) or self.cache.get("DB_PATH") or clean_param(os.getenv("DB_PATH"))

    def get_db_path(self, custom_path: str = "", *, persist_cache: bool = False) -> Path:
        """解析並驗證 DB_PATH 目標檔案路徑（先於檔案系統操作攔截 UNC／裝置路徑／網路符號連結）。"""
        with self.cache.lock:
            cleaned_custom = clean_param(custom_path)
            from_custom = bool(cleaned_custom)
            from_cache = False
            from_env_or_tty = False

            if cleaned_custom:
                raw_path = cleaned_custom
            else:
                cached_path = self.cache.get("DB_PATH")
                env_val = clean_param(os.getenv("DB_PATH"))
                if not is_empty_or_null(cached_path):
                    raw_path = cached_path
                    from_cache = True
                elif env_val:
                    raw_path = env_val
                    from_env_or_tty = True
                else:
                    prompted = tty_prompt_text(t("sqlite_mcp.prompt_db_path"), hint="DB_PATH")
                    raw_path = clean_param(prompted)
                    from_env_or_tty = True

            if is_empty_or_null(raw_path):
                if from_cache:
                    self.cache.clear()
                raise ValueError(t("sqlite_mcp.missing_db_path"))

            if is_network_path(raw_path):
                raise ValueError(t("sqlite_mcp.unc_blocked"))

            expanded = Path(raw_path).expanduser()
            try:
                if os.path.islink(expanded):
                    link_target = os.readlink(expanded)
                    if is_network_path(link_target):
                        raise ValueError(t("sqlite_mcp.unc_blocked"))
            except OSError:
                pass

            resolved = expanded.resolve()
            if is_network_path(str(resolved)):
                raise ValueError(t("sqlite_mcp.unc_blocked"))

            if not resolved.exists():
                if from_cache:
                    self.cache.clear()
                raise FileNotFoundError(t("sqlite_mcp.db_not_found", path=resolved))
            if not resolved.is_file():
                if from_cache:
                    self.cache.clear()
                raise ValueError(t("sqlite_mcp.db_not_file", path=resolved))

            if persist_cache or (from_env_or_tty and not from_custom):
                self.cache.set("DB_PATH", str(resolved))
            return resolved

    def open_connection(
        self,
        tool_name: str = "sqlite",
        sql_context: str = "",
        db_path: str = "",
        *,
        allow_diagnostic_pragmas: bool = False,
        persist_cache: bool = False,
        query_timeout_sec: float | None = None,
    ) -> tuple[sqlite3.Connection, Path]:
        """以唯讀 URI 模式開啟連線，掛載 authorizer 與 progress_handler 逾時中斷。"""
        has_custom = bool(clean_param(db_path))
        try:
            resolved_path = self.get_db_path(db_path, persist_cache=persist_cache)
        except Exception as exc:
            safe_msg = DEFAULT_REDACTION_POLICY.redact(str(exc))
            raise ConnAuthError(f"[CONN_AUTH] {safe_msg}") from exc

        uri_path = resolved_path.as_uri() + "?mode=ro"
        busy_timeout = get_busy_timeout_sec()
        try:
            conn = sqlite3.connect(uri_path, uri=True, timeout=busy_timeout)
        except Exception as exc:
            if not has_custom:
                self.cache.clear()
            safe_err = DEFAULT_REDACTION_POLICY.redact(str(exc))
            if "unable to open database file" in safe_err.lower():
                safe_err = f"{safe_err} {t('sqlite_mcp.connect_wal_ro_hint')}"
            raise ConnAuthError(f"[CONN_AUTH] {t('sqlite_mcp.connect_failed', error=safe_err)}") from exc

        def _authorizer(action_code: int, arg1: Any, arg2: Any, db_name: Any, trigger_name: Any) -> int:
            if action_code == sqlite3.SQLITE_PRAGMA:
                pragma_name = str(arg1 or "").lower()
                if pragma_name in _BASE_ALLOWED_PRAGMAS:
                    return sqlite3.SQLITE_OK
                if allow_diagnostic_pragmas:
                    if pragma_name in _ARG_ALLOWED_DIAGNOSTIC_PRAGMAS:
                        return sqlite3.SQLITE_OK
                    if pragma_name in _NO_ARG_DIAGNOSTIC_PRAGMAS and arg2 is None:
                        return sqlite3.SQLITE_OK
                self.audit.log(
                    tool_name,
                    sql_context or f"PRAGMA {arg1}",
                    target=str(resolved_path),
                    status="SECURITY_BLOCK",
                    detail=f"Authorizer blocked PRAGMA {arg1!r} arg2={arg2!r}",
                )
                return sqlite3.SQLITE_DENY

            if action_code == sqlite3.SQLITE_FUNCTION:
                func_name = str(arg2 or "").lower()
                if func_name in _FORBIDDEN_FUNCTIONS:
                    self.audit.log(
                        tool_name,
                        sql_context or f"FUNCTION {func_name}",
                        target=str(resolved_path),
                        status="SECURITY_BLOCK",
                        detail=f"Authorizer blocked function {func_name!r}",
                    )
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

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

        effective_timeout = query_timeout_sec if query_timeout_sec is not None else get_query_timeout_sec()
        if effective_timeout > 0:
            deadline = time.monotonic() + effective_timeout

            def _on_progress() -> int:
                return 1 if time.monotonic() > deadline else 0

            conn.set_progress_handler(_on_progress, 10_000)

        return conn, resolved_path

    def connect(
        self,
        tool_name: str = "sqlite",
        sql_context: str = "",
        db_path: str = "",
        *,
        allow_diagnostic_pragmas: bool = False,
        persist_cache: bool = False,
        query_timeout_sec: float | None = None,
    ) -> sqlite3.Connection:
        """向下相容介面：以唯讀 URI 模式開啟 SQLite 連線。"""
        conn, _ = self.open_connection(
            tool_name=tool_name,
            sql_context=sql_context,
            db_path=db_path,
            allow_diagnostic_pragmas=allow_diagnostic_pragmas,
            persist_cache=persist_cache,
            query_timeout_sec=query_timeout_sec,
        )
        return conn

    def close_safely(self, conn: Any) -> None:
        """解除 progress_handler 閉包後強制 rollback 並安全關閉連線。"""
        if conn is None:
            return
        try:
            conn.set_progress_handler(None, 0)
        except Exception:
            pass
        super().close_safely(conn)

    def abort_connection(self, conn: Any, cursor: Any = None) -> None:
        """提早截斷或異常時立即關閉游標並安全釋放連線。"""
        super().abort_connection(conn, cursor)


SQLITE_ADAPTER = SqliteReadOnlyAdapter()

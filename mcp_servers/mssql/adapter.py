"""Microsoft SQL Server (MSSQL) 唯讀連線轉接器（Adapter Pattern）。"""

import importlib
import os
import re
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
    tty_prompt_secret,
    tty_prompt_text,
    validate_host,
    validate_identifier,
    validate_port,
)

_USER_RE = re.compile(r"^[a-zA-Z0-9_.@$\-\\]+$")
_DRIVER_RE = re.compile(r"^[a-zA-Z0-9_.\- ]+$")


def load_mssql_driver() -> tuple[str, Any]:
    """延遲載入 SQL Server 驅動（優先使用 pymssql，次選 pyodbc）。"""
    for mod_name, driver_type in (("pymssql", "pymssql"), ("pyodbc", "pyodbc")):
        try:
            mod = importlib.import_module(mod_name)
            return driver_type, mod
        except ImportError:
            continue
    raise RuntimeError(t("mssql_mcp.driver_missing"))


def _resolve_odbc_driver(pyodbc_mod: Any) -> str:
    custom = clean_param(os.getenv("MSSQL_DRIVER"))
    if custom:
        if not _DRIVER_RE.match(custom):
            raise ValueError(t("mssql_mcp.invalid_driver_name"))
        return custom
    preferred = (
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "SQL Server Native Client 11.0",
        "SQL Server",
    )
    try:
        installed = set(pyodbc_mod.drivers())
        for candidate in preferred:
            if candidate in installed:
                return candidate
    except Exception:
        pass
    return preferred[0]


def _escape_odbc_value(val: str) -> str:
    """為 ODBC 連線字串屬性值加上大括號跳脫，避免分號注入。"""
    return "{" + val.replace("}", "}}") + "}"


def _parse_int_env(name: str, default: int, min_val: int, max_val: int) -> int:
    raw = clean_param(os.getenv(name))
    if not raw:
        return default
    try:
        return max(min_val, min(int(raw), max_val))
    except ValueError:
        return default


def adapt_placeholders(sql: str, conn: Any) -> str:
    """根據底層連線驅動轉換參數佔位符（pyodbc 使用 ?，忽略單引號字串常值內之 %s）。"""
    mod_name = type(conn).__module__.lower()
    if "pyodbc" not in mod_name:
        return sql

    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        if sql[i] == "'":
            out.append("'")
            i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 1
                        out.append(sql[i])
                        i += 1
                        continue
                    i += 1
                    break
                i += 1
            continue
        if sql[i : i + 2] == "%s":
            out.append("?")
            i += 2
            continue
        out.append(sql[i])
        i += 1
    return "".join(out)


class MssqlReadOnlyAdapter(ReadOnlyDatabaseAdapter):
    """封裝 MSSQL 憑證快取、雙驅動唯讀隔離、查詢與鎖等待逾時及快速中止。"""

    def __init__(
        self,
        *,
        cache: CredentialCache | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self.cache = cache or CredentialCache(ttl_seconds=CONSTANTS.mssql_creds_cache_ttl_sec)
        self.audit = audit or AuditLogger("mssql")

    def clear_cache(self) -> None:
        self.cache.clear()

    def format_audit_target(
        self,
        host: str = "",
        port: str = "",
        user: str = "",
        db: str = "",
    ) -> str:
        h = host.strip() or self.cache.get("MSSQL_HOST") or clean_param(os.getenv("MSSQL_HOST")) or "?"
        p = port.strip() or self.cache.get("MSSQL_PORT") or clean_param(os.getenv("MSSQL_PORT")) or "1433"
        u = user.strip() or self.cache.get("MSSQL_USER") or clean_param(os.getenv("MSSQL_USER")) or "?"
        d = db.strip() or self.cache.get("MSSQL_DATABASE") or clean_param(os.getenv("MSSQL_DATABASE")) or "*"
        return f"{u}@{h}:{p}/{d}"

    def resolve_connection_params(
        self,
        mssql_host: str = "",
        mssql_port: str = "",
        mssql_user: str = "",
        mssql_password: str = "",
        mssql_database: str = "",
    ) -> tuple[str, str, str, str, str]:
        """依「注入參數 → 記憶體快取 → 環境變數 → 終端機互動詢問」順序取得 SQL Server 連線參數。"""
        with self.cache.lock:
            cached_host = self.cache.get("MSSQL_HOST")
            cached_port = self.cache.get("MSSQL_PORT")
            cached_user = self.cache.get("MSSQL_USER")
            cached_db = self.cache.get("MSSQL_DATABASE")

            env_host = clean_param(os.getenv("MSSQL_HOST"))
            env_port = clean_param(os.getenv("MSSQL_PORT"))
            env_user = clean_param(os.getenv("MSSQL_USER"))
            env_db = clean_param(os.getenv("MSSQL_DATABASE"))

            host = clean_param(mssql_host) or cached_host or env_host
            port = clean_param(mssql_port) or cached_port or env_port
            user = clean_param(mssql_user) or cached_user or env_user
            database = clean_param(mssql_database) or cached_db or env_db

            if not host:
                host = clean_param(tty_prompt_text(t("mssql_mcp.prompt_host"), hint="MSSQL_*"))
            if not port:
                port = clean_param(tty_prompt_text(t("mssql_mcp.prompt_port"), hint="MSSQL_*")) or "1433"
            if not user:
                user = clean_param(tty_prompt_text(t("mssql_mcp.prompt_user", host=host), hint="MSSQL_*"))

            if not host:
                raise ValueError(t("mssql_mcp.missing_host"))
            if not user:
                raise ValueError(t("mssql_mcp.missing_user"))

            host = validate_host(host, "MSSQL_HOST", allow_backslash=True)
            port = validate_port(port, "MSSQL_PORT")
            user = validate_identifier(user, "MSSQL_USER", pattern=_USER_RE)
            if database:
                database = validate_identifier(database, "MSSQL_DATABASE")

            norm_env_host = ""
            if env_host:
                try:
                    norm_env_host = validate_host(env_host, "MSSQL_HOST", allow_backslash=True)
                except ValueError:
                    norm_env_host = ""
            norm_env_port = ""
            if env_port:
                try:
                    norm_env_port = validate_port(env_port, "MSSQL_PORT")
                except ValueError:
                    norm_env_port = ""

            target_auth = (host, port, user)
            cached_auth = (cached_host, cached_port, cached_user)
            env_matches = (
                (not norm_env_host or norm_env_host == host)
                and (not norm_env_port or norm_env_port == port)
                and (not env_user or env_user == user)
            )

            password = mssql_password if not is_empty_or_null(mssql_password) else ""
            if not password and target_auth == cached_auth:
                password = self.cache.get_secret("MSSQL_PASSWORD")
            if not password and env_matches:
                env_pw = os.getenv("MSSQL_PASSWORD")
                if not is_empty_or_null(env_pw):
                    password = str(env_pw)

            if not password:
                password = tty_prompt_secret(t("mssql_mcp.prompt_password", user=user, host=host))
            if not password:
                raise ValueError(t("mssql_mcp.missing_password"))

            self.cache.update(
                {
                    "MSSQL_HOST": host,
                    "MSSQL_PORT": port,
                    "MSSQL_USER": user,
                    "MSSQL_DATABASE": database,
                }
            )
            self.cache.set_secret("MSSQL_PASSWORD", password)
            return host, port, user, password, database

    @staticmethod
    def _apply_session_guards(cursor: Any, lock_timeout_ms: int) -> None:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;")
        try:
            cursor.execute(f"SET LOCK_TIMEOUT {lock_timeout_ms};")
        except Exception:
            pass

    def connect(
        self,
        mssql_host: str = "",
        mssql_port: str = "",
        mssql_user: str = "",
        mssql_password: str = "",
        mssql_database: str = "",
    ) -> Any:
        """建立遠端 SQL Server 連線並強制設定為唯讀交易隔離與鎖等待上限。"""
        try:
            host, port, user, password, database = self.resolve_connection_params(
                mssql_host, mssql_port, mssql_user, mssql_password, mssql_database
            )
        except ValueError as exc:
            self.clear_cache()
            raise ConnAuthError(f"[CONN_AUTH] {DEFAULT_REDACTION_POLICY.redact(str(exc))}") from exc

        driver_type, driver_mod = load_mssql_driver()
        connect_timeout = _parse_int_env(
            "MSSQL_CONNECT_TIMEOUT",
            CONSTANTS.mssql_default_connect_timeout_sec,
            CONSTANTS.mssql_min_connect_timeout_sec,
            CONSTANTS.mssql_max_connect_timeout_sec,
        )
        query_timeout = _parse_int_env(
            "MSSQL_QUERY_TIMEOUT_SEC",
            CONSTANTS.mssql_default_query_timeout_sec,
            CONSTANTS.mssql_min_query_timeout_sec,
            CONSTANTS.mssql_max_query_timeout_sec,
        )
        lock_timeout_ms = _parse_int_env(
            "MSSQL_LOCK_TIMEOUT_MS",
            CONSTANTS.mssql_default_lock_timeout_ms,
            CONSTANTS.mssql_min_lock_timeout_ms,
            CONSTANTS.mssql_max_lock_timeout_ms,
        )
        db_kwarg = {"database": database} if database else {}

        conn = None
        try:
            if driver_type == "pymssql":
                conn = driver_mod.connect(
                    server=host,
                    port=int(port),
                    user=user,
                    password=password,
                    login_timeout=connect_timeout,
                    timeout=query_timeout,
                    autocommit=False,
                    **db_kwarg,
                )
            else:
                odbc_driver = _resolve_odbc_driver(driver_mod)
                encrypt_env = (clean_param(os.getenv("MSSQL_ENCRYPT")) or "yes").lower()
                encrypt_val = "no" if encrypt_env in ("no", "false", "0") else "yes"
                trust_env = (clean_param(os.getenv("MSSQL_TRUST_SERVER_CERTIFICATE")) or "yes").lower()
                trust_val = "no" if trust_env in ("no", "false", "0") else "yes"
                db_part = f"DATABASE={_escape_odbc_value(database)};" if database else ""
                conn_str = (
                    f"DRIVER={_escape_odbc_value(odbc_driver)};"
                    f"SERVER=tcp:{host},{port};"
                    f"{db_part}"
                    f"UID={_escape_odbc_value(user)};"
                    f"PWD={_escape_odbc_value(password)};"
                    "ApplicationIntent=ReadOnly;"
                    f"Encrypt={encrypt_val};"
                    f"TrustServerCertificate={trust_val};"
                )
                conn = driver_mod.connect(conn_str, timeout=connect_timeout, autocommit=False)
                conn.timeout = query_timeout

            cursor = conn.cursor()
            try:
                self._apply_session_guards(cursor, lock_timeout_ms)
            finally:
                cursor.close()
            return conn
        except Exception as exc:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self.clear_cache()
            safe_err = DEFAULT_REDACTION_POLICY.redact(str(exc))
            raise ConnAuthError(f"[CONN_AUTH] {t('mssql_mcp.conn_failed', error=safe_err)}") from exc

    def abort_connection(self, conn: Any, cursor: Any = None) -> None:
        """截斷大查詢或讀取異常時先嘗試取消游標並直接關閉連線，避免排空剩餘 TDS 封包。"""
        if cursor is not None:
            cancel_fn = getattr(cursor, "cancel", None)
            if callable(cancel_fn):
                try:
                    cancel_fn()
                except Exception:
                    pass
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


MSSQL_ADAPTER = MssqlReadOnlyAdapter()

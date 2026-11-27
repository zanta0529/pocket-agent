"""MySQL / MariaDB 唯讀連線與串流游標轉接器（Adapter Pattern）。"""

import importlib
import os
import ssl
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
    tty_prompt_secret,
    tty_prompt_text,
    validate_host,
    validate_identifier,
    validate_port,
)

_VALID_SSL_MODES = frozenset({"disabled", "preferred", "required", "verify_ca", "verify_identity"})


def load_mysql_driver() -> tuple[str, Any]:
    """延遲載入 MySQL 驅動（優先使用 pymysql，次選 mysql.connector）。"""
    for mod_name, driver_type in (("pymysql", "pymysql"), ("mysql.connector", "mysql_connector")):
        try:
            mod = importlib.import_module(mod_name)
            return driver_type, mod
        except ImportError:
            continue
    raise RuntimeError(t("mysql_mcp.driver_missing"))


def _parse_int_env(name: str, default: int, min_val: int, max_val: int) -> int:
    raw = clean_param(os.getenv(name))
    if not raw:
        return default
    try:
        return max(min_val, min(int(raw), max_val))
    except ValueError:
        return default


def _parse_ssl_config(driver_type: str) -> dict[str, Any]:
    """依 MYSQL_SSL_MODE 與 MYSQL_SSL_CA 組裝對應驅動的 TLS/SSL 連線參數。"""
    raw_mode = clean_param(os.getenv("MYSQL_SSL_MODE")).lower()
    raw_ca = clean_param(os.getenv("MYSQL_SSL_CA"))

    if not raw_mode and not raw_ca:
        return {}

    mode = raw_mode or ("verify_ca" if raw_ca else "preferred")
    if mode not in _VALID_SSL_MODES:
        raise ValueError(t("mysql_mcp.invalid_ssl_mode", mode=mode))

    if raw_ca:
        ca_path = Path(raw_ca).expanduser()
        if not ca_path.is_file():
            raise ValueError(t("mysql_mcp.ssl_ca_not_found", path=raw_ca))
        ca_file = str(ca_path)
    else:
        ca_file = ""

    if mode == "disabled":
        return {"ssl_disabled": True} if driver_type == "mysql_connector" else {}

    if driver_type == "mysql_connector":
        kwargs: dict[str, Any] = {"ssl_disabled": False}
        if ca_file:
            kwargs["ssl_ca"] = ca_file
        if mode in ("verify_ca", "verify_identity"):
            kwargs["ssl_verify_cert"] = True
            kwargs["ssl_verify_identity"] = mode == "verify_identity"
        elif mode == "required":
            kwargs["ssl_verify_cert"] = False
        return kwargs

    ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
    if mode in ("preferred", "required"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif mode == "verify_ca":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
    elif mode == "verify_identity":
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
    return {"ssl": ctx}


class MysqlReadOnlyAdapter(ReadOnlyDatabaseAdapter):
    """封裝 MySQL 憑證快取、唯讀交易連線、伺服器端逾時、TLS 與串流快速中止。"""

    def __init__(
        self,
        *,
        cache: CredentialCache | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self.cache = cache or CredentialCache(ttl_seconds=CONSTANTS.mysql_creds_cache_ttl_sec)
        self.audit = audit or AuditLogger("mysql")

    def clear_cache(self) -> None:
        self.cache.clear()

    def format_audit_target(
        self,
        host: str = "",
        port: str = "",
        user: str = "",
        db: str = "",
    ) -> str:
        h = host.strip() or self.cache.get("MYSQL_HOST") or clean_param(os.getenv("MYSQL_HOST")) or "?"
        p = port.strip() or self.cache.get("MYSQL_PORT") or clean_param(os.getenv("MYSQL_PORT")) or "3306"
        u = user.strip() or self.cache.get("MYSQL_USER") or clean_param(os.getenv("MYSQL_USER")) or "?"
        d = db.strip() or self.cache.get("MYSQL_DATABASE") or clean_param(os.getenv("MYSQL_DATABASE")) or "*"
        return f"{u}@{h}:{p}/{d}"

    def resolve_connection_params(
        self,
        mysql_host: str = "",
        mysql_port: str = "",
        mysql_user: str = "",
        mysql_password: str = "",
        mysql_database: str = "",
    ) -> tuple[str, str, str, str, str]:
        """依「注入參數 → 記憶體快取 → 環境變數 → 終端機互動詢問」順序取得 MySQL 連線參數。"""
        with self.cache.lock:
            cached_host = self.cache.get("MYSQL_HOST")
            cached_port = self.cache.get("MYSQL_PORT")
            cached_user = self.cache.get("MYSQL_USER")
            cached_db = self.cache.get("MYSQL_DATABASE")

            env_host = clean_param(os.getenv("MYSQL_HOST"))
            env_port = clean_param(os.getenv("MYSQL_PORT"))
            env_user = clean_param(os.getenv("MYSQL_USER"))
            env_db = clean_param(os.getenv("MYSQL_DATABASE"))

            host = clean_param(mysql_host) or cached_host or env_host
            port = clean_param(mysql_port) or cached_port or env_port
            user = clean_param(mysql_user) or cached_user or env_user
            database = clean_param(mysql_database) or cached_db or env_db

            if not host:
                host = clean_param(tty_prompt_text(t("mysql_mcp.prompt_host"), hint="MYSQL_*"))
            if not port:
                port = clean_param(tty_prompt_text(t("mysql_mcp.prompt_port"), hint="MYSQL_*")) or "3306"
            if not user:
                user = clean_param(tty_prompt_text(t("mysql_mcp.prompt_user", host=host), hint="MYSQL_*"))

            if not host:
                raise ValueError(t("mysql_mcp.missing_host"))
            if not user:
                raise ValueError(t("mysql_mcp.missing_user"))

            host = validate_host(host, "MYSQL_HOST")
            port = validate_port(port, "MYSQL_PORT")
            user = validate_identifier(user, "MYSQL_USER")
            if database:
                database = validate_identifier(database, "MYSQL_DATABASE")

            norm_env_host = ""
            if env_host:
                try:
                    norm_env_host = validate_host(env_host, "MYSQL_HOST")
                except ValueError:
                    norm_env_host = ""
            norm_env_port = ""
            if env_port:
                try:
                    norm_env_port = validate_port(env_port, "MYSQL_PORT")
                except ValueError:
                    norm_env_port = ""

            target_auth = (host, port, user)
            cached_auth = (cached_host, cached_port, cached_user)
            env_matches = (
                (not norm_env_host or norm_env_host == host)
                and (not norm_env_port or norm_env_port == port)
                and (not env_user or env_user == user)
            )

            password = mysql_password if not is_empty_or_null(mysql_password) else ""
            if not password and target_auth == cached_auth:
                password = self.cache.get_secret("MYSQL_PASSWORD")
            if not password and env_matches:
                env_pw = os.getenv("MYSQL_PASSWORD")
                if not is_empty_or_null(env_pw):
                    password = str(env_pw)

            if not password:
                password = tty_prompt_secret(t("mysql_mcp.prompt_password", user=user, host=host))
            if not password:
                raise ValueError(t("mysql_mcp.missing_password"))

            self.cache.update(
                {
                    "MYSQL_HOST": host,
                    "MYSQL_PORT": port,
                    "MYSQL_USER": user,
                    "MYSQL_DATABASE": database,
                }
            )
            self.cache.set_secret("MYSQL_PASSWORD", password)
            return host, port, user, password, database

    @staticmethod
    def _apply_session_guards(cursor: Any, query_timeout_ms: int) -> None:
        cursor.execute("SET SESSION TRANSACTION READ ONLY;")
        try:
            cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {query_timeout_ms};")
        except Exception:
            try:
                cursor.execute(f"SET SESSION max_statement_time = {query_timeout_ms / 1000:.3f};")
            except Exception:
                pass
        cursor.execute("START TRANSACTION READ ONLY;")

    def connect(
        self,
        mysql_host: str = "",
        mysql_port: str = "",
        mysql_user: str = "",
        mysql_password: str = "",
        mysql_database: str = "",
    ) -> Any:
        """建立遠端 MySQL 連線並強制設定為唯讀交易與伺服器端查詢逾時。"""
        try:
            host, port, user, password, database = self.resolve_connection_params(
                mysql_host, mysql_port, mysql_user, mysql_password, mysql_database
            )
        except ValueError as exc:
            self.clear_cache()
            raise ConnAuthError(f"[CONN_AUTH] {DEFAULT_REDACTION_POLICY.redact(str(exc))}") from exc

        driver_type, driver_mod = load_mysql_driver()
        timeout = _parse_int_env(
            "MYSQL_CONNECT_TIMEOUT",
            CONSTANTS.mysql_default_connect_timeout_sec,
            CONSTANTS.mysql_min_connect_timeout_sec,
            CONSTANTS.mysql_max_connect_timeout_sec,
        )
        query_timeout_ms = _parse_int_env(
            "MYSQL_QUERY_TIMEOUT_MS",
            CONSTANTS.mysql_default_query_timeout_ms,
            CONSTANTS.mysql_min_query_timeout_ms,
            CONSTANTS.mysql_max_query_timeout_ms,
        )

        try:
            ssl_kwargs = _parse_ssl_config(driver_type)
        except ValueError as exc:
            raise ConnAuthError(f"[CONN_AUTH] {DEFAULT_REDACTION_POLICY.redact(str(exc))}") from exc

        charset = clean_param(os.getenv("MYSQL_CHARSET")) or "utf8mb4"
        db_kwarg = {"database": database} if database else {}

        conn = None
        try:
            if driver_type == "pymysql":
                conn = driver_mod.connect(
                    host=host,
                    port=int(port),
                    user=user,
                    password=password,
                    charset=charset,
                    connect_timeout=timeout,
                    read_timeout=timeout,
                    write_timeout=timeout,
                    autocommit=False,
                    **ssl_kwargs,
                    **db_kwarg,
                )
            else:
                conn_kwargs = {
                    "host": host,
                    "port": int(port),
                    "user": user,
                    "password": password,
                    "charset": charset,
                    "connection_timeout": timeout,
                    "read_timeout": timeout,
                    "write_timeout": timeout,
                    "autocommit": False,
                    **ssl_kwargs,
                    **db_kwarg,
                }
                try:
                    conn = driver_mod.connect(**conn_kwargs)
                except TypeError:
                    conn_kwargs.pop("read_timeout", None)
                    conn_kwargs.pop("write_timeout", None)
                    conn = driver_mod.connect(**conn_kwargs)

            cursor = conn.cursor()
            try:
                self._apply_session_guards(cursor, query_timeout_ms)
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
            raise ConnAuthError(f"[CONN_AUTH] {t('mysql_mcp.conn_failed', error=safe_err)}") from exc

    def open_streaming_cursor(self, conn: Any) -> Any:
        """為 SELECT 建立 Unbuffered 串流游標（PyMySQL SSCursor 或 mysql-connector buffered=False）。"""
        driver_type, driver_mod = load_mysql_driver()

        if driver_type == "pymysql":
            ss_cursor_cls = getattr(getattr(driver_mod, "cursors", None), "SSCursor", None)
            if ss_cursor_cls is None:
                ss_cursor_cls = getattr(driver_mod, "SSCursor", None)
            if ss_cursor_cls is not None:
                return conn.cursor(ss_cursor_cls)
            raise RuntimeError(t("mysql_mcp.missing_sscursor"))

        if driver_type == "mysql_connector":
            return conn.cursor(buffered=False)

        raise RuntimeError(t("mysql_mcp.unsupported_driver", driver=driver_type))

    def abort_connection(self, conn: Any, cursor: Any = None) -> None:
        """提前截斷或讀取異常時直接中止一次性連線，避免排空剩餘巨量結果。"""
        if cursor is not None:
            try:
                if hasattr(cursor, "_result"):
                    cursor._result = None
                if hasattr(cursor, "connection"):
                    cursor.connection = None
            except Exception:
                pass
        if conn is None:
            return
        try:
            if hasattr(type(conn), "_force_close"):
                conn._force_close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


MYSQL_ADAPTER = MysqlReadOnlyAdapter()

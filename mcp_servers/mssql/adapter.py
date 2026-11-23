"""Microsoft SQL Server (MSSQL) 唯讀連線轉接器（Adapter Pattern）。"""

import importlib
import os
import re
from typing import Any

from mcp_servers._shared import (
    AuditLogger,
    ConnAuthError,
    CredentialCache,
    ReadOnlyDatabaseAdapter,
    clean_param,
    is_empty_or_null,
    tty_prompt,
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
    raise RuntimeError("尚未安裝 SQL Server 驅動套件。請在終端機執行：pip install pymssql（或 pip install pyodbc）")


def _resolve_odbc_driver(pyodbc_mod: Any) -> str:
    custom = clean_param(os.getenv("MSSQL_DRIVER"))
    if custom:
        if not _DRIVER_RE.match(custom):
            raise ValueError("非法的 MSSQL_DRIVER 驅動名稱格式。")
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


def adapt_placeholders(sql: str, conn: Any) -> str:
    """根據底層連線驅動轉換參數佔位符（pyodbc 使用 ?，pymssql 使用 %s）。"""
    mod_name = type(conn).__module__.lower()
    if "pyodbc" in mod_name:
        return sql.replace("%s", "?")
    return sql


class MssqlReadOnlyAdapter(ReadOnlyDatabaseAdapter):
    """封裝 MSSQL 憑證快取、雙驅動（pymssql / pyodbc）唯讀隔離連線與參數佔位符轉換。"""

    def __init__(
        self,
        *,
        cache: CredentialCache | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self.cache = cache or CredentialCache(ttl_seconds=600.0)
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
                host = clean_param(tty_prompt("請輸入 SQL Server 主機 IP 或位址 (MSSQL_HOST): ", hint="MSSQL_*"))
            if not port:
                port = (
                    clean_param(tty_prompt("請輸入 SQL Server 連接埠 (MSSQL_PORT) [預設 1433]: ", hint="MSSQL_*"))
                    or "1433"
                )
            if not user:
                user = clean_param(tty_prompt(f"請輸入 {host} 的 SQL Server 登入帳號 (MSSQL_USER): ", hint="MSSQL_*"))

            if not host:
                raise ValueError("未提供 SQL Server 主機位址 (MSSQL_HOST)。")
            if not user:
                raise ValueError("未提供 SQL Server 登入帳號 (MSSQL_USER)。")

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
                password = self.cache.get("MSSQL_PASSWORD")
            if not password and env_matches:
                env_pw = os.getenv("MSSQL_PASSWORD")
                if not is_empty_or_null(env_pw):
                    password = str(env_pw)

            if not password:
                password = tty_prompt(
                    f"請輸入 {user}@{host} 的 SQL Server 登入密碼: ",
                    is_password=True,
                    hint="MSSQL_*",
                )
            if not password:
                raise ValueError("未提供 SQL Server 登入密碼 (MSSQL_PASSWORD)。")

            self.cache.update(
                {
                    "MSSQL_HOST": host,
                    "MSSQL_PORT": port,
                    "MSSQL_USER": user,
                    "MSSQL_PASSWORD": password,
                    "MSSQL_DATABASE": database,
                }
            )
            return host, port, user, password, database

    def connect(
        self,
        mssql_host: str = "",
        mssql_port: str = "",
        mssql_user: str = "",
        mssql_password: str = "",
        mssql_database: str = "",
    ) -> Any:
        """建立遠端 SQL Server 連線並強制設定為唯讀交易隔離模式。"""
        try:
            host, port, user, password, database = self.resolve_connection_params(
                mssql_host, mssql_port, mssql_user, mssql_password, mssql_database
            )
        except ValueError as exc:
            self.clear_cache()
            raise ConnAuthError(f"[CONN_AUTH] {exc}") from exc

        driver_type, driver_mod = load_mssql_driver()

        try:
            timeout = max(3, min(int(os.getenv("MSSQL_CONNECT_TIMEOUT") or "10"), 60))
        except ValueError:
            timeout = 10

        db_kwarg = {"database": database} if database else {}

        conn = None
        try:
            if driver_type == "pymssql":
                conn = driver_mod.connect(
                    server=host,
                    port=int(port),
                    user=user,
                    password=password,
                    login_timeout=timeout,
                    timeout=timeout,
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
                conn = driver_mod.connect(conn_str, timeout=timeout, autocommit=False)

            cursor = conn.cursor()
            try:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;")
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
            raise ConnAuthError(f"[CONN_AUTH] SQL Server 連線或認證失敗: {exc}") from exc


MSSQL_ADAPTER = MssqlReadOnlyAdapter()

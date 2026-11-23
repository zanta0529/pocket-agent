"""MySQL / MariaDB 唯讀連線與串流游標轉接器（Adapter Pattern）。"""

import importlib
import os
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


def load_mysql_driver() -> tuple[str, Any]:
    """延遲載入 MySQL 驅動（優先使用 pymysql，次選 mysql.connector）。"""
    for mod_name, driver_type in (("pymysql", "pymysql"), ("mysql.connector", "mysql_connector")):
        try:
            mod = importlib.import_module(mod_name)
            return driver_type, mod
        except ImportError:
            continue
    raise RuntimeError(
        "尚未安裝 MySQL 驅動套件。請在終端機執行：pip install pymysql（或 pip install mysql-connector-python）"
    )


class MysqlReadOnlyAdapter(ReadOnlyDatabaseAdapter):
    """封裝 MySQL 憑證快取、唯讀交易連線、SSCursor/unbuffered 串流與快速中止。"""

    def __init__(
        self,
        *,
        cache: CredentialCache | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self.cache = cache or CredentialCache(ttl_seconds=600.0)
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
                host = clean_param(tty_prompt("請輸入 MySQL/MariaDB 主機 IP 或位址 (MYSQL_HOST): ", hint="MYSQL_*"))
            if not port:
                port = (
                    clean_param(tty_prompt("請輸入 MySQL 連接埠 (MYSQL_PORT) [預設 3306]: ", hint="MYSQL_*")) or "3306"
                )
            if not user:
                user = clean_param(tty_prompt(f"請輸入 {host} 的 MySQL 登入帳號 (MYSQL_USER): ", hint="MYSQL_*"))

            if not host:
                raise ValueError("未提供 MySQL 主機位址 (MYSQL_HOST)。")
            if not user:
                raise ValueError("未提供 MySQL 登入帳號 (MYSQL_USER)。")

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
                password = tty_prompt(f"請輸入 {user}@{host} 的 MySQL 登入密碼: ", is_password=True, hint="MYSQL_*")
            if not password:
                raise ValueError("未提供 MySQL 登入密碼 (MYSQL_PASSWORD)。")

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

    def connect(
        self,
        mysql_host: str = "",
        mysql_port: str = "",
        mysql_user: str = "",
        mysql_password: str = "",
        mysql_database: str = "",
    ) -> Any:
        """建立遠端 MySQL 連線並強制設定為 SESSION TRANSACTION READ ONLY 唯讀模式。"""
        try:
            host, port, user, password, database = self.resolve_connection_params(
                mysql_host, mysql_port, mysql_user, mysql_password, mysql_database
            )
        except ValueError as exc:
            self.clear_cache()
            raise ConnAuthError(f"[CONN_AUTH] {exc}") from exc

        driver_type, driver_mod = load_mysql_driver()

        try:
            timeout = max(3, min(int(os.getenv("MYSQL_CONNECT_TIMEOUT") or "10"), 60))
        except ValueError:
            timeout = 10

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
                    **db_kwarg,
                )
            else:
                conn = driver_mod.connect(
                    host=host,
                    port=int(port),
                    user=user,
                    password=password,
                    charset=charset,
                    connection_timeout=timeout,
                    autocommit=False,
                    **db_kwarg,
                )

            cursor = conn.cursor()
            try:
                cursor.execute("SET SESSION TRANSACTION READ ONLY;")
                cursor.execute("START TRANSACTION READ ONLY;")
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
            raise ConnAuthError(f"[CONN_AUTH] MySQL 連線或認證失敗: {exc}") from exc

    def open_streaming_cursor(self, conn: Any) -> Any:
        """為 SELECT 建立 Unbuffered 串流游標（PyMySQL SSCursor 或 mysql-connector buffered=False）。"""
        driver_type, driver_mod = load_mysql_driver()

        if driver_type == "pymysql":
            ss_cursor_cls = getattr(getattr(driver_mod, "cursors", None), "SSCursor", None)
            if ss_cursor_cls is None:
                ss_cursor_cls = getattr(driver_mod, "SSCursor", None)
            if ss_cursor_cls is not None:
                return conn.cursor(ss_cursor_cls)
            raise RuntimeError(
                "PyMySQL 環境缺少 SSCursor（需 pymysql.cursors.SSCursor），"
                "無法建立串流游標。請確認 pymysql 版本 >= 1.0 或改用 mysql-connector-python。"
            )

        if driver_type == "mysql_connector":
            return conn.cursor(buffered=False)

        raise RuntimeError(f"不支援的 MySQL 驅動類型：{driver_type}")

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

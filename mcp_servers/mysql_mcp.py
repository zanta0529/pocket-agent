"""遠端 MySQL / MariaDB 唯讀查詢與結構檢索 MCP 伺服器。

安全設計原則：
1. 連線強制執行 SET SESSION TRANSACTION READ ONLY 與 START TRANSACTION READ ONLY，
   並於查詢結束時強制 rollback()，嚴密攔截任何寫入與結構變更操作。
2. 語法層級僅允許單一 SELECT 或 WITH (CTE) 查詢，封鎖多重語句（分號注入）、
   INTO OUTFILE / DUMPFILE、FOR UPDATE 及系統設定變更。
3. 串流分批讀取（fetchmany），防範巨量資料表一次性抓取導致記憶體耗盡 (OOM DoS)。
4. 連線資源於 finally 區塊確保關閉釋放，避免佔用遠端資料庫連線池。
5. 內建敏感欄位名稱與內容正則遮罩，防止密碼、Token、金鑰外洩至 LLM 上下文。
6. 支援彈性設定：MYSQL_HOST、MYSQL_PORT、MYSQL_USER、MYSQL_PASSWORD、MYSQL_DATABASE
   可於 mcp_config.yaml 預填、留空、設為 null 或省略；MYSQL_DATABASE 為選填，未設定時可直接探查所有資料庫清單。
"""

import getpass
import importlib
import ipaddress
import os
import re
import sys
import time
import warnings
from pathlib import Path
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

_MCP_DIR = str(Path(__file__).resolve().parent)
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)

import _db_common  # noqa: E402

MAX_OUTPUT_CHARS = _db_common.MAX_OUTPUT_CHARS
DB_HEALTH_MAX_TABLES = _db_common.DB_HEALTH_MAX_TABLES
MYSQL_CHECK_TABLE_MAX_BYTES = _db_common.MYSQL_CHECK_TABLE_MAX_BYTES
DB_FRAGMENTATION_WARN_RATIO = _db_common.DB_FRAGMENTATION_WARN_RATIO
DB_FRAGMENTATION_MIN_FREE_BYTES = _db_common.DB_FRAGMENTATION_MIN_FREE_BYTES
DB_AUTOINC_WARN_RATIO = _db_common.DB_AUTOINC_WARN_RATIO
_SENSITIVE_COL_RE = _db_common.SENSITIVE_COL_RE
_ConnAuthError = _db_common.ConnAuthError
_clean_param = _db_common.clean_param
_find_redundant_prefix_indexes = _db_common.find_redundant_prefix_indexes
_format_bytes = _db_common.format_bytes
_format_cell_value = _db_common.format_cell_value
_get_audit_log_path = _db_common.get_audit_log_path
_is_empty_or_null = _db_common.is_empty_or_null
_redact_output = _db_common.redact_output
_truncate_health_report = _db_common.truncate_health_report
_validate_identifier = _db_common.validate_identifier

mcp = MCPServer("mysql")

# 執行期記憶體快取（僅存活於當前行程生命週期，不落盤）
_RUNTIME_CREDS: dict[str, str] = {}
_CREDS_UPDATED_AT: float = 0.0
_CREDS_TTL_SECONDS: float = 600.0  # 10 分鐘閒置清除

_HOST_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")
_FORBIDDEN_MYSQL_RE = re.compile(
    r"(?i)\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|RENAME"
    r"|GRANT|REVOKE|CALL|EXECUTE|PREPARE|DEALLOCATE|HANDLER|LOAD\s+DATA|LOAD\s+XML"
    r"|INTO\s+OUTFILE|INTO\s+DUMPFILE|INTO\s+@"
    r"|FOR\s+UPDATE|LOCK\s+IN\s+SHARE\s+MODE|LOCK\s+TABLES|UNLOCK\s+TABLES"
    r"|SET\s+GLOBAL|SET\s+SESSION|SET\s+PERSIST|SET\s+@@"
    r")\b|\b(?:SLEEP|BENCHMARK|GET_LOCK|RELEASE_LOCK|RELEASE_ALL_LOCKS)\s*\("
)


def _format_audit_target(host: str = "", port: str = "", user: str = "", db: str = "") -> str:
    h = host.strip() or _RUNTIME_CREDS.get("MYSQL_HOST", "") or _clean_param(os.getenv("MYSQL_HOST")) or "?"
    p = port.strip() or _RUNTIME_CREDS.get("MYSQL_PORT", "") or _clean_param(os.getenv("MYSQL_PORT")) or "3306"
    u = user.strip() or _RUNTIME_CREDS.get("MYSQL_USER", "") or _clean_param(os.getenv("MYSQL_USER")) or "?"
    d = db.strip() or _RUNTIME_CREDS.get("MYSQL_DATABASE", "") or _clean_param(os.getenv("MYSQL_DATABASE")) or "*"
    return f"{u}@{h}:{p}/{d}"


def _audit_log(
    tool_name: str,
    command: str,
    *,
    target: str = "",
    status: str = "OK",
    detail: str = "",
) -> None:
    _db_common.audit_log("mysql", tool_name, command, target=target, status=status, detail=detail)


def _check_creds_ttl() -> None:
    global _CREDS_UPDATED_AT
    now = time.monotonic()
    if _RUNTIME_CREDS and (now - _CREDS_UPDATED_AT > _CREDS_TTL_SECONDS):
        _RUNTIME_CREDS.clear()
        _CREDS_UPDATED_AT = 0.0


def _clear_creds_cache() -> None:
    global _CREDS_UPDATED_AT
    _RUNTIME_CREDS.clear()
    _CREDS_UPDATED_AT = 0.0


def _validate_host(value: str) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-"):
        raise ValueError("非法的 MYSQL_HOST 參數格式。")
    raw_ip = clean.strip("[]")
    try:
        ip = ipaddress.ip_address(raw_ip)
        return str(ip)
    except ValueError:
        pass
    if not _HOST_RE.match(clean):
        raise ValueError("非法的 MYSQL_HOST 參數格式（僅允許合法的 IPv4、IPv6 或主機名稱）。")
    return clean


def _validate_port(value: str) -> str:
    return _db_common.validate_port(value, "MYSQL_PORT")


def _normalize_sql_comments(sql: str) -> str:
    """具引號狀態之 SQL 註解掃描與正規化。

    - 將引號外的一般註解替換為單一空白以保留 token 邊界。
    - 保留引號內（'...'、"..."、`...`）的字面值與註解符號。
    - 拒絕可執行註解（/*! ... */、/*M! ... */）、未閉合引號／註解、
      引號內反斜線跳脫（防範 SQL_MODE 差異）及非標準 '--' 語法。
    """
    out: list[str] = []
    i = 0
    n = len(sql)

    while i < n:
        ch = sql[i]

        # 1. 引號字串或識別字 ('...', "...", `...`)
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(quote)
            i += 1
            closed = False
            while i < n:
                c = sql[i]
                if c == "\\" and quote in ("'", '"'):
                    raise ValueError(
                        "基於安全考量，禁止於引號內使用反斜線跳脫（請改用重複引號），"
                        "以避免不同 SQL_MODE 下引號邊界解析差異。"
                    )
                if c == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        out.append(quote + quote)
                        i += 2
                        continue
                    out.append(quote)
                    i += 1
                    closed = True
                    break
                out.append(c)
                i += 1
            if not closed:
                raise ValueError("查詢包含未閉合的引號。")
            continue

        # 2. 區塊註解 /* ... */ 與可執行註解 /*! ... */、/*M! ... */
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end_idx = sql.find("*/", i + 2)
            if end_idx == -1:
                raise ValueError("查詢包含未閉合的區塊註解。")
            body = sql[i + 2 : end_idx]
            if body.startswith("!") or re.match(r"^[mM]!", body):
                raise ValueError("基於安全考量，禁止使用 MySQL/MariaDB 可執行註解 (Executable Comments)。")
            out.append(" ")
            i = end_idx + 2
            continue

        # 3. 井字單行註解 # ...
        if ch == "#":
            newline_idx = sql.find("\n", i + 1)
            if newline_idx == -1:
                out.append(" ")
                break
            out.append(" ")
            i = newline_idx
            continue

        # 4. 雙連字號單行註解 -- ...
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            if i + 2 < n and not (sql[i + 2].isspace() or ord(sql[i + 2]) <= 32):
                raise ValueError("基於安全考量，SQL 單行註解 '--' 後方必須緊接空白或換行字元。")
            newline_idx = sql.find("\n", i + 2)
            if newline_idx == -1:
                out.append(" ")
                break
            out.append(" ")
            i = newline_idx
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _strip_leading_sql_comments(sql: str) -> str:
    """移除並正規化 SQL 註解，回傳去除首尾空白之語句。"""
    return _normalize_sql_comments(sql).strip()


def _validate_select_query(query: str) -> str:
    """驗證 SQL 查詢是否為純唯讀單一 SELECT / WITH / EXPLAIN 語句。"""
    normalized = _normalize_sql_comments(query).strip()
    if not normalized:
        raise ValueError("查詢語句不可為空。")

    # 移除結尾分號後，若內部仍含分號則拒絕（防止多重語句堆疊注入）
    without_trailing_semi = normalized.rstrip("; \t\r\n").strip()
    if not without_trailing_semi:
        raise ValueError("查詢語句不可為空。")
    if ";" in without_trailing_semi:
        raise ValueError("基於安全考量，禁止執行包含分號之多重 SQL 語句。")

    if not re.match(r"(?i)^(?:EXPLAIN\s+(?:FORMAT\s*=\s*\w+\s+)?)?(?:SELECT|WITH)\b", without_trailing_semi):
        raise ValueError("基於安全考量，僅允許執行以 SELECT、WITH 或 EXPLAIN SELECT 開頭之唯讀查詢。")

    if _FORBIDDEN_MYSQL_RE.search(without_trailing_semi):
        raise ValueError("基於安全考量，查詢包含禁止執行的寫入、鎖定或系統變更關鍵字。")

    return without_trailing_semi


def _tty_prompt(prompt_text: str, *, is_password: bool = False) -> str:
    """當未於設定檔指定連線資訊時，直接透過作業系統終端機設備（CONIN$/CONOUT$ 或 /dev/tty）詢問使用者。"""
    if is_password:
        if os.name == "nt" and sys.stdin is not sys.__stdin__:
            raise ValueError("沒有安全的互動終端機，請由上層注入連線參數。")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                return getpass.getpass(prompt_text)
            except (getpass.GetPassWarning, EOFError, OSError) as exc:
                raise ValueError("無法安全讀取密碼，請由上層注入連線參數。") from exc

    tty_in_name = "CONIN$" if os.name == "nt" else "/dev/tty"
    tty_out_name = "CONOUT$" if os.name == "nt" else "/dev/tty"
    try:
        with (
            open(tty_out_name, "w", encoding="utf-8") as tty_out,
            open(tty_in_name, encoding="utf-8") as tty_in,
        ):
            tty_out.write(prompt_text)
            tty_out.flush()
            return tty_in.readline().strip()
    except OSError as exc:
        raise ValueError(
            "沒有可用的互動終端機，請於環境變數或 mcp_config.yaml 設定 MYSQL_* 連線參數；不讀取 MCP stdin。"
        ) from exc


def _resolve_connection_params(
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> tuple[str, str, str, str, str]:
    """依「注入參數 → 記憶體快取 → 環境變數 → 終端機互動詢問」順序取得 MySQL 連線參數（MYSQL_DATABASE 為選填）。"""
    _check_creds_ttl()
    cached_host = _RUNTIME_CREDS.get("MYSQL_HOST", "")
    cached_port = _RUNTIME_CREDS.get("MYSQL_PORT", "")
    cached_user = _RUNTIME_CREDS.get("MYSQL_USER", "")
    cached_db = _RUNTIME_CREDS.get("MYSQL_DATABASE", "")

    env_host = _clean_param(os.getenv("MYSQL_HOST"))
    env_port = _clean_param(os.getenv("MYSQL_PORT"))
    env_user = _clean_param(os.getenv("MYSQL_USER"))
    env_db = _clean_param(os.getenv("MYSQL_DATABASE"))

    host = _clean_param(mysql_host) or cached_host or env_host
    port = _clean_param(mysql_port) or cached_port or env_port
    user = _clean_param(mysql_user) or cached_user or env_user
    database = _clean_param(mysql_database) or cached_db or env_db

    if not host:
        host = _clean_param(_tty_prompt("請輸入 MySQL/MariaDB 主機 IP 或位址 (MYSQL_HOST): "))
    if not port:
        port = _clean_param(_tty_prompt("請輸入 MySQL 連接埠 (MYSQL_PORT) [預設 3306]: ")) or "3306"
    if not user:
        user = _clean_param(_tty_prompt(f"請輸入 {host} 的 MySQL 登入帳號 (MYSQL_USER): "))

    if not host:
        raise ValueError("未提供 MySQL 主機位址 (MYSQL_HOST)。")
    if not user:
        raise ValueError("未提供 MySQL 登入帳號 (MYSQL_USER)。")

    host = _validate_host(host)
    port = _validate_port(port)
    user = _validate_identifier(user, "MYSQL_USER")
    if database:
        database = _validate_identifier(database, "MYSQL_DATABASE")

    norm_env_host = ""
    if env_host:
        try:
            norm_env_host = _validate_host(env_host)
        except ValueError:
            norm_env_host = ""
    norm_env_port = ""
    if env_port:
        try:
            norm_env_port = _validate_port(env_port)
        except ValueError:
            norm_env_port = ""

    target_auth = (host, port, user)
    cached_auth = (cached_host, cached_port, cached_user)
    env_matches = (
        (not norm_env_host or norm_env_host == host)
        and (not norm_env_port or norm_env_port == port)
        and (not env_user or env_user == user)
    )

    password = mysql_password if not _is_empty_or_null(mysql_password) else ""
    if not password and target_auth == cached_auth:
        password = _RUNTIME_CREDS.get("MYSQL_PASSWORD", "")
    if not password and env_matches:
        env_pw = os.getenv("MYSQL_PASSWORD")
        if not _is_empty_or_null(env_pw):
            password = str(env_pw)

    if not password:
        password = _tty_prompt(f"請輸入 {user}@{host} 的 MySQL 登入密碼: ", is_password=True)
    if not password:
        raise ValueError("未提供 MySQL 登入密碼 (MYSQL_PASSWORD)。")

    _RUNTIME_CREDS["MYSQL_HOST"] = host
    _RUNTIME_CREDS["MYSQL_PORT"] = port
    _RUNTIME_CREDS["MYSQL_USER"] = user
    _RUNTIME_CREDS["MYSQL_PASSWORD"] = password
    _RUNTIME_CREDS["MYSQL_DATABASE"] = database
    global _CREDS_UPDATED_AT
    _CREDS_UPDATED_AT = time.monotonic()
    return host, port, user, password, database


def _load_mysql_driver() -> tuple[str, Any]:
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


def _get_readonly_connection(
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> Any:
    """建立遠端 MySQL 連線並強制設定為 SESSION TRANSACTION READ ONLY 唯讀模式。"""
    try:
        host, port, user, password, database = _resolve_connection_params(
            mysql_host, mysql_port, mysql_user, mysql_password, mysql_database
        )
    except ValueError as exc:
        _clear_creds_cache()
        raise _ConnAuthError(f"[CONN_AUTH] {exc}") from exc

    # 驅動載入置於連線區塊外，避免未安裝驅動套件時誤清除已快取的連線帳密
    driver_type, driver_mod = _load_mysql_driver()

    try:
        timeout = max(3, min(int(os.getenv("MYSQL_CONNECT_TIMEOUT") or "10"), 60))
    except ValueError:
        timeout = 10

    charset = _clean_param(os.getenv("MYSQL_CHARSET")) or "utf8mb4"
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
        _clear_creds_cache()
        raise _ConnAuthError(f"[CONN_AUTH] MySQL 連線或認證失敗: {exc}") from exc


def _close_safely(conn: Any) -> None:
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


def _open_streaming_cursor(conn: Any) -> Any:
    """為 SELECT 建立 Unbuffered 串流 Cursor，防止大型查詢預先載入全表。

    僅支援具備真正串流能力的驅動組合：
    - PyMySQL + SSCursor (pymysql.cursors.SSCursor)
    - mysql-connector-python + buffered=False

    若環境不滿足上述條件，拋出 RuntimeError 而非靜默退回 buffered cursor。
    """
    driver_type, driver_mod = _load_mysql_driver()

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

    elif driver_type == "mysql_connector":
        # mysql-connector-python 官方支援 buffered=False
        return conn.cursor(buffered=False)

    # 理論上不會到達（_load_mysql_driver 只會回傳這兩種）
    raise RuntimeError(f"不支援的 MySQL 驅動類型：{driver_type}")


def _abort_unbuffered_connection(conn: Any, cursor: Any = None) -> None:
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


@mcp.tool()
def mysql_list_databases(
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """列出遠端 MySQL/MariaDB 伺服器上目前帳號可存取的所有資料庫 (Databases/Schemas) 清單"""
    conn = None
    sql = "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA ORDER BY SCHEMA_NAME;"
    try:
        conn = _get_readonly_connection(mysql_host, mysql_port, mysql_user, mysql_password, mysql_database)
        target = _format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database)
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            dbs = cursor.fetchall()
        finally:
            cursor.close()

        _audit_log("mysql_list_databases", sql, target=target, status="OK", detail=f"databases={len(dbs)}")
        if not dbs:
            return "查無可見的資料庫。"
        return "\n".join(str(row[0]) for row in dbs)
    except _ConnAuthError as e:
        _audit_log(
            "mysql_list_databases",
            sql,
            target=_format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database),
            status="AUTH_FAIL",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    except Exception as e:
        _audit_log(
            "mysql_list_databases",
            sql,
            target=_format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    finally:
        _close_safely(conn)


@mcp.tool()
def mysql_list_tables(
    database: str = "",
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """列出遠端 MySQL/MariaDB 目標資料庫中的所有非系統資料表（未指定 database 時自動列出所有使用者資料庫之資料表）"""
    conn = None
    sql_executed = "SELECT ... FROM INFORMATION_SCHEMA.TABLES"
    try:
        explicit_db = _clean_param(database)
        if explicit_db:
            try:
                explicit_db = _validate_identifier(explicit_db, "database")
            except ValueError as e:
                _audit_log(
                    "mysql_list_tables",
                    f"database={database!r}",
                    target=_format_audit_target(mysql_host, mysql_port, mysql_user, database),
                    status="BLOCKED",
                    detail=str(e),
                )
                return f"[ERROR] {e}"

        conn = _get_readonly_connection(mysql_host, mysql_port, mysql_user, mysql_password, mysql_database)
        target_db = (
            explicit_db
            or _clean_param(mysql_database)
            or _RUNTIME_CREDS.get("MYSQL_DATABASE", "")
            or _clean_param(os.getenv("MYSQL_DATABASE"))
        )
        target = _format_audit_target(mysql_host, mysql_port, mysql_user, target_db)

        cursor = conn.cursor()
        try:
            if target_db:
                sql_executed = (
                    "SELECT TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE IN ('BASE TABLE', 'VIEW') "
                    "ORDER BY TABLE_NAME;"
                )
                cursor.execute(sql_executed, (target_db,))
                tables = cursor.fetchall()
                _audit_log(
                    "mysql_list_tables",
                    f"{sql_executed} [database={target_db!r}]",
                    target=target,
                    status="OK",
                    detail=f"tables={len(tables)}",
                )
                if not tables:
                    return "資料庫中沒有資料表。"
                return "\n".join(
                    f"{t[0]} (VIEW)" if len(t) > 1 and str(t[1]).upper() == "VIEW" else str(t[0]) for t in tables
                )

            sql_executed = (
                "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW') "
                "AND TABLE_SCHEMA NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys') "
                "ORDER BY TABLE_SCHEMA, TABLE_NAME;"
            )
            cursor.execute(sql_executed)
            tables = cursor.fetchall()
            _audit_log(
                "mysql_list_tables",
                sql_executed,
                target=target,
                status="OK",
                detail=f"tables={len(tables)}",
            )
        finally:
            cursor.close()

        if not tables:
            return "資料庫中沒有資料表。"
        out_lines = []
        for t in tables:
            base = f"{t[0]}.{t[1]}" if len(t) >= 2 else str(t[0])
            if len(t) >= 3 and str(t[2]).upper() == "VIEW":
                base += " (VIEW)"
            out_lines.append(base)
        return "\n".join(out_lines)
    except _ConnAuthError as e:
        _audit_log(
            "mysql_list_tables",
            sql_executed,
            target=_format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="AUTH_FAIL",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    except Exception as e:
        _audit_log(
            "mysql_list_tables",
            sql_executed,
            target=_format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    finally:
        _close_safely(conn)


@mcp.tool()
def mysql_describe_schema(
    table_name: str,
    database: str = "",
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """顯示指定 MySQL/MariaDB 資料表的 Schema (欄位、型別、主鍵與 NOT NULL 約束)"""
    conn = None
    sql_executed = f"DESCRIBE {table_name}"
    try:
        clean_name = table_name.strip().strip("'\"`")
        if not clean_name:
            _audit_log(
                "mysql_describe_schema",
                "table_name=''",
                target=_format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
                status="BLOCKED",
                detail="缺少 table_name 參數",
            )
            return "[ERROR] 缺少 table_name 參數。"

        explicit_db = _clean_param(database)
        try:
            if "." in clean_name:
                schema_part, tbl_part = clean_name.split(".", 1)
                explicit_schema = _validate_identifier(schema_part.strip("`'\""), "schema_name")
                target_table = _validate_identifier(tbl_part.strip("`'\""), "table_name")
            else:
                explicit_schema = _validate_identifier(explicit_db, "database") if explicit_db else ""
                target_table = _validate_identifier(clean_name, "table_name")
        except ValueError as e:
            _audit_log(
                "mysql_describe_schema",
                f"table_name={table_name!r} database={database!r}",
                target=_format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
                status="BLOCKED",
                detail=str(e),
            )
            return f"[ERROR] {e}"

        conn = _get_readonly_connection(mysql_host, mysql_port, mysql_user, mysql_password, mysql_database)
        target_schema = (
            explicit_schema
            or _clean_param(mysql_database)
            or _RUNTIME_CREDS.get("MYSQL_DATABASE", "")
            or _clean_param(os.getenv("MYSQL_DATABASE"))
        )
        target = _format_audit_target(mysql_host, mysql_port, mysql_user, target_schema)

        cursor = conn.cursor()
        idx_rows: list[Any] = []
        fk_rows: list[Any] = []
        try:
            if target_schema:
                sql_executed = (
                    "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_KEY "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                    "ORDER BY ORDINAL_POSITION;"
                )
                cursor.execute(sql_executed, (target_schema, target_table))
            else:
                sql_executed = (
                    "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_KEY "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_NAME = %s "
                    "AND TABLE_SCHEMA NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys') "
                    "ORDER BY TABLE_SCHEMA, ORDINAL_POSITION;"
                )
                cursor.execute(sql_executed, (target_table,))
            columns = cursor.fetchall()

            if columns:
                try:
                    if target_schema:
                        idx_sql = (
                            "SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME "
                            "FROM INFORMATION_SCHEMA.STATISTICS "
                            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                            "ORDER BY INDEX_NAME, SEQ_IN_INDEX;"
                        )
                        cursor.execute(idx_sql, (target_schema, target_table))
                    else:
                        idx_sql = (
                            "SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME "
                            "FROM INFORMATION_SCHEMA.STATISTICS "
                            "WHERE TABLE_NAME = %s "
                            "AND TABLE_SCHEMA NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys') "
                            "ORDER BY INDEX_NAME, SEQ_IN_INDEX;"
                        )
                        cursor.execute(idx_sql, (target_table,))
                    fetched_idx = cursor.fetchall()
                    if fetched_idx and fetched_idx != columns:
                        idx_rows = list(fetched_idx)

                    if target_schema:
                        fk_sql = (
                            "SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                            "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                            "AND REFERENCED_TABLE_NAME IS NOT NULL "
                            "ORDER BY ORDINAL_POSITION;"
                        )
                        cursor.execute(fk_sql, (target_schema, target_table))
                    else:
                        fk_sql = (
                            "SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                            "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                            "WHERE TABLE_NAME = %s AND REFERENCED_TABLE_NAME IS NOT NULL "
                            "AND TABLE_SCHEMA NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys') "
                            "ORDER BY ORDINAL_POSITION;"
                        )
                        cursor.execute(fk_sql, (target_table,))
                    fetched_fk = cursor.fetchall()
                    if fetched_fk and fetched_fk != columns:
                        fk_rows = list(fetched_fk)
                except Exception:
                    pass
        finally:
            cursor.close()

        if not columns:
            _audit_log(
                "mysql_describe_schema",
                f"{sql_executed} [table={target_table!r}]",
                target=target,
                status="NOT_FOUND",
                detail=f"找不到資料表 {clean_name}",
            )
            return f"找不到資料表 {clean_name}。"

        _audit_log(
            "mysql_describe_schema",
            f"{sql_executed} [table={target_table!r}]",
            target=target,
            status="OK",
            detail=f"columns={len(columns)}",
        )
        lines = []
        for c in columns:
            col_name = str(c[0])
            col_type = str(c[1] or "TEXT").upper()
            is_nullable = str(c[2] or "").upper()
            col_default = c[3]
            col_key = str(c[4] or "").upper()
            pk_tag = " [PK]" if col_key == "PRI" else ""
            null_tag = " NOT NULL" if is_nullable == "NO" else ""
            default_tag = f" DEFAULT {col_default}" if col_default is not None else ""
            lines.append(f"{col_name} ({col_type}{pk_tag}{null_tag}{default_tag})")

        if idx_rows:
            idx_map: dict[str, tuple[bool, list[str]]] = {}
            for r in idx_rows:
                if len(r) >= 3:
                    iname = str(r[0])
                    is_uniq = not bool(int(r[1])) if str(r[1]).isdigit() else not bool(r[1])
                    cname = str(r[2])
                    if iname not in idx_map:
                        idx_map[iname] = (is_uniq, [])
                    idx_map[iname][1].append(cname)
            if idx_map:
                lines.append("\n[Indexes]")
                for iname, (is_uniq, cols) in idx_map.items():
                    u_tag = "UNIQUE " if is_uniq else ""
                    lines.append(f"- {iname}: {u_tag}({', '.join(cols)})")

        if fk_rows:
            fk_lines = [f"- {r[0]} -> {r[1]}.{r[2]}" for r in fk_rows if len(r) >= 3]
            if fk_lines:
                lines.append("\n[Foreign Keys]")
                lines.extend(fk_lines)

        return "\n".join(lines)
    except _ConnAuthError as e:
        _audit_log(
            "mysql_describe_schema",
            sql_executed,
            target=_format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="AUTH_FAIL",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    except Exception as e:
        _audit_log(
            "mysql_describe_schema",
            sql_executed,
            target=_format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    finally:
        _close_safely(conn)


@mcp.tool()
def mysql_run_select_query(
    query: str,
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """於遠端 MySQL/MariaDB 執行唯讀的 SELECT 或 WITH 查詢 (具備串流防爆與機密欄位遮罩)"""
    try:
        clean_query = _validate_select_query(query)
    except ValueError as e:
        _audit_log(
            "mysql_run_select_query",
            query,
            target=_format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database),
            status="BLOCKED",
            detail=str(e),
        )
        return f"[ERROR] {e}"

    conn = None
    cursor = None
    stream_completed = False
    try:
        conn = _get_readonly_connection(mysql_host, mysql_port, mysql_user, mysql_password, mysql_database)
        target = _format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database)
        cursor = _open_streaming_cursor(conn)
        cursor.execute(clean_query)
        if not cursor.description:
            stream_completed = True
            _audit_log("mysql_run_select_query", clean_query, target=target, status="OK", detail="rows=0")
            return "查詢未產生欄位結果。"

        col_names = [str(description[0]) for description in cursor.description]
        sensitive_indices = {idx for idx, name in enumerate(col_names) if _SENSITIVE_COL_RE.search(name)}

        result = [", ".join(col_names)]
        chars_count = len(result[0]) + 1
        total_rows = 0
        truncated = False

        batch_size = 100
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                stream_completed = True
                break
            for row in batch:
                total_rows += 1
                formatted_vals = [
                    _format_cell_value(val, is_sensitive=(idx in sensitive_indices)) for idx, val in enumerate(row)
                ]
                row_str = ", ".join(formatted_vals)
                if chars_count + len(row_str) + 1 > MAX_OUTPUT_CHARS:
                    truncated = True
                    break
                result.append(row_str)
                chars_count += len(row_str) + 1
            if truncated:
                break

        if total_rows == 0:
            _audit_log("mysql_run_select_query", clean_query, target=target, status="OK", detail="rows=0")
            return "查詢結果為空。"

        shown_rows = len(result) - 1
        _audit_log(
            "mysql_run_select_query",
            clean_query,
            target=target,
            status="TRUNCATED" if truncated else "OK",
            detail=f"rows={shown_rows}",
        )
        output = "\n".join(result)
        if truncated:
            output += (
                f"\n\n... (查詢結果已截斷，達單次上限 {MAX_OUTPUT_CHARS} 字元，"
                f"已呈現前 {shown_rows} 筆紀錄)\n"
                "[提示：若需檢視特定區段資料，請在 SQL 中加入 WHERE 條件或 LIMIT/OFFSET 分頁]"
            )
        return output
    except _ConnAuthError as e:
        _audit_log(
            "mysql_run_select_query",
            clean_query,
            target=_format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database),
            status="AUTH_FAIL",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    except Exception as e:
        _audit_log(
            "mysql_run_select_query",
            clean_query,
            target=_format_audit_target(mysql_host, mysql_port, mysql_user, mysql_database),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] 查詢失敗: {e}"
    finally:
        if stream_completed:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            _close_safely(conn)
        else:
            _abort_unbuffered_connection(conn, cursor)


def _mysql_int_type_max(column_type: str) -> int | None:
    """根據 MySQL 整數欄位型別字串（含 unsigned）回傳理論最大正整數值。"""
    t = (column_type or "").strip().lower()
    unsigned = "unsigned" in t
    if t.startswith("tinyint"):
        return 255 if unsigned else 127
    if t.startswith("smallint"):
        return 65535 if unsigned else 32767
    if t.startswith("mediumint"):
        return 16777215 if unsigned else 8388607
    if t.startswith(("int", "integer")):
        return 4294967295 if unsigned else 2147483647
    if t.startswith("bigint"):
        return 18446744073709551615 if unsigned else 9223372036854775807
    return None


def _parse_mysql_target(table_name: str, database: str) -> tuple[str, str]:
    """解析並驗證選填或必填之 database 與 table_name（支援 schema.table 格式）。"""
    clean_tbl = (table_name or "").strip().strip("'\"`")
    clean_db = _clean_param(database)
    if clean_tbl and "." in clean_tbl:
        schema_part, tbl_part = clean_tbl.split(".", 1)
        validated_schema = _validate_identifier(schema_part.strip("`'\""), "schema_name")
        validated_tbl = _validate_identifier(tbl_part.strip("`'\""), "table_name")
        return validated_schema, validated_tbl
    validated_schema = _validate_identifier(clean_db, "database") if clean_db else ""
    validated_tbl = _validate_identifier(clean_tbl, "table_name") if clean_tbl else ""
    return validated_schema, validated_tbl


@mcp.tool()
def mysql_inspect_health(
    database: str = "",
    table_name: str = "",
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """檢視遠端 MySQL/MariaDB 資料庫或指定資料表的結構與空間健康狀態（零鎖表 INFORMATION_SCHEMA 巡檢）"""
    conn = None
    sql_executed = "SELECT ... FROM INFORMATION_SCHEMA.TABLES"
    try:
        try:
            explicit_schema, target_table = _parse_mysql_target(table_name, database)
        except ValueError as e:
            _audit_log(
                "mysql_inspect_health",
                f"database={database!r} table_name={table_name!r}",
                target=_format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
                status="BLOCKED",
                detail=str(e),
            )
            return f"[ERROR] {e}"

        conn = _get_readonly_connection(mysql_host, mysql_port, mysql_user, mysql_password, mysql_database)
        target_schema = (
            explicit_schema
            or _clean_param(mysql_database)
            or _RUNTIME_CREDS.get("MYSQL_DATABASE", "")
            or _clean_param(os.getenv("MYSQL_DATABASE"))
        )
        target = _format_audit_target(mysql_host, mysql_port, mysql_user, target_schema)

        where_clauses = ["TABLE_TYPE IN ('BASE TABLE', 'VIEW')"]
        params: list[Any] = []
        if target_schema:
            where_clauses.append("TABLE_SCHEMA = %s")
            params.append(target_schema)
        else:
            where_clauses.append("TABLE_SCHEMA NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys')")
        if target_table:
            where_clauses.append("TABLE_NAME = %s")
            params.append(target_table)

        where_sql = " AND ".join(where_clauses)
        limit_n = int(DB_HEALTH_MAX_TABLES)
        sql_executed = (
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, ENGINE, ROW_FORMAT, "
            "TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH, DATA_FREE, "
            "AUTO_INCREMENT, TABLE_COLLATION, CHECK_TIME "
            f"FROM INFORMATION_SCHEMA.TABLES WHERE {where_sql} "
            "ORDER BY (COALESCE(DATA_LENGTH, 0) + COALESCE(INDEX_LENGTH, 0)) DESC, "
            f"TABLE_SCHEMA, TABLE_NAME LIMIT {limit_n};"
        )

        cursor = conn.cursor()
        col_rows: list[Any] = []
        idx_rows: list[Any] = []
        try:
            cursor.execute(sql_executed, tuple(params))
            tables = cursor.fetchall()
            if tables:
                try:
                    col_where = [c for c in where_clauses if not c.startswith("TABLE_TYPE")]
                    col_where_sql = " AND ".join(col_where) if col_where else "1=1"
                    col_sql = (
                        "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY, EXTRA "
                        f"FROM INFORMATION_SCHEMA.COLUMNS WHERE {col_where_sql};"
                    )
                    cursor.execute(col_sql, tuple(params))
                    fetched_cols = cursor.fetchall()
                    if fetched_cols and fetched_cols != tables:
                        col_rows = list(fetched_cols)

                    idx_sql = (
                        "SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, NON_UNIQUE, COLUMN_NAME "
                        f"FROM INFORMATION_SCHEMA.STATISTICS WHERE {col_where_sql} "
                        "ORDER BY TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX;"
                    )
                    cursor.execute(idx_sql, tuple(params))
                    fetched_idx = cursor.fetchall()
                    if fetched_idx and fetched_idx != tables:
                        idx_rows = list(fetched_idx)
                except Exception:
                    pass
        finally:
            cursor.close()

        if not tables:
            _audit_log(
                "mysql_inspect_health",
                sql_executed,
                target=target,
                status="NOT_FOUND",
                detail="查無符合條件的資料表",
            )
            return "查無符合條件的資料表。"

        _audit_log(
            "mysql_inspect_health",
            sql_executed,
            target=target,
            status="OK",
            detail=f"tables={len(tables)}",
        )

        # 彙整欄位 PK 與自增型別
        pk_tables: set[tuple[str, str]] = set()
        autoinc_col_types: dict[tuple[str, str], tuple[str, str]] = {}
        for c in col_rows:
            if len(c) >= 6:
                key_pair = (str(c[0]), str(c[1]))
                col_name = str(c[2])
                col_type = str(c[3] or "")
                col_key = str(c[4] or "").upper()
                extra = str(c[5] or "").lower()
                if col_key == "PRI":
                    pk_tables.add(key_pair)
                if "auto_increment" in extra:
                    autoinc_col_types[key_pair] = (col_name, col_type)

        # 彙整索引結構供冗餘前綴分析
        table_idx_map: dict[tuple[str, str], dict[str, tuple[bool, list[str]]]] = {}
        for r in idx_rows:
            if len(r) >= 5:
                key_pair = (str(r[0]), str(r[1]))
                iname = str(r[2])
                is_uniq = not bool(int(r[3])) if str(r[3]).isdigit() else not bool(r[3])
                cname = str(r[4])
                t_map = table_idx_map.setdefault(key_pair, {})
                if iname not in t_map:
                    t_map[iname] = (is_uniq, [])
                t_map[iname][1].append(cname)

        base_count = 0
        view_count = 0
        total_used_bytes = 0
        total_free_bytes = 0
        overview_lines: list[str] = []
        warnings_list: list[str] = []

        for t in tables:
            t_schema = str(t[0])
            t_name = str(t[1])
            t_type = str(t[2] or "BASE TABLE").upper()
            engine = str(t[3] or "-").upper()
            row_fmt = str(t[4] or "-")
            est_rows = int(t[5] or 0)
            data_len = int(t[6] or 0)
            idx_len = int(t[7] or 0)
            data_free = int(t[8] or 0)
            auto_inc = t[9]
            collation = str(t[10] or "-")
            key_pair = (t_schema, t_name)
            display_name = t_name if target_schema else f"{t_schema}.{t_name}"

            if t_type == "VIEW":
                view_count += 1
                overview_lines.append(f"- {display_name}: VIEW (Collation={collation})")
                continue

            base_count += 1
            used_b = data_len + idx_len
            total_used_bytes += used_b
            total_free_bytes += data_free
            overview_lines.append(
                f"- {display_name}: Engine={engine}, Format={row_fmt}, "
                f"Rows~{est_rows}, Size={_format_bytes(used_b)} "
                f"(Data={_format_bytes(data_len)}, Index={_format_bytes(idx_len)}), "
                f"Free={_format_bytes(data_free)}"
            )

            if engine not in ("INNODB", "-"):
                warnings_list.append(f"[Engine] {display_name} 使用 {engine} 引擎（非 InnoDB，留意交易與表級鎖限制）")
            if col_rows and key_pair not in pk_tables:
                warnings_list.append(
                    f"[Missing PK] {display_name} 缺少主鍵 (Primary Key)，可能影響 InnoDB 叢集索引效能與複製穩定度"
                )
            if data_free >= DB_FRAGMENTATION_MIN_FREE_BYTES and (used_b + data_free) > 0:
                frag_ratio = data_free / (used_b + data_free)
                if frag_ratio >= DB_FRAGMENTATION_WARN_RATIO:
                    warnings_list.append(
                        f"[Fragmentation] {display_name} 碎片空間達 {_format_bytes(data_free)} "
                        f"({frag_ratio * 100:.1f}%)，建議於離峰評估 OPTIMIZE TABLE"
                    )
            if auto_inc is not None and int(auto_inc) > 0:
                col_info = autoinc_col_types.get(key_pair)
                max_val = _mysql_int_type_max(col_info[1]) if col_info else None
                if max_val:
                    ratio = int(auto_inc) / max_val
                    if ratio >= DB_AUTOINC_WARN_RATIO:
                        warnings_list.append(
                            f"[AutoIncrement] {display_name}.{col_info[0]} ({col_info[1]}) "
                            f"水位已達 {ratio * 100:.1f}% ({auto_inc}/{max_val})，有溢位風險"
                        )
            if key_pair in table_idx_map:
                for red_msg in _find_redundant_prefix_indexes(table_idx_map[key_pair]):
                    warnings_list.append(f"[Redundant Index] {display_name}: {red_msg}")

        report: list[str] = [
            "[MySQL Health Summary]",
            f"巡檢範圍: {target_schema or '所有使用者資料庫'}" + (f" / 資料表: {target_table}" if target_table else ""),
            f"資料表數: {base_count} 張實體表, {view_count} 個檢視表",
            f"總占用空間 (Data + Index): {_format_bytes(total_used_bytes)}",
            f"總未釋放碎片空間 (Data Free): {_format_bytes(total_free_bytes)}",
            "",
            "[Health Warnings]",
        ]
        if warnings_list:
            report.extend(f"⚠️ {w}" for w in warnings_list)
        else:
            report.append("✓ 未發現明顯結構、主鍵、碎片或索引健康風險。")

        report.append("")
        report.append("[Table Overview]")
        report.extend(overview_lines)
        return _truncate_health_report(report, MAX_OUTPUT_CHARS)
    except _ConnAuthError as e:
        _audit_log(
            "mysql_inspect_health",
            sql_executed,
            target=_format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="AUTH_FAIL",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    except Exception as e:
        _audit_log(
            "mysql_inspect_health",
            sql_executed,
            target=_format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] 健康檢查失敗: {e}"
    finally:
        _close_safely(conn)


@mcp.tool()
def mysql_check_table(
    table_name: str,
    database: str = "",
    check_mode: str = "QUICK",
    mysql_host: str = "",
    mysql_port: str = "",
    mysql_user: str = "",
    mysql_password: str = "",
    mysql_database: str = "",
) -> str:
    """對指定 MySQL/MariaDB 單一資料表執行輕量級 CHECK TABLE 檢查（僅限 QUICK/FAST，具 2GB 容量保護）"""
    conn = None
    sql_executed = f"CHECK TABLE {table_name}"
    try:
        clean_name = (table_name or "").strip().strip("'\"`")
        if not clean_name:
            _audit_log(
                "mysql_check_table",
                "table_name=''",
                target=_format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
                status="BLOCKED",
                detail="缺少 table_name 參數",
            )
            return "[ERROR] 缺少 table_name 參數。"

        mode_upper = (check_mode or "QUICK").strip().upper()
        if mode_upper not in ("QUICK", "FAST"):
            msg = "基於正式環境穩定度考量，check_mode 僅允許 'QUICK' 或 'FAST'（禁止 EXTENDED、MEDIUM、FOR UPGRADE）。"
            _audit_log(
                "mysql_check_table",
                f"table_name={table_name!r} check_mode={check_mode!r}",
                target=_format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
                status="BLOCKED",
                detail=msg,
            )
            return f"[ERROR] {msg}"

        try:
            explicit_schema, target_table = _parse_mysql_target(clean_name, database)
        except ValueError as e:
            _audit_log(
                "mysql_check_table",
                f"table_name={table_name!r} database={database!r}",
                target=_format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
                status="BLOCKED",
                detail=str(e),
            )
            return f"[ERROR] {e}"

        conn = _get_readonly_connection(mysql_host, mysql_port, mysql_user, mysql_password, mysql_database)
        target_schema = (
            explicit_schema
            or _clean_param(mysql_database)
            or _RUNTIME_CREDS.get("MYSQL_DATABASE", "")
            or _clean_param(os.getenv("MYSQL_DATABASE"))
        )
        if not target_schema:
            _audit_log(
                "mysql_check_table",
                f"table_name={target_table!r}",
                target=_format_audit_target(mysql_host, mysql_port, mysql_user, ""),
                status="BLOCKED",
                detail="未指定 database 或 MYSQL_DATABASE",
            )
            return "[ERROR] 執行 CHECK TABLE 必須指定目標資料庫（請傳入 database 參數或設定 schema.table）。"

        target = _format_audit_target(mysql_host, mysql_port, mysql_user, target_schema)
        qualified_display = f"{target_schema}.{target_table}"
        qualified_sql = f"`{target_schema}`.`{target_table}`"

        cursor = conn.cursor()
        try:
            # 1. 事前查詢詮釋資料，攔截 VIEW 與超過 2GB 的大型資料表
            meta_sql = (
                "SELECT TABLE_TYPE, COALESCE(DATA_LENGTH, 0) + COALESCE(INDEX_LENGTH, 0) "
                "FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s;"
            )
            cursor.execute(meta_sql, (target_schema, target_table))
            meta_row = cursor.fetchone()
            if not meta_row:
                _audit_log(
                    "mysql_check_table",
                    f"{meta_sql} [table={qualified_display}]",
                    target=target,
                    status="NOT_FOUND",
                    detail=f"找不到資料表 {qualified_display}",
                )
                return f"找不到資料表 {qualified_display}。"

            tbl_type = str(meta_row[0] or "BASE TABLE").upper()
            total_bytes = int(meta_row[1] or 0)
            if tbl_type == "VIEW":
                _audit_log(
                    "mysql_check_table",
                    f"SKIP VIEW {qualified_display}",
                    target=target,
                    status="SKIPPED",
                    detail="VIEW 不適用實體頁檢查",
                )
                return f"[SKIPPED] {qualified_display} 為 VIEW（檢視表），不適用實體資料頁 CHECK TABLE 檢查。"

            if total_bytes > MYSQL_CHECK_TABLE_MAX_BYTES:
                limit_str = _format_bytes(MYSQL_CHECK_TABLE_MAX_BYTES)
                size_str = _format_bytes(total_bytes)
                _audit_log(
                    "mysql_check_table",
                    f"SIZE_GUARD {qualified_display} ({size_str} > {limit_str})",
                    target=target,
                    status="BLOCKED",
                    detail=f"size={total_bytes} exceeds limit={MYSQL_CHECK_TABLE_MAX_BYTES}",
                )
                return (
                    f"[BLOCKED] 資料表 {qualified_display} 總容量 ({size_str}) 超過安全檢查上限 ({limit_str})；"
                    "為防止正式環境磁碟 I/O 尖峰與讀取鎖阻塞，已拒絕執行 CHECK TABLE。"
                    "請改用 mysql_inspect_health 進行零鎖表健康巡檢。"
                )

            # 2. 執行受控之輕量級 CHECK TABLE（注意：CHECK TABLE 會觸發 MySQL 隱式提交，完成後立即關閉連線）
            sql_executed = f"CHECK TABLE {qualified_sql} {mode_upper};"
            cursor.execute(sql_executed)
            check_rows = cursor.fetchall()
        finally:
            cursor.close()

        _audit_log(
            "mysql_check_table",
            sql_executed,
            target=target,
            status="OK",
            detail=f"mode={mode_upper} rows={len(check_rows)}",
        )
        lines = [
            f"[MySQL CHECK TABLE Result ({mode_upper})]",
            f"目標資料表: {qualified_display} (容量: {_format_bytes(total_bytes)})",
        ]
        for r in check_rows:
            if len(r) >= 4:
                lines.append(f"- [{r[2]}] {r[0]} ({r[1]}): {_redact_output(str(r[3]))}")
            else:
                lines.append("- " + ", ".join(_redact_output(str(c)) for c in r))
        return "\n".join(lines)
    except _ConnAuthError as e:
        _audit_log(
            "mysql_check_table",
            sql_executed,
            target=_format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="AUTH_FAIL",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    except Exception as e:
        _audit_log(
            "mysql_check_table",
            sql_executed,
            target=_format_audit_target(mysql_host, mysql_port, mysql_user, database or mysql_database),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] CHECK TABLE 執行失敗: {e}"
    finally:
        _close_safely(conn)


if __name__ == "__main__":
    mcp.run()

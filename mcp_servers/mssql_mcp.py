"""遠端 Microsoft SQL Server (MSSQL) 唯讀查詢與結構檢索 MCP 伺服器。

安全設計原則：
1. 連線啟用唯讀交易隔離（autocommit=False、ApplicationIntent=ReadOnly、READ UNCOMMITTED），
   並於查詢結束時強制 rollback() 絕不 commit()，嚴密阻絕寫入與結構異動。
2. 語法層級僅允許單一 SELECT 或 WITH (CTE) 查詢，封鎖多重語句（分號注入）、
   SELECT ... INTO、EXEC / EXECUTE、xp_cmdshell、OPENROWSET、BULK 等危險指令。
3. 串流分批讀取（fetchmany），防範巨量資料表一次性抓取導致記憶體耗盡 (OOM DoS)。
4. 連線資源於 finally 區塊確保關閉釋放，避免佔用遠端資料庫連線池。
5. 內建敏感欄位名稱與內容正則遮罩，防止密碼、Token、金鑰外洩至 LLM 上下文。
6. 支援彈性設定：MSSQL_HOST、MSSQL_PORT、MSSQL_USER、MSSQL_PASSWORD、MSSQL_DATABASE
   可於 mcp_config.yaml 預填、留空、設為 null 或省略；MSSQL_DATABASE 為選填，未設定時可探查所有資料庫清單。
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

mcp = MCPServer("mssql")

# 執行期記憶體快取（僅存活於當前行程生命週期，不落盤）
_RUNTIME_CREDS: dict[str, str] = {}
_CREDS_UPDATED_AT: float = 0.0
_CREDS_TTL_SECONDS: float = 600.0  # 10 分鐘閒置清除

_HOST_RE = re.compile(r"^[a-zA-Z0-9_.\-\\]+$")
_USER_RE = re.compile(r"^[a-zA-Z0-9_.@$\-\\]+$")
_DRIVER_RE = re.compile(r"^[a-zA-Z0-9_.\- ]+$")
_FORBIDDEN_MSSQL_RE = re.compile(
    r"(?i)\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|MERGE"
    r"|GRANT|REVOKE|DENY|EXEC|EXECUTE|DECLARE|SET|USE"
    r"|BACKUP|RESTORE|SHUTDOWN|RECONFIGURE|DBCC|KILL"
    r"|BULK|OPENROWSET|OPENDATASOURCE|OPENQUERY|OPENXML"
    r"|INTO|WAITFOR|UPDATETEXT|WRITETEXT|READTEXT"
    r"|NEXT\s+VALUE\s+FOR|SEQUENCE"
    r"|XP_[\w]+|SP_OA[\w]*|SP_CONFIGURE|SP_ADD[\w]*)\b"
)


def _format_audit_target(host: str = "", port: str = "", user: str = "", db: str = "") -> str:
    h = host.strip() or _RUNTIME_CREDS.get("MSSQL_HOST", "") or _clean_param(os.getenv("MSSQL_HOST")) or "?"
    p = port.strip() or _RUNTIME_CREDS.get("MSSQL_PORT", "") or _clean_param(os.getenv("MSSQL_PORT")) or "1433"
    u = user.strip() or _RUNTIME_CREDS.get("MSSQL_USER", "") or _clean_param(os.getenv("MSSQL_USER")) or "?"
    d = db.strip() or _RUNTIME_CREDS.get("MSSQL_DATABASE", "") or _clean_param(os.getenv("MSSQL_DATABASE")) or "*"
    return f"{u}@{h}:{p}/{d}"


def _audit_log(
    tool_name: str,
    command: str,
    *,
    target: str = "",
    status: str = "OK",
    detail: str = "",
) -> None:
    _db_common.audit_log("mssql", tool_name, command, target=target, status=status, detail=detail)


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
        raise ValueError("非法的 MSSQL_HOST 參數格式。")
    raw_ip = clean.strip("[]")
    try:
        ip = ipaddress.ip_address(raw_ip)
        return str(ip)
    except ValueError:
        pass
    if not _HOST_RE.match(clean):
        raise ValueError("非法的 MSSQL_HOST 參數格式（僅允許合法的 IPv4、IPv6、主機名稱或 host\\instance）。")
    return clean


def _validate_port(value: str) -> str:
    return _db_common.validate_port(value, "MSSQL_PORT")


def _validate_user(value: str) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _USER_RE.match(clean):
        raise ValueError("非法的 MSSQL_USER 參數格式（僅允許英數字、點、底線、連字號、@、$ 與 \\）。")
    return clean


def _strip_leading_sql_comments(sql: str) -> str:
    """移除 SQL 開頭的單行 (--) 與多行 (/* */) 註解，以便精準檢查語句前綴。"""
    s = sql.lstrip()
    while s:
        if s.startswith("--"):
            newline_idx = s.find("\n")
            if newline_idx == -1:
                return ""
            s = s[newline_idx + 1 :].lstrip()
        elif s.startswith("/*"):
            end_idx = s.find("*/")
            if end_idx == -1:
                return ""
            s = s[end_idx + 2 :].lstrip()
        else:
            break
    return s


def _normalize_sql_for_validation(query: str) -> str:
    """在進行關鍵字安全檢查前，正規化移除註解並保留字串常值邊界，防範混淆繞過。

    語法狀態機解析規則：
    1. 支援 T-SQL 單行註解 (-- ...)，替換為單一空格。
    2. 支援 T-SQL 區塊註解 (/* ... */) 與巢狀區塊註解，替換為單一空格。
    3. 嚴格區分字串常值 ('...' 或 N'...')：在字串常值內部之 '--' 或 '/*' 視為字串內容，
       不作為註解消除，避免將後續查詢代碼誤殺；字串內部以空格填補以遮罩潛在干擾。
    4. 支援方括號識別字 ([...]) 完整保留。
    5. 未閉合之字串常值或括號識別字直接拋出 ValueError。
    """
    chars: list[str] = []
    i = 0
    n = len(query)
    while i < n:
        # 1. 區塊註解 /* ... */ (支援 T-SQL 巢狀區塊註解)
        if query[i : i + 2] == "/*":
            nesting = 1
            i += 2
            while i < n and nesting > 0:
                if query[i : i + 2] == "/*":
                    nesting += 1
                    i += 2
                elif query[i : i + 2] == "*/":
                    nesting -= 1
                    i += 2
                else:
                    i += 1
            chars.append(" ")
            continue

        # 2. 單行註解 -- ...
        if query[i : i + 2] == "--":
            i += 2
            while i < n and query[i] not in "\r\n":
                i += 1
            chars.append(" ")
            continue

        # 3. 字串常值 '...' 或 N'...'
        if query[i] == "'" or (query[i] in "nN" and i + 1 < n and query[i + 1] == "'"):
            if query[i] in "nN":
                chars.append("N")
                i += 1
            chars.append("'")
            i += 1
            closed = False
            while i < n:
                if query[i] == "'":
                    if i + 1 < n and query[i + 1] == "'":
                        # '' 轉義單引號
                        chars.append("''")
                        i += 2
                    else:
                        chars.append("'")
                        i += 1
                        closed = True
                        break
                else:
                    # 將字串內容以空格遮罩，保留常值邊界而不洩漏內部關鍵字或誤傷外部代碼
                    chars.append(" ")
                    i += 1
            if not closed:
                raise ValueError("基於安全考量，查詢包含未閉合的字串常值。")
            continue

        # 4. 方括號識別字 [column]
        if query[i] == "[":
            chars.append("[")
            i += 1
            while i < n and query[i] != "]":
                chars.append(query[i])
                i += 1
            if i < n:
                chars.append("]")
                i += 1
            else:
                raise ValueError("基於安全考量，查詢包含未閉合的括號識別字。")
            continue

        # 5. 一般字元
        chars.append(query[i])
        i += 1

    normalized = "".join(chars)
    return re.sub(r"\s+", " ", normalized).strip()


def _validate_select_query(query: str) -> str:
    """驗證 T-SQL 查詢是否為純唯讀單一 SELECT / WITH 語句。"""
    stripped = _strip_leading_sql_comments(query).strip()
    if not stripped:
        raise ValueError("查詢語句不可為空。")

    # 移除結尾分號後，若內部仍含分號則拒絕（防止多重語句堆疊注入）
    without_trailing_semi = stripped.rstrip("; \t\r\n")
    if ";" in without_trailing_semi:
        raise ValueError("基於安全考量，禁止執行包含分號之多重 SQL 語句。")

    normalized = _normalize_sql_for_validation(without_trailing_semi)

    if not re.match(r"(?i)^(?:SELECT|WITH)\b", normalized):
        raise ValueError("基於安全考量，僅允許執行以 SELECT 或 WITH 開頭之唯讀查詢。")

    if _FORBIDDEN_MSSQL_RE.search(normalized):
        raise ValueError("基於安全考量，查詢包含禁止執行的寫入、預存程序或系統變更關鍵字。")

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
            "沒有可用的互動終端機，請於環境變數或 mcp_config.yaml 設定 MSSQL_* 連線參數；不讀取 MCP stdin。"
        ) from exc


def _resolve_connection_params(
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> tuple[str, str, str, str, str]:
    """依「注入參數 → 記憶體快取 → 環境變數 → 終端機互動詢問」順序取得 SQL Server 連線參數（MSSQL_DATABASE 為選填）。"""
    _check_creds_ttl()
    cached_host = _RUNTIME_CREDS.get("MSSQL_HOST", "")
    cached_port = _RUNTIME_CREDS.get("MSSQL_PORT", "")
    cached_user = _RUNTIME_CREDS.get("MSSQL_USER", "")
    cached_db = _RUNTIME_CREDS.get("MSSQL_DATABASE", "")

    env_host = _clean_param(os.getenv("MSSQL_HOST"))
    env_port = _clean_param(os.getenv("MSSQL_PORT"))
    env_user = _clean_param(os.getenv("MSSQL_USER"))
    env_db = _clean_param(os.getenv("MSSQL_DATABASE"))

    host = _clean_param(mssql_host) or cached_host or env_host
    port = _clean_param(mssql_port) or cached_port or env_port
    user = _clean_param(mssql_user) or cached_user or env_user
    database = _clean_param(mssql_database) or cached_db or env_db

    if not host:
        host = _clean_param(_tty_prompt("請輸入 SQL Server 主機 IP 或位址 (MSSQL_HOST): "))
    if not port:
        port = _clean_param(_tty_prompt("請輸入 SQL Server 連接埠 (MSSQL_PORT) [預設 1433]: ")) or "1433"
    if not user:
        user = _clean_param(_tty_prompt(f"請輸入 {host} 的 SQL Server 登入帳號 (MSSQL_USER): "))

    if not host:
        raise ValueError("未提供 SQL Server 主機位址 (MSSQL_HOST)。")
    if not user:
        raise ValueError("未提供 SQL Server 登入帳號 (MSSQL_USER)。")

    host = _validate_host(host)
    port = _validate_port(port)
    user = _validate_user(user)
    if database:
        database = _validate_identifier(database, "MSSQL_DATABASE")

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

    password = mssql_password if not _is_empty_or_null(mssql_password) else ""
    if not password and target_auth == cached_auth:
        password = _RUNTIME_CREDS.get("MSSQL_PASSWORD", "")
    if not password and env_matches:
        env_pw = os.getenv("MSSQL_PASSWORD")
        if not _is_empty_or_null(env_pw):
            password = str(env_pw)

    if not password:
        password = _tty_prompt(f"請輸入 {user}@{host} 的 SQL Server 登入密碼: ", is_password=True)
    if not password:
        raise ValueError("未提供 SQL Server 登入密碼 (MSSQL_PASSWORD)。")

    _RUNTIME_CREDS["MSSQL_HOST"] = host
    _RUNTIME_CREDS["MSSQL_PORT"] = port
    _RUNTIME_CREDS["MSSQL_USER"] = user
    _RUNTIME_CREDS["MSSQL_PASSWORD"] = password
    _RUNTIME_CREDS["MSSQL_DATABASE"] = database
    global _CREDS_UPDATED_AT
    _CREDS_UPDATED_AT = time.monotonic()
    return host, port, user, password, database


def _load_mssql_driver() -> tuple[str, Any]:
    """延遲載入 SQL Server 驅動（優先使用 pymssql，次選 pyodbc）。"""
    for mod_name, driver_type in (("pymssql", "pymssql"), ("pyodbc", "pyodbc")):
        try:
            mod = importlib.import_module(mod_name)
            return driver_type, mod
        except ImportError:
            continue
    raise RuntimeError("尚未安裝 SQL Server 驅動套件。請在終端機執行：pip install pymssql（或 pip install pyodbc）")


def _resolve_odbc_driver(pyodbc_mod: Any) -> str:
    custom = _clean_param(os.getenv("MSSQL_DRIVER"))
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


def _get_readonly_connection(
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> Any:
    """建立遠端 SQL Server 連線並強制設定為唯讀交易隔離模式。"""
    try:
        host, port, user, password, database = _resolve_connection_params(
            mssql_host, mssql_port, mssql_user, mssql_password, mssql_database
        )
    except ValueError as exc:
        _clear_creds_cache()
        raise _ConnAuthError(f"[CONN_AUTH] {exc}") from exc

    # 驅動載入置於連線區塊外，避免未安裝驅動套件時誤清除已快取的連線帳密
    driver_type, driver_mod = _load_mssql_driver()

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
            encrypt_env = (_clean_param(os.getenv("MSSQL_ENCRYPT")) or "yes").lower()
            encrypt_val = "no" if encrypt_env in ("no", "false", "0") else "yes"
            trust_env = (_clean_param(os.getenv("MSSQL_TRUST_SERVER_CERTIFICATE")) or "yes").lower()
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
        _clear_creds_cache()
        raise _ConnAuthError(f"[CONN_AUTH] SQL Server 連線或認證失敗: {exc}") from exc


def _adapt_placeholders(sql: str, conn: Any) -> str:
    """根據底層連線驅動轉換參數佔位符（pyodbc 使用 ?，pymssql 使用 %s）。"""
    mod_name = type(conn).__module__.lower()
    if "pyodbc" in mod_name:
        return sql.replace("%s", "?")
    return sql


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


@mcp.tool()
def mssql_list_databases(
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """列出遠端 Microsoft SQL Server 伺服器上所有狀態為 ONLINE 的資料庫清單"""
    conn = None
    sql = "SELECT name FROM sys.databases WHERE state_desc = 'ONLINE' ORDER BY name;"
    try:
        conn = _get_readonly_connection(mssql_host, mssql_port, mssql_user, mssql_password, mssql_database)
        target = _format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database)
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            dbs = cursor.fetchall()
        finally:
            cursor.close()

        _audit_log("mssql_list_databases", sql, target=target, status="OK", detail=f"databases={len(dbs)}")
        if not dbs:
            return "查無可見的資料庫。"
        return "\n".join(str(row[0]) for row in dbs)
    except _ConnAuthError as e:
        _audit_log(
            "mssql_list_databases",
            sql,
            target=_format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database),
            status="AUTH_FAIL",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    except Exception as e:
        _audit_log(
            "mssql_list_databases",
            sql,
            target=_format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    finally:
        _close_safely(conn)


@mcp.tool()
def mssql_list_tables(
    database: str = "",
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """列出遠端 Microsoft SQL Server 目標資料庫中的所有非系統資料表"""
    conn = None
    sql_executed = "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES"
    try:
        explicit_db = _clean_param(database)
        if explicit_db:
            try:
                explicit_db = _validate_identifier(explicit_db, "database")
            except ValueError as e:
                _audit_log(
                    "mssql_list_tables",
                    f"database={database!r}",
                    target=_format_audit_target(mssql_host, mssql_port, mssql_user, database),
                    status="BLOCKED",
                    detail=str(e),
                )
                return f"[ERROR] {e}"

        conn = _get_readonly_connection(mssql_host, mssql_port, mssql_user, mssql_password, mssql_database)
        target_db = explicit_db or _RUNTIME_CREDS.get("MSSQL_DATABASE", "")
        target = _format_audit_target(mssql_host, mssql_port, mssql_user, target_db)
        cursor = conn.cursor()
        try:
            if explicit_db:
                sql_executed = (
                    f"SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM [{explicit_db}].INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW') "
                    "ORDER BY TABLE_SCHEMA, TABLE_NAME;"
                )
            else:
                sql_executed = (
                    "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW') "
                    "ORDER BY TABLE_SCHEMA, TABLE_NAME;"
                )
            cursor.execute(sql_executed)
            tables = cursor.fetchall()
        finally:
            cursor.close()

        _audit_log("mssql_list_tables", sql_executed, target=target, status="OK", detail=f"tables={len(tables)}")
        if not tables:
            return "資料庫中沒有資料表。"
        lines = []
        for row in tables:
            if len(row) >= 2:
                schema_name, tbl_name = str(row[0]), str(row[1])
                base = tbl_name if schema_name.lower() == "dbo" else f"{schema_name}.{tbl_name}"
                if len(row) >= 3 and str(row[2]).upper() == "VIEW":
                    base += " (VIEW)"
                lines.append(base)
            else:
                lines.append(str(row[0]))
        return "\n".join(lines)
    except _ConnAuthError as e:
        _audit_log(
            "mssql_list_tables",
            sql_executed,
            target=_format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="AUTH_FAIL",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    except Exception as e:
        _audit_log(
            "mssql_list_tables",
            sql_executed,
            target=_format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    finally:
        _close_safely(conn)


@mcp.tool()
def mssql_describe_schema(
    table_name: str,
    database: str = "",
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """顯示指定 Microsoft SQL Server 資料表的 Schema (欄位、型別、主鍵與 NOT NULL 約束)"""
    conn = None
    sql_executed = f"DESCRIBE {table_name}"
    try:
        clean_name = table_name.strip().strip("'\"`[]")
        if not clean_name:
            _audit_log(
                "mssql_describe_schema",
                "table_name=''",
                target=_format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
                status="BLOCKED",
                detail="缺少 table_name 參數",
            )
            return "[ERROR] 缺少 table_name 參數。"

        explicit_db = _clean_param(database)
        try:
            if explicit_db:
                explicit_db = _validate_identifier(explicit_db, "database")

            parts = [p.strip("[]'\"") for p in clean_name.split(".")]
            if len(parts) == 3:
                explicit_db = _validate_identifier(parts[0], "database")
                target_schema = _validate_identifier(parts[1], "schema_name")
                target_table = _validate_identifier(parts[2], "table_name")
            elif len(parts) == 2:
                target_schema = _validate_identifier(parts[0], "schema_name")
                target_table = _validate_identifier(parts[1], "table_name")
            elif len(parts) == 1:
                target_schema = "dbo"
                target_table = _validate_identifier(parts[0], "table_name")
            else:
                _audit_log(
                    "mssql_describe_schema",
                    f"table_name={table_name!r}",
                    target=_format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
                    status="BLOCKED",
                    detail=f"非法的 table_name 格式：{table_name!r}",
                )
                return f"[ERROR] 非法的 table_name 格式：{table_name!r}"
        except ValueError as e:
            _audit_log(
                "mssql_describe_schema",
                f"table_name={table_name!r} database={database!r}",
                target=_format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
                status="BLOCKED",
                detail=str(e),
            )
            return f"[ERROR] {e}"

        conn = _get_readonly_connection(mssql_host, mssql_port, mssql_user, mssql_password, mssql_database)
        target_db = explicit_db or _RUNTIME_CREDS.get("MSSQL_DATABASE", "")
        target = _format_audit_target(mssql_host, mssql_port, mssql_user, target_db)
        db_prefix = f"[{explicit_db}]." if explicit_db else ""
        raw_sql = (
            "SELECT c.COLUMN_NAME, c.DATA_TYPE, c.IS_NULLABLE, c.COLUMN_DEFAULT, "
            "CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 1 ELSE 0 END AS IS_PK, "
            "c.CHARACTER_MAXIMUM_LENGTH, c.NUMERIC_PRECISION, c.NUMERIC_SCALE "
            f"FROM {db_prefix}INFORMATION_SCHEMA.COLUMNS c "
            "LEFT JOIN ("
            "  SELECT ku.TABLE_SCHEMA, ku.TABLE_NAME, ku.COLUMN_NAME "
            f"  FROM {db_prefix}INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
            f"  JOIN {db_prefix}INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku "
            "    ON tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME "
            "    AND tc.TABLE_SCHEMA = ku.TABLE_SCHEMA "
            "    AND tc.TABLE_NAME = ku.TABLE_NAME "
            "  WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'"
            ") pk "
            "  ON c.TABLE_SCHEMA = pk.TABLE_SCHEMA "
            "  AND c.TABLE_NAME = pk.TABLE_NAME "
            "  AND c.COLUMN_NAME = pk.COLUMN_NAME "
            "WHERE c.TABLE_SCHEMA = %s AND c.TABLE_NAME = %s "
            "ORDER BY c.ORDINAL_POSITION;"
        )
        sql = _adapt_placeholders(raw_sql, conn)
        sql_executed = f"{sql} [schema={target_schema!r}, table={target_table!r}]"
        cursor = conn.cursor()
        idx_rows: list[Any] = []
        fk_rows: list[Any] = []
        try:
            cursor.execute(sql, (target_schema, target_table))
            columns = cursor.fetchall()
            if columns and len(columns[0]) >= 8:
                try:
                    idx_raw_sql = (
                        "SELECT i.name AS index_name, i.is_unique, col.name AS column_name "
                        f"FROM {db_prefix}sys.indexes i "
                        f"JOIN {db_prefix}sys.index_columns ic "
                        "  ON i.object_id = ic.object_id AND i.index_id = ic.index_id "
                        f"JOIN {db_prefix}sys.columns col "
                        "  ON ic.object_id = col.object_id AND ic.column_id = col.column_id "
                        f"JOIN {db_prefix}sys.tables t ON i.object_id = t.object_id "
                        f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
                        "WHERE i.name IS NOT NULL AND s.name = %s AND t.name = %s "
                        "ORDER BY i.name, ic.key_ordinal;"
                    )
                    cursor.execute(_adapt_placeholders(idx_raw_sql, conn), (target_schema, target_table))
                    fetched_idx = cursor.fetchall()
                    if fetched_idx and fetched_idx != columns:
                        idx_rows = list(fetched_idx)

                    fk_raw_sql = (
                        "SELECT cp.name AS column_name, tr.name AS ref_table, cr.name AS ref_column "
                        f"FROM {db_prefix}sys.foreign_key_columns fkc "
                        f"JOIN {db_prefix}sys.tables tp ON fkc.parent_object_id = tp.object_id "
                        f"JOIN {db_prefix}sys.schemas sp ON tp.schema_id = sp.schema_id "
                        f"JOIN {db_prefix}sys.columns cp "
                        "  ON fkc.parent_object_id = cp.object_id AND fkc.parent_column_id = cp.column_id "
                        f"JOIN {db_prefix}sys.tables tr ON fkc.referenced_object_id = tr.object_id "
                        f"JOIN {db_prefix}sys.columns cr "
                        "  ON fkc.referenced_object_id = cr.object_id AND fkc.referenced_column_id = cr.column_id "
                        "WHERE sp.name = %s AND tp.name = %s "
                        "ORDER BY fkc.constraint_column_id;"
                    )
                    cursor.execute(_adapt_placeholders(fk_raw_sql, conn), (target_schema, target_table))
                    fetched_fk = cursor.fetchall()
                    if fetched_fk and fetched_fk != columns:
                        fk_rows = list(fetched_fk)
                except Exception:
                    pass
        finally:
            cursor.close()

        if not columns:
            _audit_log(
                "mssql_describe_schema",
                sql_executed,
                target=target,
                status="NOT_FOUND",
                detail=f"找不到資料表 {clean_name}",
            )
            return f"找不到資料表 {clean_name}。"

        _audit_log(
            "mssql_describe_schema",
            sql_executed,
            target=target,
            status="OK",
            detail=f"columns={len(columns)}",
        )
        lines = []
        for c in columns:
            col_name = str(c[0])
            col_type = str(c[1] or "NVARCHAR").upper()
            is_nullable = str(c[2] or "").upper()
            col_default = c[3]
            is_pk = bool(c[4])
            char_len = c[5] if len(c) > 5 else None
            num_prec = c[6] if len(c) > 6 else None
            num_scale = c[7] if len(c) > 7 else None
            if col_type in ("VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "BINARY", "VARBINARY") and char_len is not None:
                len_str = "MAX" if int(char_len) == -1 else str(char_len)
                col_type = f"{col_type}({len_str})"
            elif col_type in ("DECIMAL", "NUMERIC") and num_prec is not None:
                scale_str = f",{num_scale}" if num_scale is not None else ""
                col_type = f"{col_type}({num_prec}{scale_str})"
            pk_tag = " [PK]" if is_pk else ""
            null_tag = " NOT NULL" if is_nullable == "NO" else ""
            default_tag = f" DEFAULT {col_default}" if col_default is not None else ""
            lines.append(f"{col_name} ({col_type}{pk_tag}{null_tag}{default_tag})")

        if idx_rows:
            idx_map: dict[str, tuple[bool, list[str]]] = {}
            for r in idx_rows:
                if len(r) >= 3:
                    iname = str(r[0])
                    is_uniq = bool(r[1])
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
            "mssql_describe_schema",
            sql_executed,
            target=_format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="AUTH_FAIL",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    except Exception as e:
        _audit_log(
            "mssql_describe_schema",
            sql_executed,
            target=_format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    finally:
        _close_safely(conn)


@mcp.tool()
def mssql_run_select_query(
    query: str,
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """於遠端 Microsoft SQL Server 執行唯讀的 SELECT 或 WITH 查詢 (具備串流防爆與機密欄位遮罩)"""
    try:
        clean_query = _validate_select_query(query)
    except ValueError as e:
        _audit_log(
            "mssql_run_select_query",
            query,
            target=_format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database),
            status="BLOCKED",
            detail=str(e),
        )
        return f"[ERROR] {e}"

    conn = None
    try:
        conn = _get_readonly_connection(mssql_host, mssql_port, mssql_user, mssql_password, mssql_database)
        target = _format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database)
        cursor = conn.cursor()
        try:
            cursor.execute(clean_query)
            if not cursor.description:
                _audit_log("mssql_run_select_query", clean_query, target=target, status="OK", detail="rows=0")
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
        finally:
            cursor.close()

        if total_rows == 0:
            _audit_log("mssql_run_select_query", clean_query, target=target, status="OK", detail="rows=0")
            return "查詢結果為空。"

        shown_rows = len(result) - 1
        _audit_log(
            "mssql_run_select_query",
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
                "[提示：若需檢視特定區段資料，請在 SQL 中加入 WHERE 條件或 TOP / OFFSET-FETCH 分頁]"
            )
        return output
    except _ConnAuthError as e:
        _audit_log(
            "mssql_run_select_query",
            clean_query,
            target=_format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database),
            status="AUTH_FAIL",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    except Exception as e:
        _audit_log(
            "mssql_run_select_query",
            clean_query,
            target=_format_audit_target(mssql_host, mssql_port, mssql_user, mssql_database),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] 查詢失敗: {e}"
    finally:
        _close_safely(conn)


def _mssql_int_type_max(type_name: str) -> int | None:
    """根據 SQL Server 整數型別名稱回傳理論最大正整數值。"""
    t = (type_name or "").strip().lower()
    if t == "tinyint":
        return 255
    if t == "smallint":
        return 32767
    if t in ("int", "integer"):
        return 2147483647
    if t == "bigint":
        return 9223372036854775807
    return None


def _parse_mssql_target(table_name: str, database: str) -> tuple[str, str, str]:
    """解析並驗證選填之 database 與 table_name（支援 db.schema.table、schema.table、table）。"""
    explicit_db = _clean_param(database)
    if explicit_db:
        explicit_db = _validate_identifier(explicit_db, "database")

    clean_tbl = (table_name or "").strip().strip("'\"`[]")
    if not clean_tbl:
        return explicit_db, "", ""

    parts = [p.strip("[]'\"") for p in clean_tbl.split(".")]
    if len(parts) == 3:
        explicit_db = _validate_identifier(parts[0], "database")
        target_schema = _validate_identifier(parts[1], "schema_name")
        target_table = _validate_identifier(parts[2], "table_name")
        return explicit_db, target_schema, target_table
    if len(parts) == 2:
        target_schema = _validate_identifier(parts[0], "schema_name")
        target_table = _validate_identifier(parts[1], "table_name")
        return explicit_db, target_schema, target_table
    if len(parts) == 1:
        target_table = _validate_identifier(parts[0], "table_name")
        return explicit_db, "", target_table
    raise ValueError(f"非法的 table_name 格式：{table_name!r}")


@mcp.tool()
def mssql_inspect_health(
    database: str = "",
    table_name: str = "",
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """檢視遠端 SQL Server 資料庫或資料表的結構、空間、索引與約束健康狀態（零鎖定 DMV 巡檢）"""
    conn = None
    sql_executed = "SELECT ... FROM sys.tables"
    try:
        try:
            explicit_db, target_schema, target_table = _parse_mssql_target(table_name, database)
        except ValueError as e:
            _audit_log(
                "mssql_inspect_health",
                f"database={database!r} table_name={table_name!r}",
                target=_format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
                status="BLOCKED",
                detail=str(e),
            )
            return f"[ERROR] {e}"

        conn = _get_readonly_connection(mssql_host, mssql_port, mssql_user, mssql_password, mssql_database)
        target_db = (
            explicit_db
            or _clean_param(mssql_database)
            or _RUNTIME_CREDS.get("MSSQL_DATABASE", "")
            or _clean_param(os.getenv("MSSQL_DATABASE"))
        )
        target = _format_audit_target(mssql_host, mssql_port, mssql_user, target_db)
        db_prefix = f"[{explicit_db}]." if explicit_db else ""

        where_parts = ["t.is_ms_shipped = 0"]
        params: list[Any] = []
        if target_schema:
            where_parts.append("s.name = %s")
            params.append(target_schema)
        if target_table:
            where_parts.append("t.name = %s")
            params.append(target_table)
        where_sql = " AND ".join(where_parts)
        limit_n = int(DB_HEALTH_MAX_TABLES)

        dmv_sql_raw = (
            f"SELECT TOP {limit_n} "
            "s.name AS schema_name, t.name AS table_name, "
            "CASE WHEN EXISTS ("
            f"  SELECT 1 FROM {db_prefix}sys.indexes i "
            "  WHERE i.object_id = t.object_id AND i.is_primary_key = 1"
            ") THEN 1 ELSE 0 END AS has_pk, "
            "CASE WHEN EXISTS ("
            f"  SELECT 1 FROM {db_prefix}sys.indexes i "
            "  WHERE i.object_id = t.object_id AND i.index_id = 0"
            ") THEN 1 ELSE 0 END AS is_heap, "
            "COALESCE(ps.row_count, 0) AS row_count, "
            "COALESCE(ps.reserved_pages, 0) * 8192 AS reserved_bytes, "
            "COALESCE(ps.used_pages, 0) * 8192 AS used_bytes "
            f"FROM {db_prefix}sys.tables t "
            f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
            "LEFT JOIN ("
            "  SELECT object_id, "
            "    SUM(CASE WHEN index_id IN (0, 1) THEN row_count ELSE 0 END) AS row_count, "
            "    SUM(reserved_page_count) AS reserved_pages, "
            "    SUM(used_page_count) AS used_pages "
            f"  FROM {db_prefix}sys.dm_db_partition_stats GROUP BY object_id"
            ") ps ON t.object_id = ps.object_id "
            f"WHERE {where_sql} "
            "ORDER BY COALESCE(ps.reserved_pages, 0) DESC, s.name, t.name;"
        )
        fallback_sql_raw = (
            f"SELECT TOP {limit_n} "
            "s.name AS schema_name, t.name AS table_name, "
            "CASE WHEN EXISTS ("
            f"  SELECT 1 FROM {db_prefix}sys.indexes i "
            "  WHERE i.object_id = t.object_id AND i.is_primary_key = 1"
            ") THEN 1 ELSE 0 END AS has_pk, "
            "CASE WHEN EXISTS ("
            f"  SELECT 1 FROM {db_prefix}sys.indexes i "
            "  WHERE i.object_id = t.object_id AND i.index_id = 0"
            ") THEN 1 ELSE 0 END AS is_heap, "
            "NULL AS row_count, 0 AS reserved_bytes, 0 AS used_bytes "
            f"FROM {db_prefix}sys.tables t "
            f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
            f"WHERE {where_sql} "
            "ORDER BY s.name, t.name;"
        )

        cursor = conn.cursor()
        dmv_degraded = False
        idx_rows: list[Any] = []
        constraint_rows: list[Any] = []
        identity_rows: list[Any] = []
        try:
            try:
                sql_executed = _adapt_placeholders(dmv_sql_raw, conn)
                cursor.execute(sql_executed, tuple(params))
                tables = cursor.fetchall()
            except Exception:
                dmv_degraded = True
                sql_executed = _adapt_placeholders(fallback_sql_raw, conn)
                cursor.execute(sql_executed, tuple(params))
                tables = cursor.fetchall()

            if tables:
                try:
                    idx_raw = (
                        "SELECT s.name, t.name, i.name, i.is_unique, i.is_disabled, col.name "
                        f"FROM {db_prefix}sys.indexes i "
                        f"JOIN {db_prefix}sys.tables t ON i.object_id = t.object_id "
                        f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
                        f"LEFT JOIN {db_prefix}sys.index_columns ic "
                        "  ON i.object_id = ic.object_id AND i.index_id = ic.index_id AND ic.is_included_column = 0 "
                        f"LEFT JOIN {db_prefix}sys.columns col "
                        "  ON ic.object_id = col.object_id AND ic.column_id = col.column_id "
                        f"WHERE {where_sql} AND i.name IS NOT NULL "
                        "ORDER BY s.name, t.name, i.name, ic.key_ordinal;"
                    )
                    cursor.execute(_adapt_placeholders(idx_raw, conn), tuple(params))
                    fetched_idx = cursor.fetchall()
                    if fetched_idx and fetched_idx != tables:
                        idx_rows = list(fetched_idx)
                except Exception:
                    pass

                try:
                    cons_raw = (
                        "SELECT s.name, t.name, fk.name, 'FOREIGN KEY', fk.is_disabled, fk.is_not_trusted "
                        f"FROM {db_prefix}sys.foreign_keys fk "
                        f"JOIN {db_prefix}sys.tables t ON fk.parent_object_id = t.object_id "
                        f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
                        f"WHERE {where_sql} AND (fk.is_disabled = 1 OR fk.is_not_trusted = 1) "
                        "UNION ALL "
                        "SELECT s.name, t.name, cc.name, 'CHECK', cc.is_disabled, cc.is_not_trusted "
                        f"FROM {db_prefix}sys.check_constraints cc "
                        f"JOIN {db_prefix}sys.tables t ON cc.parent_object_id = t.object_id "
                        f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
                        f"WHERE {where_sql} AND (cc.is_disabled = 1 OR cc.is_not_trusted = 1);"
                    )
                    cursor.execute(_adapt_placeholders(cons_raw, conn), tuple(params) * 2)
                    fetched_cons = cursor.fetchall()
                    if fetched_cons and fetched_cons != tables:
                        constraint_rows = list(fetched_cons)
                except Exception:
                    pass

                try:
                    ident_raw = (
                        "SELECT s.name, t.name, ic.name, tp.name, ic.last_value "
                        f"FROM {db_prefix}sys.identity_columns ic "
                        f"JOIN {db_prefix}sys.tables t ON ic.object_id = t.object_id "
                        f"JOIN {db_prefix}sys.schemas s ON t.schema_id = s.schema_id "
                        f"JOIN {db_prefix}sys.types tp ON ic.system_type_id = tp.user_type_id "
                        f"WHERE {where_sql} AND ic.last_value IS NOT NULL;"
                    )
                    cursor.execute(_adapt_placeholders(ident_raw, conn), tuple(params))
                    fetched_ident = cursor.fetchall()
                    if fetched_ident and fetched_ident != tables:
                        identity_rows = list(fetched_ident)
                except Exception:
                    pass
        finally:
            cursor.close()

        if not tables:
            _audit_log(
                "mssql_inspect_health",
                sql_executed,
                target=target,
                status="NOT_FOUND",
                detail="查無符合條件的資料表",
            )
            return "查無符合條件的資料表。"

        _audit_log(
            "mssql_inspect_health",
            sql_executed,
            target=target,
            status="OK",
            detail=f"tables={len(tables)} degraded={dmv_degraded}",
        )

        # 彙整索引（含已停用索引與冗餘前綴）
        table_idx_map: dict[tuple[str, str], dict[str, tuple[bool, list[str]]]] = {}
        disabled_indexes: list[tuple[str, str, str]] = []
        seen_disabled: set[tuple[str, str, str]] = set()
        for r in idx_rows:
            if len(r) >= 6:
                key_pair = (str(r[0]), str(r[1]))
                iname = str(r[2])
                is_uniq = bool(r[3])
                is_dis = bool(r[4])
                cname = r[5]
                if is_dis and (key_pair[0], key_pair[1], iname) not in seen_disabled:
                    seen_disabled.add((key_pair[0], key_pair[1], iname))
                    disabled_indexes.append((key_pair[0], key_pair[1], iname))
                if cname is not None:
                    t_map = table_idx_map.setdefault(key_pair, {})
                    if iname not in t_map:
                        t_map[iname] = (is_uniq, [])
                    t_map[iname][1].append(str(cname))

        total_reserved_bytes = 0
        total_used_bytes = 0
        overview_lines: list[str] = []
        warnings_list: list[str] = []

        for t in tables:
            s_name = str(t[0])
            t_name = str(t[1])
            has_pk = bool(t[2]) if len(t) > 2 else True
            is_heap = bool(t[3]) if len(t) > 3 else False
            row_cnt = t[4] if len(t) > 4 else None
            res_bytes = int(t[5] or 0) if len(t) > 5 else 0
            used_bytes = int(t[6] or 0) if len(t) > 6 else 0
            unused_bytes = max(0, res_bytes - used_bytes)

            key_pair = (s_name, t_name)
            display_name = t_name if s_name.lower() == "dbo" else f"{s_name}.{t_name}"
            total_reserved_bytes += res_bytes
            total_used_bytes += used_bytes

            structure_tag = "Heap (無叢集索引)" if is_heap else "Clustered"
            pk_tag = "PK=Yes" if has_pk else "PK=No"
            if dmv_degraded or row_cnt is None:
                overview_lines.append(f"- {display_name}: Structure={structure_tag}, {pk_tag}")
            else:
                overview_lines.append(
                    f"- {display_name}: Structure={structure_tag}, {pk_tag}, "
                    f"Rows={int(row_cnt)}, Reserved={_format_bytes(res_bytes)}, "
                    f"Used={_format_bytes(used_bytes)}, Unused={_format_bytes(unused_bytes)}"
                )

            if not has_pk:
                warnings_list.append(f"[Missing PK] {display_name} 缺少主鍵 (Primary Key)")
            if is_heap:
                warnings_list.append(
                    f"[Heap Table] {display_name} 為堆積表 (Heap，無叢集索引)，"
                    "頻繁更新易產生轉發紀錄 (Forwarded Records) 與空間碎片"
                )
            if not dmv_degraded and res_bytes > 0 and unused_bytes >= DB_FRAGMENTATION_MIN_FREE_BYTES:
                unused_ratio = unused_bytes / res_bytes
                if unused_ratio >= DB_FRAGMENTATION_WARN_RATIO:
                    warnings_list.append(
                        f"[Unused Space] {display_name} 未用保留空間達 {_format_bytes(unused_bytes)} "
                        f"({unused_ratio * 100:.1f}%)，建議評估索引重組或空間回收"
                    )
            if key_pair in table_idx_map:
                for red_msg in _find_redundant_prefix_indexes(table_idx_map[key_pair]):
                    warnings_list.append(f"[Redundant Index] {display_name}: {red_msg}")

        for s_name, t_name, iname in disabled_indexes:
            disp = t_name if s_name.lower() == "dbo" else f"{s_name}.{t_name}"
            warnings_list.append(f"[Disabled Index] {disp}.{iname} 處於停用狀態 (is_disabled=1)")

        for c_row in constraint_rows:
            if len(c_row) >= 6:
                s_name, t_name, cname, ctype = str(c_row[0]), str(c_row[1]), str(c_row[2]), str(c_row[3])
                is_dis = bool(c_row[4])
                disp = t_name if s_name.lower() == "dbo" else f"{s_name}.{t_name}"
                state_desc = "已停用 (Disabled)" if is_dis else "未受信任 (Untrusted / WITH NOCHECK)"
                warnings_list.append(f"[Constraint] {disp} 的 {ctype} 約束 {cname} 處於 {state_desc} 狀態")

        for id_row in identity_rows:
            if len(id_row) >= 5:
                s_name, t_name, col_name, tp_name, last_val = (
                    str(id_row[0]),
                    str(id_row[1]),
                    str(id_row[2]),
                    str(id_row[3]),
                    id_row[4],
                )
                max_val = _mssql_int_type_max(tp_name)
                try:
                    cur_val = int(last_val)
                except TypeError, ValueError:
                    cur_val = 0
                if max_val and cur_val > 0:
                    ratio = cur_val / max_val
                    if ratio >= DB_AUTOINC_WARN_RATIO:
                        disp = t_name if s_name.lower() == "dbo" else f"{s_name}.{t_name}"
                        warnings_list.append(
                            f"[Identity] {disp}.{col_name} ({tp_name.upper()}) "
                            f"水位已達 {ratio * 100:.1f}% ({cur_val}/{max_val})，有溢位風險"
                        )

        scope_label = target_db or "預設連線資料庫"
        if target_schema or target_table:
            tbl_filter = f"{target_schema}.{target_table}" if target_schema else target_table
            scope_label += f" / 資料表: {tbl_filter}"

        report: list[str] = [
            "[MSSQL Health Summary]",
            f"巡檢範圍: {scope_label}",
            f"資料表數: {len(tables)} 張使用者資料表",
        ]
        if dmv_degraded:
            report.append("空間統計: ℹ️ 目前帳號無 VIEW DATABASE STATE 權限，已自動降級略過 DMV 分區空間與列數統計")
        else:
            total_unused = max(0, total_reserved_bytes - total_used_bytes)
            report.append(f"總保留空間 (Reserved): {_format_bytes(total_reserved_bytes)}")
            report.append(f"總已用空間 (Used): {_format_bytes(total_used_bytes)}")
            report.append(f"總未用保留空間 (Unused): {_format_bytes(total_unused)}")

        report.extend(["", "[Health Warnings]"])
        if warnings_list:
            report.extend(f"⚠️ {w}" for w in warnings_list)
        else:
            report.append("✓ 未發現明顯結構、主鍵、Heap、停用索引或未受信任約束風險。")

        report.extend(["", "[Table Overview]"])
        report.extend(overview_lines)
        return _truncate_health_report(report, MAX_OUTPUT_CHARS)
    except _ConnAuthError as e:
        _audit_log(
            "mssql_inspect_health",
            sql_executed,
            target=_format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="AUTH_FAIL",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    except Exception as e:
        _audit_log(
            "mssql_inspect_health",
            sql_executed,
            target=_format_audit_target(mssql_host, mssql_port, mssql_user, database or mssql_database),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] 健康檢查失敗: {e}"
    finally:
        _close_safely(conn)


if __name__ == "__main__":
    mcp.run()

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
   可於 mcp_config.yaml 預填、留空、設為 null 或省略；未設定時於執行期互動詢問並快取於記憶體。
"""

import getpass
import importlib
import ipaddress
import os
import re
import sys
import time
import warnings
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

try:
    from pocket_agent.system.constants import DEFAULT_TOOL_OUTPUT_CHARS

    MAX_OUTPUT_CHARS = DEFAULT_TOOL_OUTPUT_CHARS
except ImportError:
    MAX_OUTPUT_CHARS = 12000

mcp = MCPServer("mssql")

# 執行期記憶體快取（僅存活於當前行程生命週期，不落盤）
_RUNTIME_CREDS: dict[str, str] = {}
_CREDS_UPDATED_AT: float = 0.0
_CREDS_TTL_SECONDS: float = 600.0  # 10 分鐘閒置清除

_HOST_RE = re.compile(r"^[a-zA-Z0-9_.\-\\]+$")
_USER_RE = re.compile(r"^[a-zA-Z0-9_.@$\-\\]+$")
_IDENT_RE = re.compile(r"^[a-zA-Z0-9_.@$\-]+$")
_DRIVER_RE = re.compile(r"^[a-zA-Z0-9_.\- ]+$")

_REDACT_SECRET_RE = re.compile(
    r"(?im)\b(?:[\w-]*(?:password|passwd|secret|token|passphrase|api[_-]?key)[\w-]*"
    r"|authorization|private_key|preshared_key|connectionstring)\b[^\r\n\x00]*"
)
_SENSITIVE_COL_RE = re.compile(
    r"(?i)(?:password|passwd|secret|token|passphrase|api[_-]?key"
    r"|private[_-]?key|preshared[_-]?key|credential|connectionstring)"
)
_FORBIDDEN_MSSQL_RE = re.compile(
    r"(?i)\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|MERGE"
    r"|GRANT|REVOKE|DENY|EXEC|EXECUTE|DECLARE|SET|USE"
    r"|BACKUP|RESTORE|SHUTDOWN|RECONFIGURE|DBCC|KILL"
    r"|BULK|OPENROWSET|OPENDATASOURCE|OPENQUERY|OPENXML"
    r"|INTO|WAITFOR|UPDATETEXT|WRITETEXT|READTEXT"
    r"|XP_[\w]+|SP_OA[\w]*|SP_CONFIGURE|SP_ADD[\w]*)\b"
)


class _ConnAuthError(Exception):
    """表示連線參數驗證失敗、網路無法連線或資料庫帳號密碼認證失敗。"""


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


def _redact_output(text: str) -> str:
    """自動遮罩查詢結果中的敏感機密欄位與鍵值。"""
    if not text:
        return text
    return _REDACT_SECRET_RE.sub("[REDACTED]", text)


def _is_empty_or_null(val: str | None) -> bool:
    """檢查字串是否為空值、空白、null 或無效預留值。"""
    if val is None:
        return True
    cleaned = str(val).strip()
    return not cleaned or cleaned.lower() in ("null", "none", "~")


def _clean_param(val: str | None) -> str:
    """清理並移除外層引號；若為空或無效預留值則回傳空字串。"""
    if _is_empty_or_null(val):
        return ""
    return str(val).strip().strip("'\"")


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
    clean = value.strip()
    if not clean.isdigit() or not (1 <= int(clean) <= 65535):
        raise ValueError("非法的 MSSQL_PORT 連接埠數值（必須為 1~65535 之間的整數）。")
    return str(int(clean))


def _validate_user(value: str) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _USER_RE.match(clean):
        raise ValueError("非法的 MSSQL_USER 參數格式（僅允許英數字、點、底線、連字號、@、$ 與 \\）。")
    return clean


def _validate_identifier(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _IDENT_RE.match(clean):
        raise ValueError(
            f"非法的 {field_name} 參數格式（僅允許英數字、點、底線、連字號、@ 與 $，且不得以連字號開頭）。"
        )
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


def _validate_select_query(query: str) -> str:
    """驗證 T-SQL 查詢是否為純唯讀單一 SELECT / WITH 語句。"""
    stripped = _strip_leading_sql_comments(query).strip()
    if not stripped:
        raise ValueError("查詢語句不可為空。")

    # 移除結尾分號後，若內部仍含分號則拒絕（防止多重語句堆疊注入）
    without_trailing_semi = stripped.rstrip("; \t\r\n")
    if ";" in without_trailing_semi:
        raise ValueError("基於安全考量，禁止執行包含分號之多重 SQL 語句。")

    if not re.match(r"(?i)^(?:SELECT|WITH)\b", without_trailing_semi):
        raise ValueError("基於安全考量，僅允許執行以 SELECT 或 WITH 開頭之唯讀查詢。")

    if _FORBIDDEN_MSSQL_RE.search(without_trailing_semi):
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
    """依「注入參數 → 記憶體快取 → 環境變數 → 終端機互動詢問」順序取得 SQL Server 連線五要素。"""
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
    if not database:
        database = _clean_param(_tty_prompt(f"請輸入 {host} 的目標資料庫名稱 (MSSQL_DATABASE): "))

    if not host:
        raise ValueError("未提供 SQL Server 主機位址 (MSSQL_HOST)。")
    if not user:
        raise ValueError("未提供 SQL Server 登入帳號 (MSSQL_USER)。")
    if not database:
        raise ValueError("未提供 SQL Server 資料庫名稱 (MSSQL_DATABASE)。")

    host = _validate_host(host)
    port = _validate_port(port)
    user = _validate_user(user)
    database = _validate_identifier(database, "MSSQL_DATABASE")

    target = (host, port, user, database)
    cached_target = (cached_host, cached_port, cached_user, cached_db)
    env_matches = (
        (not env_host or env_host == host)
        and (not env_port or _validate_port(env_port) == port)
        and (not env_user or env_user == user)
        and (not env_db or env_db == database)
    )

    password = mssql_password if not _is_empty_or_null(mssql_password) else ""
    if not password and target == cached_target:
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

    driver_type, driver_mod = _load_mssql_driver()

    try:
        timeout = max(3, min(int(os.getenv("MSSQL_CONNECT_TIMEOUT") or "10"), 60))
    except ValueError:
        timeout = 10

    conn = None
    try:
        if driver_type == "pymssql":
            conn = driver_mod.connect(
                server=host,
                port=int(port),
                user=user,
                password=password,
                database=database,
                login_timeout=timeout,
                timeout=timeout,
                autocommit=False,
            )
        else:
            odbc_driver = _resolve_odbc_driver(driver_mod)
            encrypt_env = (_clean_param(os.getenv("MSSQL_ENCRYPT")) or "yes").lower()
            encrypt_val = "no" if encrypt_env in ("no", "false", "0") else "yes"
            trust_env = (_clean_param(os.getenv("MSSQL_TRUST_SERVER_CERTIFICATE")) or "yes").lower()
            trust_val = "no" if trust_env in ("no", "false", "0") else "yes"
            conn_str = (
                f"DRIVER={_escape_odbc_value(odbc_driver)};"
                f"SERVER=tcp:{host},{port};"
                f"DATABASE={_escape_odbc_value(database)};"
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
def mssql_list_tables(
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """列出遠端 Microsoft SQL Server 目標資料庫中的所有非系統資料表"""
    conn = None
    try:
        conn = _get_readonly_connection(mssql_host, mssql_port, mssql_user, mssql_password, mssql_database)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE = 'BASE TABLE' "
                "ORDER BY TABLE_SCHEMA, TABLE_NAME;"
            )
            tables = cursor.fetchall()
        finally:
            cursor.close()

        if not tables:
            return "資料庫中沒有資料表。"
        lines = []
        for row in tables:
            if len(row) >= 2:
                schema_name, tbl_name = str(row[0]), str(row[1])
                lines.append(tbl_name if schema_name.lower() == "dbo" else f"{schema_name}.{tbl_name}")
            else:
                lines.append(str(row[0]))
        return "\n".join(lines)
    except Exception as e:
        return f"[ERROR] {e}"
    finally:
        _close_safely(conn)


@mcp.tool()
def mssql_describe_schema(
    table_name: str,
    mssql_host: str = "",
    mssql_port: str = "",
    mssql_user: str = "",
    mssql_password: str = "",
    mssql_database: str = "",
) -> str:
    """顯示指定 Microsoft SQL Server 資料表的 Schema (欄位、型別、主鍵與 NOT NULL 約束)"""
    conn = None
    try:
        clean_name = table_name.strip().strip("'\"`[]")
        if not clean_name:
            return "[ERROR] 缺少 table_name 參數。"

        if "." in clean_name:
            schema_part, tbl_part = clean_name.split(".", 1)
            target_schema = _validate_identifier(schema_part.strip("[]'\""), "schema_name")
            target_table = _validate_identifier(tbl_part.strip("[]'\""), "table_name")
        else:
            target_schema = "dbo"
            target_table = _validate_identifier(clean_name, "table_name")

        conn = _get_readonly_connection(mssql_host, mssql_port, mssql_user, mssql_password, mssql_database)
        raw_sql = (
            "SELECT c.COLUMN_NAME, c.DATA_TYPE, c.IS_NULLABLE, c.COLUMN_DEFAULT, "
            "CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 1 ELSE 0 END AS IS_PK "
            "FROM INFORMATION_SCHEMA.COLUMNS c "
            "LEFT JOIN ("
            "  SELECT ku.TABLE_SCHEMA, ku.TABLE_NAME, ku.COLUMN_NAME "
            "  FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
            "  JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku "
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
        cursor = conn.cursor()
        try:
            cursor.execute(sql, (target_schema, target_table))
            columns = cursor.fetchall()
        finally:
            cursor.close()

        if not columns:
            return f"找不到資料表 {clean_name}。"

        lines = []
        for c in columns:
            col_name = str(c[0])
            col_type = str(c[1] or "NVARCHAR").upper()
            is_nullable = str(c[2] or "").upper()
            is_pk = bool(c[4])
            pk_tag = " [PK]" if is_pk else ""
            null_tag = " NOT NULL" if is_nullable == "NO" else ""
            lines.append(f"{col_name} ({col_type}{pk_tag}{null_tag})")
        return "\n".join(lines)
    except Exception as e:
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
        return f"[ERROR] {e}"

    conn = None
    try:
        conn = _get_readonly_connection(mssql_host, mssql_port, mssql_user, mssql_password, mssql_database)
        cursor = conn.cursor()
        try:
            cursor.execute(clean_query)
            if not cursor.description:
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
                        "[REDACTED]" if idx in sensitive_indices else _redact_output(str(val))
                        for idx, val in enumerate(row)
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
            return "查詢結果為空。"

        output = "\n".join(result)
        if truncated:
            output += (
                f"\n\n... (查詢結果已截斷，達單次上限 {MAX_OUTPUT_CHARS} 字元，"
                f"已呈現前 {len(result) - 1} 筆紀錄)\n"
                "[提示：若需檢視特定區段資料，請在 SQL 中加入 WHERE 條件或 TOP / OFFSET-FETCH 分頁]"
            )
        return output
    except _ConnAuthError as e:
        return f"[ERROR] {e}"
    except Exception as e:
        return f"[ERROR] 查詢失敗: {e}"
    finally:
        _close_safely(conn)


if __name__ == "__main__":
    mcp.run()

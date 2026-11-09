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

try:
    from pocket_agent.system.constants import DEFAULT_TOOL_OUTPUT_CHARS

    MAX_OUTPUT_CHARS = DEFAULT_TOOL_OUTPUT_CHARS
except ImportError:
    MAX_OUTPUT_CHARS = 12000

mcp = MCPServer("mysql")

# 執行期記憶體快取（僅存活於當前行程生命週期，不落盤）
_RUNTIME_CREDS: dict[str, str] = {}
_CREDS_UPDATED_AT: float = 0.0
_CREDS_TTL_SECONDS: float = 600.0  # 10 分鐘閒置清除

_HOST_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")
_IDENT_RE = re.compile(r"^[a-zA-Z0-9_.@$\-]+$")

_REDACT_SECRET_RE = re.compile(
    r"(?im)\b(?:[\w-]*(?:password|passwd|secret|token|passphrase|api[_-]?key)[\w-]*"
    r"|authorization|private_key|preshared_key|connectionstring)\b[^\r\n\x00]*"
)
_REDACT_URL_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^\s/@\"']+:[^\s/@\"']+@")
_REDACT_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----")
_REDACT_KV_RE = re.compile(
    r"(?i)(\b(?:password|passwd|secret|token|api_key|apikey|client_secret|passphrase|connectionstring)\b"
    r"\s*[:=]\s*)([^\s#;\"',)]+|\"[^\"]*\"|'[^']*')"
)
_SENSITIVE_COL_RE = re.compile(
    r"(?i)(?:password|passwd|secret|token|passphrase|api[_-]?key"
    r"|private[_-]?key|preshared[_-]?key|credential|connectionstring)"
)
_FORBIDDEN_MYSQL_RE = re.compile(
    r"(?i)\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|RENAME"
    r"|GRANT|REVOKE|CALL|EXECUTE|PREPARE|DEALLOCATE|HANDLER|LOAD\s+DATA|LOAD\s+XML"
    r"|INTO\s+OUTFILE|INTO\s+DUMPFILE|INTO\s+@"
    r"|FOR\s+UPDATE|LOCK\s+IN\s+SHARE\s+MODE|LOCK\s+TABLES|UNLOCK\s+TABLES"
    r"|SET\s+GLOBAL|SET\s+SESSION|SET\s+PERSIST|SET\s+@@"
    r")\b|\b(?:SLEEP|BENCHMARK|GET_LOCK|RELEASE_LOCK|RELEASE_ALL_LOCKS)\s*\("
)


class _ConnAuthError(Exception):
    """表示連線參數驗證失敗、網路無法連線或資料庫帳號密碼認證失敗。"""


def _get_audit_log_path() -> Path:
    """解析集中化稽核日誌 (agent.log) 路徑。"""
    env_log = (os.getenv("POCKET_AGENT_LOG_FILE") or "").strip()
    if env_log:
        return Path(env_log).expanduser().resolve()
    try:
        from pocket_agent.system.paths import log_dir

        return log_dir() / "agent.log"
    except ImportError:
        src_dir = Path(__file__).resolve().parent.parent / "src"
        if src_dir.is_dir() and str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            try:
                from pocket_agent.system.paths import log_dir

                return log_dir() / "agent.log"
            except ImportError:
                pass
        return Path.home() / ".pocket-agent" / "logs" / "agent.log"


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
    """將 MCP 工具執行的 SQL 指令與敏感操作寫入 Pocket Agent 集中化日誌 (agent.log) 供稽核使用。"""
    try:
        log_path = _get_audit_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        now_t = time.time()
        lt = time.localtime(now_t)
        ms = int((now_t - int(now_t)) * 1000)
        ts = f"{time.strftime('%Y-%m-%d %H:%M:%S', lt)},{ms:03d}"
        safe_cmd = " ".join(_REDACT_KV_RE.sub(r"\1[REDACTED]", command).splitlines()).strip()
        safe_target = " ".join(_REDACT_KV_RE.sub(r"\1[REDACTED]", target).splitlines()).strip()
        safe_detail = " ".join(_REDACT_KV_RE.sub(r"\1[REDACTED]", detail).splitlines()).strip()
        parts = [f"{ts} [AUDIT] [MCP:mysql] tool={tool_name} status={status}"]
        if safe_target:
            parts.append(f"target={safe_target}")
        if safe_cmd:
            parts.append(f"command={safe_cmd!r}")
        if safe_detail:
            parts.append(f"detail={safe_detail!r}")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(" ".join(parts) + "\n")
    except Exception:
        pass


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
    """自動遮罩查詢結果中的敏感機密欄位、連線 URI 帳密與私鑰。"""
    if not text:
        return text
    out = _REDACT_PEM_RE.sub("[REDACTED_PRIVATE_KEY]", text)
    out = _REDACT_URL_RE.sub(r"\1[REDACTED]@", out)
    return _REDACT_SECRET_RE.sub("[REDACTED]", out)


def _format_cell_value(val: object, *, is_sensitive: bool = False) -> str:
    """格式化單一欄位值：敏感欄強制遮罩、None 轉為 NULL、多行文字遮罩後單行化。"""
    if is_sensitive:
        return "[REDACTED]"
    if val is None:
        return "NULL"
    redacted = _redact_output(str(val))
    return redacted.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


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
    clean = value.strip()
    if not clean.isdigit() or not (1 <= int(clean) <= 65535):
        raise ValueError("非法的 MYSQL_PORT 連接埠數值（必須為 1~65535 之間的整數）。")
    return str(int(clean))


def _validate_identifier(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _IDENT_RE.match(clean):
        raise ValueError(
            f"非法的 {field_name} 參數格式（僅允許英數字、點、底線、連字號、@ 與 $，且不得以連字號開頭）。"
        )
    return clean


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


if __name__ == "__main__":
    mcp.run()

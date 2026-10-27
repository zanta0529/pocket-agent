"""SQLite 唯讀查詢與結構檢索 MCP 伺服器。

安全設計原則：
1. 強制以 URI mode=ro 模式開啟連線，並配置 SQLite Authorizer 嚴密攔截非唯讀操作。
2. 串流分批讀取（fetchmany），防範巨量資料表一次性抓取導致記憶體耗盡 (OOM DoS)。
3. 連線資源確保釋放，杜絕 Windows 檔案佔用與鎖死。
4. 內建機密欄位與正則遮罩，防止資料庫機敏欄位（密碼、Token、金鑰）外洩至 LLM 上下文。
5. 100% 記憶體運作，不產生任何磁碟暫存檔（零落盤）。
6. 支援彈性路徑設定：DB_PATH 可於 mcp_config.yaml 預填、留空、設為 null 或省略；
   未設定或為空時於執行期互動詢問並快取於記憶體。
"""

import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

try:
    from pocket_agent.system.constants import DEFAULT_TOOL_OUTPUT_CHARS

    MAX_OUTPUT_CHARS = DEFAULT_TOOL_OUTPUT_CHARS
except ImportError:
    MAX_OUTPUT_CHARS = 12000

mcp = MCPServer("sqlite")

# 執行期記憶體快取（僅存活於當前行程生命週期，不落盤）
_RUNTIME_CREDS: dict[str, str] = {}

_REDACT_SECRET_RE = re.compile(
    r"(?im)\b(?:[\w-]*(?:password|passwd|secret|token|passphrase|api[_-]?key)[\w-]*"
    r"|authorization|private_key|preshared_key)\b[^\r\n\x00]*"
)
_REDACT_KV_RE = re.compile(
    r"(?i)(\b(?:password|passwd|secret|token|api_key|apikey|client_secret|passphrase|connectionstring)\b"
    r"\s*[:=]\s*)([^\s#;\"',)]+|\"[^\"]*\"|'[^']*')"
)


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
        parts = [f"{ts} [AUDIT] [MCP:sqlite] tool={tool_name} status={status}"]
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


def _redact_output(text: str) -> str:
    """自動遮罩查詢結果中的敏感機密欄位與鍵值。"""
    if not text:
        return text
    return _REDACT_SECRET_RE.sub("[REDACTED]", text)


def _is_empty_or_null(val: str | None) -> bool:
    """檢查字串是否為空值、空白、null 或無效預留值。"""
    if val is None:
        return True
    cleaned = val.strip()
    return not cleaned or cleaned.lower() in ("null", "none", "~")


def _tty_prompt(prompt_text: str) -> str:
    """當未於設定檔指定 DB_PATH 時，直接透過作業系統終端機設備（CONIN$/CONOUT$ 或 /dev/tty）詢問使用者。"""
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
            "沒有可用的互動終端機，請於環境變數或 mcp_config.yaml 設定 DB_PATH；不讀取 MCP stdin。"
        ) from exc


def get_db_path(custom_path: str = "") -> Path:
    """解析並驗證 DB_PATH 目標檔案路徑。若未設定或為空，則提示使用者輸入並快取於記憶體。"""
    raw_path = custom_path.strip().strip("'\"") if custom_path else ""

    if _is_empty_or_null(raw_path):
        env_val = os.getenv("DB_PATH")
        if not _is_empty_or_null(env_val):
            raw_path = env_val.strip().strip("'\"")
        elif not _is_empty_or_null(_RUNTIME_CREDS.get("DB_PATH")):
            raw_path = _RUNTIME_CREDS["DB_PATH"]
        else:
            prompt_text = "請輸入要連線的 SQLite 資料庫完整路徑與檔名 (DB_PATH): "
            raw_path = _tty_prompt(prompt_text).strip().strip("'\"")

    if _is_empty_or_null(raw_path):
        raise ValueError("未提供 SQLite 資料庫路徑 (DB_PATH)。")

    p = Path(raw_path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"找不到指定的資料庫檔案：{p}")
    if not p.is_file():
        raise ValueError(f"指定的資料庫路徑並非檔案：{p}")

    _RUNTIME_CREDS["DB_PATH"] = str(p)
    return p


def _get_readonly_connection(tool_name: str = "sqlite", sql_context: str = "") -> sqlite3.Connection:
    """以唯讀 URI 模式開啟 SQLite 連線，並註冊 authorizer 阻止任何寫入操作。"""
    db_path = get_db_path()
    # 透過 file: URI 加上 mode=ro 強制唯讀
    uri_path = db_path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri_path, uri=True)

    def _authorizer(action_code, arg1, arg2, db_name, trigger_name):
        # 針對 PRAGMA 進行細部限制：僅允許安全的結構探查 PRAGMA，嚴禁路徑或設定 PRAGMA
        if action_code == sqlite3.SQLITE_PRAGMA:
            allowed_pragmas = {"table_info", "table_xinfo", "index_list", "index_info", "foreign_key_list"}
            pragma_name = str(arg1 or "").lower()
            if pragma_name in allowed_pragmas:
                return sqlite3.SQLITE_OK
            _audit_log(
                tool_name,
                sql_context or f"PRAGMA {arg1}",
                target=str(db_path),
                status="SECURITY_BLOCK",
                detail=f"Authorizer blocked PRAGMA {arg1!r}",
            )
            return sqlite3.SQLITE_DENY

        # 允許讀取、查詢相關操作，阻擋任何建立、寫入、修改、刪除操作
        allowed_actions = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
        }
        if action_code in allowed_actions:
            return sqlite3.SQLITE_OK
        _audit_log(
            tool_name,
            sql_context or f"action={action_code}",
            target=str(db_path),
            status="SECURITY_BLOCK",
            detail=f"Authorizer blocked action_code={action_code} arg1={arg1!r} arg2={arg2!r}",
        )
        return sqlite3.SQLITE_DENY

    conn.set_authorizer(_authorizer)
    return conn


@mcp.tool()
def sqlite_list_tables() -> str:
    """列出 SQLite 資料庫中的所有資料表"""
    conn = None
    sql = "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
    try:
        conn = _get_readonly_connection("sqlite_list_tables", sql)
        target = _RUNTIME_CREDS.get("DB_PATH", "")
        cursor = conn.cursor()
        cursor.execute(sql)
        tables = cursor.fetchall()
        _audit_log("sqlite_list_tables", sql, target=target, status="OK", detail=f"tables={len(tables)}")
        if not tables:
            return "資料庫中沒有資料表。"
        return "\n".join(t[0] for t in tables)
    except Exception as e:
        _audit_log(
            "sqlite_list_tables",
            sql,
            target=_RUNTIME_CREDS.get("DB_PATH") or (os.getenv("DB_PATH") or ""),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    finally:
        if conn:
            conn.close()


@mcp.tool()
def sqlite_describe_schema(table_name: str) -> str:
    """顯示指定 SQLite 資料表的 Schema (欄位與型別)"""
    conn = None
    clean_name = table_name.strip().strip("'\"`")
    sql = "SELECT name, type, [notnull], dflt_value, pk FROM pragma_table_info(?);"
    try:
        if not clean_name:
            _audit_log(
                "sqlite_describe_schema",
                f"{sql} [table_name='']",
                target=_RUNTIME_CREDS.get("DB_PATH") or (os.getenv("DB_PATH") or ""),
                status="BLOCKED",
                detail="缺少 table_name 參數",
            )
            return "[ERROR] 缺少 table_name 參數。"

        conn = _get_readonly_connection("sqlite_describe_schema", f"{sql} [table={clean_name!r}]")
        target = _RUNTIME_CREDS.get("DB_PATH", "")
        cursor = conn.cursor()
        # 透過參數化查詢 table_info 避免注入，並相容各種命名的資料表
        cursor.execute(sql, (clean_name,))
        columns = cursor.fetchall()
        if not columns:
            _audit_log(
                "sqlite_describe_schema",
                f"{sql} [table={clean_name!r}]",
                target=target,
                status="NOT_FOUND",
                detail=f"找不到資料表 {clean_name}",
            )
            return f"找不到資料表 {clean_name}。"
        _audit_log(
            "sqlite_describe_schema",
            f"{sql} [table={clean_name!r}]",
            target=target,
            status="OK",
            detail=f"columns={len(columns)}",
        )
        lines = []
        for c in columns:
            col_name, col_type, not_null, is_pk = c[0], c[1] or "TEXT", c[2], c[4]
            pk_tag = " [PK]" if is_pk else ""
            null_tag = " NOT NULL" if not_null else ""
            lines.append(f"{col_name} ({col_type}{pk_tag}{null_tag})")
        return "\n".join(lines)
    except Exception as e:
        _audit_log(
            "sqlite_describe_schema",
            f"{sql} [table={clean_name!r}]",
            target=_RUNTIME_CREDS.get("DB_PATH") or (os.getenv("DB_PATH") or ""),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] {e}"
    finally:
        if conn:
            conn.close()


@mcp.tool()
def sqlite_run_select_query(query: str) -> str:
    """執行唯讀的 SELECT 或 WITH 查詢以檢視資料 (具備串流防爆與機密欄位遮罩)"""
    clean_query = query.strip()
    upper_q = clean_query.upper()
    if not (upper_q.startswith("SELECT") or upper_q.startswith("WITH")):
        _audit_log(
            "sqlite_run_select_query",
            clean_query,
            target=_RUNTIME_CREDS.get("DB_PATH") or (os.getenv("DB_PATH") or ""),
            status="BLOCKED",
            detail="僅允許執行以 SELECT 或 WITH 開頭之唯讀查詢",
        )
        return "[ERROR] 基於安全考量，僅允許執行以 SELECT 或 WITH 開頭之唯讀查詢。"

    conn = None
    try:
        conn = _get_readonly_connection("sqlite_run_select_query", clean_query)
        target = _RUNTIME_CREDS.get("DB_PATH", "")
        cursor = conn.cursor()
        cursor.execute(clean_query)
        if not cursor.description:
            _audit_log("sqlite_run_select_query", clean_query, target=target, status="OK", detail="rows=0")
            return "查詢未產生欄位結果。"

        col_names = [description[0] for description in cursor.description]
        result = [", ".join(col_names)]
        chars_count = len(result[0]) + 1
        total_rows = 0
        truncated = False

        # 批次提取防範大表 OOM
        batch_size = 100
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                break
            for row in batch:
                total_rows += 1
                row_str = ", ".join(str(val) for val in row)
                if chars_count + len(row_str) + 1 > MAX_OUTPUT_CHARS:
                    truncated = True
                    break
                result.append(row_str)
                chars_count += len(row_str) + 1
            if truncated:
                break

        if total_rows == 0:
            _audit_log("sqlite_run_select_query", clean_query, target=target, status="OK", detail="rows=0")
            return "查詢結果為空。"

        raw_output = "\n".join(result)
        redacted = _redact_output(raw_output)
        shown_rows = len(result) - 1
        _audit_log(
            "sqlite_run_select_query",
            clean_query,
            target=target,
            status="TRUNCATED" if truncated else "OK",
            detail=f"rows={shown_rows}",
        )

        if truncated:
            redacted += (
                f"\n\n... (查詢結果已截斷，達單次上限 {MAX_OUTPUT_CHARS} 字元，"
                f"已呈現前 {shown_rows} 筆紀錄)\n"
                "[提示：若需檢視特定區段資料，請在 SQL 中加入 WHERE 條件或 LIMIT/OFFSET 分頁]"
            )
        return redacted
    except Exception as e:
        _audit_log(
            "sqlite_run_select_query",
            clean_query,
            target=_RUNTIME_CREDS.get("DB_PATH") or (os.getenv("DB_PATH") or ""),
            status="ERROR",
            detail=str(e),
        )
        return f"[ERROR] 查詢失敗: {e}"
    finally:
        if conn:
            conn.close()


# 向後相容別名 (Backward compatibility aliases)
db_list_tables = sqlite_list_tables
db_describe_schema = sqlite_describe_schema
db_run_select_query = sqlite_run_select_query


if __name__ == "__main__":
    mcp.run()

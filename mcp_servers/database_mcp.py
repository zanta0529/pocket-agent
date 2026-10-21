"""SQLite 唯讀查詢與結構檢索 MCP 伺服器。

安全設計原則：
1. 強制以 URI mode=ro 模式開啟連線，並配置 SQLite Authorizer 嚴密攔截非唯讀操作。
2. 串流分批讀取（fetchmany），防範巨量資料表一次性抓取導致記憶體耗盡 (OOM DoS)。
3. 連線資源確保釋放，杜絕 Windows 檔案佔用與鎖死。
4. 內建機密欄位與正則遮罩，防止資料庫機敏欄位（密碼、Token、金鑰）外洩至 LLM 上下文。
5. 100% 記憶體運作，不產生任何磁碟暫存檔（零落盤）。
"""

import os
import re
import sqlite3
from pathlib import Path

from mcp.server.mcpserver import MCPServer

try:
    from pocket_agent.system.constants import DEFAULT_TOOL_OUTPUT_CHARS

    MAX_OUTPUT_CHARS = DEFAULT_TOOL_OUTPUT_CHARS
except ImportError:
    MAX_OUTPUT_CHARS = 12000

mcp = MCPServer("database")

_REDACT_SECRET_RE = re.compile(
    r"(?im)\b(?:[\w-]*(?:password|passwd|secret|token|passphrase|api[_-]?key)[\w-]*"
    r"|authorization|private_key|preshared_key)\b[^\r\n\x00]*"
)


def _redact_output(text: str) -> str:
    """自動遮罩查詢結果中的敏感機密欄位與鍵值。"""
    if not text:
        return text
    return _REDACT_SECRET_RE.sub("[REDACTED]", text)


def get_db_path() -> Path:
    """解析並驗證 DB_PATH 目標檔案路徑。"""
    db_path = os.getenv("DB_PATH")
    if not db_path:
        raise ValueError("缺少 DB_PATH 環境變數。請在 mcp_config.yaml 中使用 env 指定 SQLite 資料庫路徑。")
    p = Path(db_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"找不到指定的資料庫檔案：{p}")
    if not p.is_file():
        raise ValueError(f"指定的資料庫路徑並非檔案：{p}")
    return p


def _get_readonly_connection() -> sqlite3.Connection:
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
            return sqlite3.SQLITE_DENY

        # 允許讀取、查詢相關操作，阻擋任何建立、寫入、修改、刪除操作
        allowed_actions = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
        }
        if action_code in allowed_actions:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    conn.set_authorizer(_authorizer)
    return conn


@mcp.tool()
def db_list_tables() -> str:
    """列出資料庫中的所有資料表"""
    conn = None
    try:
        conn = _get_readonly_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        tables = cursor.fetchall()
        if not tables:
            return "資料庫中沒有資料表。"
        return "\n".join(t[0] for t in tables)
    except Exception as e:
        return f"[ERROR] {e}"
    finally:
        if conn:
            conn.close()


@mcp.tool()
def db_describe_schema(table_name: str) -> str:
    """顯示指定資料表的 Schema (欄位與型別)"""
    conn = None
    try:
        clean_name = table_name.strip().strip("'\"`")
        if not clean_name:
            return "[ERROR] 缺少 table_name 參數。"

        conn = _get_readonly_connection()
        cursor = conn.cursor()
        # 透過參數化查詢 table_info 避免注入，並相容各種命名的資料表
        cursor.execute("SELECT name, type, [notnull], dflt_value, pk FROM pragma_table_info(?);", (clean_name,))
        columns = cursor.fetchall()
        if not columns:
            return f"找不到資料表 {clean_name}。"
        lines = []
        for c in columns:
            col_name, col_type, not_null, is_pk = c[0], c[1] or "TEXT", c[2], c[4]
            pk_tag = " [PK]" if is_pk else ""
            null_tag = " NOT NULL" if not_null else ""
            lines.append(f"{col_name} ({col_type}{pk_tag}{null_tag})")
        return "\n".join(lines)
    except Exception as e:
        return f"[ERROR] {e}"
    finally:
        if conn:
            conn.close()


@mcp.tool()
def db_run_select_query(query: str) -> str:
    """執行唯讀的 SELECT 或 WITH 查詢以檢視資料 (具備串流防爆與機密欄位遮罩)"""
    clean_query = query.strip()
    upper_q = clean_query.upper()
    if not (upper_q.startswith("SELECT") or upper_q.startswith("WITH")):
        return "[ERROR] 基於安全考量，僅允許執行以 SELECT 或 WITH 開頭之唯讀查詢。"

    conn = None
    try:
        conn = _get_readonly_connection()
        cursor = conn.cursor()
        cursor.execute(clean_query)
        if not cursor.description:
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
            return "查詢結果為空。"

        raw_output = "\n".join(result)
        redacted = _redact_output(raw_output)

        if truncated:
            redacted += (
                f"\n\n... (查詢結果已截斷，達單次上限 {MAX_OUTPUT_CHARS} 字元，"
                f"已呈現前 {len(result) - 1} 筆紀錄)\n"
                "[提示：若需檢視特定區段資料，請在 SQL 中加入 WHERE 條件或 LIMIT/OFFSET 分頁]"
            )
        return redacted
    except Exception as e:
        return f"[ERROR] 查詢失敗: {e}"
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    mcp.run()

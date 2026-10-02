import os
import sqlite3

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("database")


def get_db_path():
    db_path = os.getenv("DB_PATH")
    if not db_path:
        raise ValueError("缺少 DB_PATH 環境變數。請在 mcp_config.yaml 中使用 env 指定 SQLite 資料庫路徑。")
    return db_path


@mcp.tool()
def db_list_tables() -> str:
    """列出資料庫中的所有資料表"""
    try:
        with sqlite3.connect(get_db_path()) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = cursor.fetchall()
            if not tables:
                return "資料庫中沒有資料表。"
            return "\n".join(t[0] for t in tables)
    except Exception as e:
        return f"[ERROR] {e}"


@mcp.tool()
def db_describe_schema(table_name: str) -> str:
    """顯示指定資料表的 Schema (欄位與型別)"""
    try:
        with sqlite3.connect(get_db_path()) as conn:
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA table_info({table_name});")
            columns = cursor.fetchall()
            if not columns:
                return f"找不到資料表 {table_name}。"
            return "\n".join(f"{c[1]} ({c[2]})" for c in columns)
    except Exception as e:
        return f"[ERROR] {e}"


@mcp.tool()
def db_run_select_query(query: str) -> str:
    """執行唯讀的 SELECT 查詢以檢視資料"""
    if not query.strip().upper().startswith("SELECT"):
        return "[ERROR] 基於安全考量，僅允許執行 SELECT 查詢。"

    try:
        with sqlite3.connect(get_db_path()) as conn:
            cursor = conn.cursor()
            cursor.execute(query)
            rows = cursor.fetchall()
            if not rows:
                return "查詢結果為空。"
            # 取出欄位名稱
            col_names = [description[0] for description in cursor.description]
            result = [", ".join(col_names)]
            for r in rows:
                result.append(", ".join(str(val) for val in r))

            output = "\n".join(result)
            max_len = 5000
            if len(output) > max_len:
                return output[:max_len] + "\n... (結果太長已截斷)"
            return output
    except Exception as e:
        return f"[ERROR] 查詢失敗: {e}"


if __name__ == "__main__":
    mcp.run()

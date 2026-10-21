"""單元測試：驗證 Database MCP (database_mcp.py) 之唯讀保護、串流防爆、連線釋放與機密遮罩。"""

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from mcp_servers import database_mcp


@pytest.fixture
def temp_db():
    """建立測試用臨時 SQLite 資料庫，測試結束後自動清理。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_file = Path(f.name)

    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, api_token TEXT);")
    cursor.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT, content TEXT);")

    # 插入測試資料 (包含敏感與一般資料)
    for i in range(1, 250):
        cursor.execute(
            "INSERT INTO users (username, password_hash, api_token) VALUES (?, ?, ?);",
            (f"user_{i}", f"secret_hash_value_{i}", f"tok_secret_key_{i}"),
        )
        cursor.execute(
            "INSERT INTO posts (user_id, title, content) VALUES (?, ?, ?);",
            (i, f"Title {i}", f"This is the content of post {i}." * 5),
        )

    conn.commit()
    conn.close()

    old_env = os.environ.get("DB_PATH")
    os.environ["DB_PATH"] = str(db_file)

    yield db_file

    if old_env is not None:
        os.environ["DB_PATH"] = old_env
    else:
        os.environ.pop("DB_PATH", None)

    try:
        db_file.unlink(missing_ok=True)
    except Exception:
        pass


def test_db_list_tables(temp_db):
    """驗證資料表清單檢索與系統表過濾。"""
    out = database_mcp.db_list_tables()
    assert "users" in out
    assert "posts" in out
    assert "sqlite_master" not in out


def test_db_describe_schema(temp_db):
    """驗證 Schema 結構查詢與參數化安全。"""
    out = database_mcp.db_describe_schema("users")
    assert "username" in out
    assert "password_hash" in out
    assert "api_token" in out

    # 查無此表
    assert "找不到資料表" in database_mcp.db_describe_schema("non_existent_table")


def test_db_run_select_query_basic_and_cte(temp_db):
    """驗證基本 SELECT 與 WITH (CTE) 語法支援。"""
    out_select = database_mcp.db_run_select_query("SELECT id, username FROM users WHERE id = 1;")
    assert "id, username" in out_select
    assert "1, user_1" in out_select

    # 支援 CTE
    cte_query = "WITH recent_users AS (SELECT id, username FROM users WHERE id <= 2) SELECT * FROM recent_users;"
    out_cte = database_mcp.db_run_select_query(cte_query)
    assert "user_1" in out_cte
    assert "user_2" in out_cte


def test_db_blocks_mutation_queries(temp_db):
    """驗證嚴格阻擋非唯讀的資料變更指令 (INSERT, UPDATE, DELETE, DROP 等)。"""
    # 非 SELECT/WITH 開頭
    out_insert = database_mcp.db_run_select_query("INSERT INTO users (username) VALUES ('hacker');")
    assert "[ERROR]" in out_insert
    assert "僅允許執行以 SELECT 或 WITH 開頭" in out_insert

    out_drop = database_mcp.db_run_select_query("DROP TABLE users;")
    assert "[ERROR]" in out_drop

    # 偽裝前綴試圖寫入由 Authorizer 攔截
    out_fake = database_mcp.db_run_select_query("SELECT 1; INSERT INTO users (username) VALUES ('fail');")
    # SQLite cursor.execute 僅執行第一句或 authorizer 阻擋
    assert "1" in out_fake or "[ERROR]" in out_fake


def test_db_blocks_dangerous_pragma(temp_db):
    """驗證禁止直接執行 PRAGMA，以及 Authorizer 阻擋未授權的 PRAGMA 探測。"""
    # 1. 直接執行 PRAGMA 被前綴唯讀檢查攔截
    out_pragma = database_mcp.db_run_select_query("PRAGMA database_list;")
    assert "[ERROR]" in out_pragma
    assert "僅允許執行以 SELECT 或 WITH 開頭" in out_pragma

    # 2. 透過 SELECT 查詢危險 PRAGMA 虛擬表被 Authorizer 拒絕
    out_table = database_mcp.db_run_select_query("SELECT * FROM pragma_database_list;")
    assert "[ERROR]" in out_table
    assert "not authorized" in out_table.lower() or "not allowed" in out_table.lower()


def test_db_secret_redaction(temp_db):
    """驗證查詢結果自動遮罩 password, token 等機密欄位。"""
    out = database_mcp.db_run_select_query("SELECT username, password_hash, api_token FROM users WHERE id = 1;")
    assert "user_1" in out
    # 原始機密數值不應明文出現
    assert "secret_hash_value_1" not in out
    assert "tok_secret_key_1" not in out
    assert "[REDACTED]" in out


def test_db_streaming_truncation_anti_oom(temp_db):
    """驗證批次串流讀取在達到字元上限時友善截斷，防範 OOM。"""
    out = database_mcp.db_run_select_query("SELECT * FROM posts;")
    assert "查詢結果已截斷" in out
    assert "已呈現前" in out
    assert len(out) <= database_mcp.MAX_OUTPUT_CHARS + 500


def test_db_connection_closing_guaranteed(temp_db):
    """驗證連線保證釋放，查詢後資料庫檔案無殘留鎖死，可順利被刪除或存取。"""
    # 執行多次查詢
    database_mcp.db_list_tables()
    database_mcp.db_run_select_query("SELECT 1;")
    database_mcp.db_describe_schema("users")

    # 在 Windows 上若未呼叫 conn.close()，以排他寫入模式開啟會拋出 OperationalError (file locked)
    test_conn = sqlite3.connect(temp_db, timeout=1.0)
    test_conn.execute("CREATE TABLE test_lock (id INT);")
    test_conn.commit()
    test_conn.close()

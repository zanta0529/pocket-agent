"""單元測試：驗證 MySQL MCP (mysql_mcp.py) 之唯讀保護、串流防爆、連線釋放、機密遮罩、互動詢問與 MCPManager 整合。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_servers import mysql_mcp
from pocket_agent.extensions.mcp import MCPManager


@pytest.fixture(autouse=True)
def _clean_mysql_env_and_cache(monkeypatch, tmp_path):
    """每次測試前清空 MySQL MCP 執行期記憶體快取、環境變數並隔離測試用 agent.log。"""
    mysql_mcp._clear_creds_cache()
    monkeypatch.setenv("POCKET_AGENT_LOG_FILE", str(tmp_path / "agent.log"))
    for key in (
        "MYSQL_HOST",
        "MYSQL_PORT",
        "MYSQL_USER",
        "MYSQL_PASSWORD",
        "MYSQL_DATABASE",
        "MYSQL_CONNECT_TIMEOUT",
        "MYSQL_CHARSET",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    mysql_mcp._clear_creds_cache()


def _set_full_mysql_env(monkeypatch):
    monkeypatch.setenv("MYSQL_HOST", "192.168.10.20")
    monkeypatch.setenv("MYSQL_PORT", "3306")
    monkeypatch.setenv("MYSQL_USER", "readonly_user")
    monkeypatch.setenv("MYSQL_PASSWORD", "MySecretPass!123")
    monkeypatch.setenv("MYSQL_DATABASE", "app_db")


def _make_fake_conn(rows=None, description=None):
    cursor = MagicMock()
    batch_rows = list(rows or [])

    def fake_fetchmany(size=100):
        nonlocal batch_rows
        if not batch_rows:
            return []
        chunk = batch_rows[:size]
        batch_rows = batch_rows[size:]
        return chunk

    cursor.fetchall.return_value = list(rows or [])
    cursor.fetchmany.side_effect = fake_fetchmany
    cursor.description = description

    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


def test_mysql_list_databases(monkeypatch):
    """驗證資料庫清單檢索（無須預設指定 MYSQL_DATABASE）。"""
    monkeypatch.setenv("MYSQL_HOST", "192.168.10.20")
    monkeypatch.setenv("MYSQL_PORT", "3306")
    monkeypatch.setenv("MYSQL_USER", "readonly_user")
    monkeypatch.setenv("MYSQL_PASSWORD", "MySecretPass!123")
    conn, _ = _make_fake_conn(rows=[("app_db",), ("analytics_db",)])
    fake_driver = SimpleNamespace(connect=MagicMock(return_value=conn))

    with patch("mcp_servers.mysql_mcp._load_mysql_driver", return_value=("pymysql", fake_driver)):
        out = mysql_mcp.mysql_list_databases()

    assert out == "app_db\nanalytics_db"
    conn.rollback.assert_called_once()
    conn.close.assert_called_once()


def test_mysql_list_tables(monkeypatch):
    """驗證資料表清單檢索與連線唯讀初始化（含未指定預設資料庫時的跨庫列表）。"""
    _set_full_mysql_env(monkeypatch)
    conn, cursor = _make_fake_conn(rows=[("orders",), ("users",)])
    fake_driver = SimpleNamespace(connect=MagicMock(return_value=conn))

    with patch("mcp_servers.mysql_mcp._load_mysql_driver", return_value=("pymysql", fake_driver)):
        out = mysql_mcp.mysql_list_tables()

    assert out == "orders\nusers"
    fake_driver.connect.assert_called_once()
    executed_sqls = [call.args[0] for call in cursor.execute.call_args_list]
    assert "SET SESSION TRANSACTION READ ONLY;" in executed_sqls
    assert "START TRANSACTION READ ONLY;" in executed_sqls
    conn.rollback.assert_called_once()
    conn.close.assert_called_once()

    # 未設定預設資料庫時，應列出 database.table 格式
    monkeypatch.delenv("MYSQL_DATABASE", raising=False)
    mysql_mcp._clear_creds_cache()
    conn_all, _ = _make_fake_conn(rows=[("app_db", "orders"), ("sales_db", "invoices")])
    fake_driver_all = SimpleNamespace(connect=MagicMock(return_value=conn_all))
    with patch("mcp_servers.mysql_mcp._load_mysql_driver", return_value=("pymysql", fake_driver_all)):
        out_all = mysql_mcp.mysql_list_tables()
    assert out_all == "app_db.orders\nsales_db.invoices"


def test_mysql_describe_schema(monkeypatch):
    """驗證 Schema 結構查詢、參數化防注入與欄位格式化。"""
    _set_full_mysql_env(monkeypatch)
    schema_rows = [
        ("id", "bigint unsigned", "NO", None, "PRI"),
        ("username", "varchar(64)", "NO", None, "UNI"),
        ("bio", "text", "YES", None, ""),
    ]
    conn, cursor = _make_fake_conn(rows=schema_rows)
    fake_driver = SimpleNamespace(connect=MagicMock(return_value=conn))

    with patch("mcp_servers.mysql_mcp._load_mysql_driver", return_value=("pymysql", fake_driver)):
        out = mysql_mcp.mysql_describe_schema("users")

    assert "id (BIGINT UNSIGNED [PK] NOT NULL)" in out
    assert "username (VARCHAR(64) NOT NULL)" in out
    assert "bio (TEXT)" in out
    last_call = cursor.execute.call_args_list[-1]
    assert last_call.args[1] == ("app_db", "users")

    # 測試查無資料表
    conn_empty, _ = _make_fake_conn(rows=[])
    fake_driver_empty = SimpleNamespace(connect=MagicMock(return_value=conn_empty))
    with patch("mcp_servers.mysql_mcp._load_mysql_driver", return_value=("pymysql", fake_driver_empty)):
        assert "找不到資料表" in mysql_mcp.mysql_describe_schema("non_existent")

    # 測試非法資料表名稱攔截
    with patch("mcp_servers.mysql_mcp._get_readonly_connection") as mock_conn:
        bad_out = mysql_mcp.mysql_describe_schema("users; DROP TABLE users;--")
        assert bad_out.startswith("[ERROR]")
        mock_conn.assert_not_called()


def test_mysql_run_select_query_basic_and_cte(monkeypatch):
    """驗證基本 SELECT、開頭註解與 WITH (CTE) 語法支援。"""
    _set_full_mysql_env(monkeypatch)
    conn, _ = _make_fake_conn(
        rows=[(1, "alice"), (2, "bob")],
        description=[("id",), ("username",)],
    )
    with patch("mcp_servers.mysql_mcp._get_readonly_connection", return_value=conn):
        out = mysql_mcp.mysql_run_select_query("-- 查詢使用者\nSELECT id, username FROM users;")
    assert "id, username" in out
    assert "1, alice" in out
    assert "2, bob" in out

    conn_cte, _ = _make_fake_conn(
        rows=[(1, "alice")],
        description=[("id",), ("username",)],
    )
    cte_sql = "/* CTE */ WITH active_u AS (SELECT id, username FROM users) SELECT * FROM active_u;"
    with patch("mcp_servers.mysql_mcp._get_readonly_connection", return_value=conn_cte):
        out_cte = mysql_mcp.mysql_run_select_query(cte_sql)
    assert "1, alice" in out_cte


@pytest.mark.parametrize(
    "bad_query",
    [
        "INSERT INTO users (username) VALUES ('hacker');",
        "UPDATE users SET username = 'pwn' WHERE id = 1;",
        "DELETE FROM users;",
        "DROP TABLE users;",
        "SHOW VARIABLES;",
        "SELECT 1; DROP TABLE users;",
        "SELECT * FROM users INTO OUTFILE '/tmp/dump.txt';",
        "SELECT * FROM users FOR UPDATE;",
        "SELECT * FROM users LOCK IN SHARE MODE;",
        "SELECT SLEEP(10);",
    ],
)
def test_mysql_blocks_mutation_and_dangerous_queries(monkeypatch, bad_query):
    """驗證嚴格阻擋非唯讀、多重語句、鎖表、寫檔與延遲攻擊指令。"""
    _set_full_mysql_env(monkeypatch)
    with patch("mcp_servers.mysql_mcp._get_readonly_connection") as mock_conn:
        out = mysql_mcp.mysql_run_select_query(bad_query)
        assert out.startswith("[ERROR]")
        mock_conn.assert_not_called()


def test_mysql_secret_redaction(monkeypatch):
    """驗證查詢結果自動遮罩敏感欄位名稱（即使值為一般雜湊）與敏感內容字串。"""
    _set_full_mysql_env(monkeypatch)
    conn, _ = _make_fake_conn(
        rows=[(1, "user_1", "e10adc3949ba59abbe56e057f20f883e", "plain_tok_123", "api_key=my_inline_secret")],
        description=[("id",), ("username",), ("password_hash",), ("api_token",), ("notes",)],
    )
    with patch("mcp_servers.mysql_mcp._get_readonly_connection", return_value=conn):
        out = mysql_mcp.mysql_run_select_query(
            "SELECT id, username, password_hash, api_token, notes FROM users WHERE id = 1;"
        )

    assert "user_1" in out
    assert "e10adc3949ba59abbe56e057f20f883e" not in out
    assert "plain_tok_123" not in out
    assert "my_inline_secret" not in out
    assert "[REDACTED]" in out


def test_mysql_streaming_truncation_anti_oom(monkeypatch):
    """驗證批次串流讀取在達到字元上限時友善截斷，防範 OOM。"""
    _set_full_mysql_env(monkeypatch)
    large_rows = [(i, f"Title {i}", "Content body text " * 20) for i in range(1, 150)]
    conn, _ = _make_fake_conn(
        rows=large_rows,
        description=[("id",), ("title",), ("content",)],
    )
    with patch("mcp_servers.mysql_mcp._get_readonly_connection", return_value=conn):
        out = mysql_mcp.mysql_run_select_query("SELECT id, title, content FROM posts;")

    assert "查詢結果已截斷" in out
    assert "已呈現前" in out
    assert len(out) <= mysql_mcp.MAX_OUTPUT_CHARS + 500


def test_mysql_connection_closing_guaranteed_on_error(monkeypatch):
    """驗證查詢發生例外時，連線依然確保 rollback 與 close。"""
    _set_full_mysql_env(monkeypatch)
    conn, cursor = _make_fake_conn()
    cursor.execute.side_effect = RuntimeError("Table 'app_db.missing' doesn't exist")

    with patch("mcp_servers.mysql_mcp._get_readonly_connection", return_value=conn):
        out = mysql_mcp.mysql_run_select_query("SELECT * FROM missing;")

    assert out.startswith("[ERROR] 查詢失敗:")
    conn.rollback.assert_called_once()
    conn.close.assert_called_once()


@pytest.mark.parametrize("empty_val", ["", "   ", "null", "NULL", "None", "none", "~"])
def test_mysql_empty_or_null_env_prompts_and_caches(monkeypatch, empty_val):
    """驗證未設定或為空值/null 時透過 _tty_prompt 互動詢問（Database 為選填不詢問）並快取於記憶體。"""
    monkeypatch.setenv("MYSQL_HOST", empty_val)
    monkeypatch.setenv("MYSQL_DATABASE", empty_val)
    prompts = iter(["db.internal.local", "3307", "dbuser", "PromptedPass!99"])

    with patch("mcp_servers.mysql_mcp._tty_prompt", side_effect=lambda *a, **kw: next(prompts)) as mock_prompt:
        resolved = mysql_mcp._resolve_connection_params()
        assert resolved == ("db.internal.local", "3307", "dbuser", "PromptedPass!99", "")
        assert mock_prompt.call_count == 4

        # 第二次呼叫直接命中快取，不再詢問
        resolved2 = mysql_mcp._resolve_connection_params()
        assert resolved2 == resolved
        assert mock_prompt.call_count == 4


def test_mysql_cross_host_does_not_leak_cached_password():
    """驗證切換不同目標主機時，禁止沿用舊主機密碼快取。"""
    mysql_mcp._RUNTIME_CREDS.update(
        {
            "MYSQL_HOST": "10.0.0.1",
            "MYSQL_PORT": "3306",
            "MYSQL_USER": "admin",
            "MYSQL_PASSWORD": "OldHostPassword!1",
            "MYSQL_DATABASE": "db1",
        }
    )
    mysql_mcp._CREDS_UPDATED_AT = mysql_mcp.time.monotonic()

    with patch("mcp_servers.mysql_mcp._tty_prompt", return_value="NewHostPassword!2") as mock_prompt:
        host, port, user, pw, db = mysql_mcp._resolve_connection_params(mysql_host="10.0.0.2")
        assert host == "10.0.0.2"
        assert pw == "NewHostPassword!2"
        mock_prompt.assert_called_once()


def test_mysql_driver_fallback_and_missing_driver_error(monkeypatch):
    """驗證 pymysql 與 mysql.connector 延遲載入降級，以及無驅動時的明確錯誤訊息且不清除快取。"""
    _set_full_mysql_env(monkeypatch)
    fake_connector = SimpleNamespace(connect=MagicMock())

    def fake_import(name):
        if name == "pymysql":
            raise ImportError("No module named 'pymysql'")
        if name == "mysql.connector":
            return fake_connector
        raise ImportError(name)

    with patch("importlib.import_module", side_effect=fake_import):
        dtype, mod = mysql_mcp._load_mysql_driver()
        assert dtype == "mysql_connector"
        assert mod is fake_connector

    with patch("importlib.import_module", side_effect=ImportError("no driver")):
        with pytest.raises(RuntimeError, match="pip install pymysql"):
            mysql_mcp._load_mysql_driver()

        # 缺少驅動不應觸發 [CONN_AUTH]，亦不應清除已快取的連線憑證
        out = mysql_mcp.mysql_list_databases()
        assert "[CONN_AUTH]" not in out
        assert "pip install pymysql" in out
        assert mysql_mcp._RUNTIME_CREDS.get("MYSQL_HOST") == "192.168.10.20"


def test_mcp_manager_strips_mysql_params_and_clears_cache_on_conn_auth():
    """驗證 MCPManager 自動隱藏 mysql_* 連線參數、MYSQL_DATABASE 為選填不強制詢問，並在 [CONN_AUTH] 時清除快取。"""
    manager = MCPManager()
    manager._server_envs["mysql"] = {
        "MYSQL_HOST": "10.10.1.5",
        "MYSQL_PORT": "3306",
        "MYSQL_DATABASE": "null",
    }
    fake_tools = [
        SimpleNamespace(
            name="mysql_run_select_query",
            description="run select",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "mysql_host": {"type": "string"},
                    "mysql_port": {"type": "string"},
                    "mysql_user": {"type": "string"},
                    "mysql_password": {"type": "string"},
                    "mysql_database": {"type": "string"},
                },
                "required": ["query"],
            },
        ),
    ]
    fake_session = SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=fake_tools)),
        call_tool=AsyncMock(return_value=MagicMock(content=[MagicMock(text="id\n1")])),
    )
    asyncio.run(
        manager._register_tools(
            "mysql",
            fake_session,
            inject={},
            safe_read_tools=["mysql_run_select_query"],
            is_sensitive=True,
        )
    )
    schemas = {s["name"]: s for s in manager.openai_tools()}
    assert schemas["mysql_run_select_query"].get("_is_safe_read") is True
    assert set(schemas["mysql_run_select_query"]["parameters"]["properties"].keys()) == {"query"}
    assert manager.is_sensitive_tool("mysql_run_select_query") is True

    prompt_calls = []

    def fake_prompt(server_name, missing_keys):
        prompt_calls.append((server_name, missing_keys))
        return {
            "MYSQL_USER": "analyst",
            "MYSQL_PASSWORD": "SecretDbPw!",
        }

    manager.on_prompt_conn = fake_prompt
    asyncio.run(manager.call("mysql_run_select_query", {"query": "SELECT 1"}))

    # MYSQL_DATABASE 為選填，不應出現在 missing_keys 中
    assert prompt_calls == [("mysql", ["MYSQL_USER", "MYSQL_PASSWORD"])]
    _, sent_args = fake_session.call_tool.call_args.args
    assert sent_args == {
        "query": "SELECT 1",
        "mysql_host": "10.10.1.5",
        "mysql_port": "3306",
        "mysql_user": "analyst",
        "mysql_password": "SecretDbPw!",
        "mysql_database": "",
    }

    # 認證失敗 ([CONN_AUTH]) 應觸發快取清除並在下次重新詢問
    fake_session.call_tool = AsyncMock(
        return_value=MagicMock(content=[MagicMock(text="[ERROR] [CONN_AUTH] MySQL 連線或認證失敗: Access denied")])
    )
    asyncio.run(manager.call("mysql_run_select_query", {"query": "SELECT 1"}))
    fake_session.call_tool = AsyncMock(return_value=MagicMock(content=[MagicMock(text="id\n1")]))
    asyncio.run(manager.call("mysql_run_select_query", {"query": "SELECT 1"}))
    assert len(prompt_calls) == 2


def test_mysql_audit_log_records_queries_and_blocks_without_leaking_password(monkeypatch, tmp_path):
    """驗證 mysql_mcp.py 將執行的 SQL 指令與攔截事件寫入集中化 agent.log，且絕不洩漏密碼。"""
    _set_full_mysql_env(monkeypatch)
    log_file = tmp_path / "agent.log"
    conn, _ = _make_fake_conn(rows=[(1, "alice")], description=[("id",), ("username",)])

    with patch("mcp_servers.mysql_mcp._get_readonly_connection", return_value=conn):
        mysql_mcp.mysql_run_select_query("SELECT id, username FROM users WHERE id = 1;")
        mysql_mcp.mysql_run_select_query("DELETE FROM users;")

    content = log_file.read_text(encoding="utf-8")
    assert "[AUDIT] [MCP:mysql] tool=mysql_run_select_query status=OK" in content
    assert "target=readonly_user@192.168.10.20:3306/app_db" in content
    assert "SELECT id, username FROM users WHERE id = 1" in content
    assert "[AUDIT] [MCP:mysql] tool=mysql_run_select_query status=BLOCKED" in content
    assert "DELETE FROM users;" in content
    assert "MySecretPass!123" not in content

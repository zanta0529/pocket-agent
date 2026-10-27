"""單元測試：驗證 MSSQL MCP (mssql_mcp.py) 之唯讀保護、串流防爆、連線釋放、機密遮罩、互動詢問與 MCPManager 整合。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_servers import mssql_mcp
from pocket_agent.extensions.mcp import MCPManager


@pytest.fixture(autouse=True)
def _clean_mssql_env_and_cache(monkeypatch):
    """每次測試前清空 MSSQL MCP 執行期記憶體快取與環境變數。"""
    mssql_mcp._clear_creds_cache()
    for key in (
        "MSSQL_HOST",
        "MSSQL_PORT",
        "MSSQL_USER",
        "MSSQL_PASSWORD",
        "MSSQL_DATABASE",
        "MSSQL_CONNECT_TIMEOUT",
        "MSSQL_DRIVER",
        "MSSQL_ENCRYPT",
        "MSSQL_TRUST_SERVER_CERTIFICATE",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    mssql_mcp._clear_creds_cache()


def _set_full_mssql_env(monkeypatch):
    monkeypatch.setenv("MSSQL_HOST", "10.20.30.50")
    monkeypatch.setenv("MSSQL_PORT", "1433")
    monkeypatch.setenv("MSSQL_USER", r"CORP\sql_reader")
    monkeypatch.setenv("MSSQL_PASSWORD", "SqlSecret#2026")
    monkeypatch.setenv("MSSQL_DATABASE", "erp_db")


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


def test_mssql_list_databases(monkeypatch):
    """驗證資料庫清單檢索（無須預設指定 MSSQL_DATABASE）。"""
    monkeypatch.setenv("MSSQL_HOST", "10.20.30.50")
    monkeypatch.setenv("MSSQL_PORT", "1433")
    monkeypatch.setenv("MSSQL_USER", r"CORP\sql_reader")
    monkeypatch.setenv("MSSQL_PASSWORD", "SqlSecret#2026")
    conn, _ = _make_fake_conn(rows=[("erp_db",), ("hr_db",)])
    fake_driver = SimpleNamespace(connect=MagicMock(return_value=conn))

    with patch("mcp_servers.mssql_mcp._load_mssql_driver", return_value=("pymssql", fake_driver)):
        out = mssql_mcp.mssql_list_databases()

    assert out == "erp_db\nhr_db"
    conn.rollback.assert_called_once()
    conn.close.assert_called_once()


def test_mssql_list_tables(monkeypatch):
    """驗證資料表清單檢索（含 dbo 簡化顯示與自訂 Schema 顯示）及唯讀交易隔離設定。"""
    _set_full_mssql_env(monkeypatch)
    conn, cursor = _make_fake_conn(rows=[("dbo", "customers"), ("sales", "invoices")])
    fake_driver = SimpleNamespace(connect=MagicMock(return_value=conn))

    with patch("mcp_servers.mssql_mcp._load_mssql_driver", return_value=("pymssql", fake_driver)):
        out = mssql_mcp.mssql_list_tables()

    assert out == "customers\nsales.invoices"
    fake_driver.connect.assert_called_once()
    executed_sqls = [call.args[0] for call in cursor.execute.call_args_list]
    assert "SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;" in executed_sqls
    conn.rollback.assert_called_once()
    conn.close.assert_called_once()


def test_mssql_describe_schema(monkeypatch):
    """驗證 Schema 結構查詢、支援 schema.table 語法與參數化防注入。"""
    _set_full_mssql_env(monkeypatch)
    schema_rows = [
        ("id", "int", "NO", None, 1),
        ("order_no", "nvarchar", "NO", None, 0),
        ("remark", "nvarchar", "YES", None, 0),
    ]
    conn, cursor = _make_fake_conn(rows=schema_rows)
    fake_driver = SimpleNamespace(connect=MagicMock(return_value=conn))

    with patch("mcp_servers.mssql_mcp._load_mssql_driver", return_value=("pymssql", fake_driver)):
        out = mssql_mcp.mssql_describe_schema("sales.invoices")

    assert "id (INT [PK] NOT NULL)" in out
    assert "order_no (NVARCHAR NOT NULL)" in out
    assert "remark (NVARCHAR)" in out
    last_call = cursor.execute.call_args_list[-1]
    assert last_call.args[1] == ("sales", "invoices")

    # 測試查無資料表
    conn_empty, _ = _make_fake_conn(rows=[])
    fake_driver_empty = SimpleNamespace(connect=MagicMock(return_value=conn_empty))
    with patch("mcp_servers.mssql_mcp._load_mssql_driver", return_value=("pymssql", fake_driver_empty)):
        assert "找不到資料表" in mssql_mcp.mssql_describe_schema("missing_tbl")

    # 測試非法資料表名稱攔截
    with patch("mcp_servers.mssql_mcp._get_readonly_connection") as mock_conn:
        bad_out = mssql_mcp.mssql_describe_schema("users; EXEC xp_cmdshell 'whoami'")
        assert bad_out.startswith("[ERROR]")
        mock_conn.assert_not_called()


def test_mssql_run_select_query_basic_and_cte(monkeypatch):
    """驗證基本 SELECT 與 WITH (CTE) 語法支援。"""
    _set_full_mssql_env(monkeypatch)
    conn, _ = _make_fake_conn(
        rows=[(1, "cust_a"), (2, "cust_b")],
        description=[("id",), ("name",)],
    )
    with patch("mcp_servers.mssql_mcp._get_readonly_connection", return_value=conn):
        out = mssql_mcp.mssql_run_select_query("SELECT TOP 2 id, name FROM customers;")
    assert "id, name" in out
    assert "1, cust_a" in out
    assert "2, cust_b" in out

    conn_cte, _ = _make_fake_conn(
        rows=[(1, "cust_a")],
        description=[("id",), ("name",)],
    )
    cte_sql = "WITH top_c AS (SELECT id, name FROM customers) SELECT * FROM top_c;"
    with patch("mcp_servers.mssql_mcp._get_readonly_connection", return_value=conn_cte):
        out_cte = mssql_mcp.mssql_run_select_query(cte_sql)
    assert "1, cust_a" in out_cte


@pytest.mark.parametrize(
    "bad_query",
    [
        "INSERT INTO customers (name) VALUES ('evil');",
        "UPDATE customers SET name = 'evil';",
        "DELETE FROM customers;",
        "DROP TABLE customers;",
        "EXEC sp_who;",
        "EXECUTE xp_cmdshell 'dir';",
        "SELECT * INTO #temp_table FROM customers;",
        "SELECT 1; EXEC xp_cmdshell 'calc.exe';",
        "SELECT * FROM OPENROWSET('SQLNCLI', 'Server=evil;Trusted_Connection=yes;', 'SELECT 1');",
        "WAITFOR DELAY '0:0:10';",
    ],
)
def test_mssql_blocks_mutation_and_dangerous_queries(monkeypatch, bad_query):
    """驗證嚴格阻擋非唯讀、SELECT INTO、EXEC、OPENROWSET、WAITFOR 與多重語句指令。"""
    _set_full_mssql_env(monkeypatch)
    with patch("mcp_servers.mssql_mcp._get_readonly_connection") as mock_conn:
        out = mssql_mcp.mssql_run_select_query(bad_query)
        assert out.startswith("[ERROR]")
        mock_conn.assert_not_called()


def test_mssql_secret_redaction(monkeypatch):
    """驗證查詢結果自動遮罩敏感欄位名稱與機密字串。"""
    _set_full_mssql_env(monkeypatch)
    conn, _ = _make_fake_conn(
        rows=[(1, "admin", "hash_99887766", "SecretTokenValue", "ConnectionString=Server=db;Password=pwn;")],
        description=[("id",), ("username",), ("passwd_hash",), ("access_token",), ("config_note",)],
    )
    with patch("mcp_servers.mssql_mcp._get_readonly_connection", return_value=conn):
        out = mssql_mcp.mssql_run_select_query(
            "SELECT id, username, passwd_hash, access_token, config_note FROM accounts;"
        )

    assert "admin" in out
    assert "hash_99887766" not in out
    assert "SecretTokenValue" not in out
    assert "Password=pwn" not in out
    assert "[REDACTED]" in out


def test_mssql_streaming_truncation_anti_oom(monkeypatch):
    """驗證批次串流讀取在達到字元上限時友善截斷，防範 OOM。"""
    _set_full_mssql_env(monkeypatch)
    large_rows = [(i, f"Item {i}", "Long description payload " * 20) for i in range(1, 150)]
    conn, _ = _make_fake_conn(
        rows=large_rows,
        description=[("id",), ("title",), ("detail",)],
    )
    with patch("mcp_servers.mssql_mcp._get_readonly_connection", return_value=conn):
        out = mssql_mcp.mssql_run_select_query("SELECT id, title, detail FROM logs;")

    assert "查詢結果已截斷" in out
    assert "已呈現前" in out
    assert len(out) <= mssql_mcp.MAX_OUTPUT_CHARS + 500


@pytest.mark.parametrize("empty_val", ["", "   ", "null", "NULL", "None", "none", "~"])
def test_mssql_empty_or_null_env_prompts_and_caches(monkeypatch, empty_val):
    """驗證未設定或為空值/null 時透過 _tty_prompt 互動詢問（Database 為選填不詢問）並快取於記憶體。"""
    monkeypatch.setenv("MSSQL_HOST", empty_val)
    monkeypatch.setenv("MSSQL_DATABASE", empty_val)
    prompts = iter(["sql.corp.local", "", r"CORP\dbuser", "PromptedSqlPass!1"])

    with patch("mcp_servers.mssql_mcp._tty_prompt", side_effect=lambda *a, **kw: next(prompts)) as mock_prompt:
        resolved = mssql_mcp._resolve_connection_params()
        assert resolved == ("sql.corp.local", "1433", r"CORP\dbuser", "PromptedSqlPass!1", "")
        assert mock_prompt.call_count == 4

        # 第二次呼叫直接命中快取，不再詢問
        resolved2 = mssql_mcp._resolve_connection_params()
        assert resolved2 == resolved
        assert mock_prompt.call_count == 4


def test_mssql_missing_driver_preserves_cached_creds(monkeypatch):
    """驗證當 pymssql 與 pyodbc 均未安裝時，回傳清楚錯誤且不清除已快取的連線憑證。"""
    _set_full_mssql_env(monkeypatch)
    with patch("importlib.import_module", side_effect=ImportError("no mssql driver")):
        out = mssql_mcp.mssql_list_databases()
        assert "[CONN_AUTH]" not in out
        assert "pip install pymssql" in out
        assert mssql_mcp._RUNTIME_CREDS.get("MSSQL_HOST") == "10.20.30.50"


def test_mssql_pyodbc_fallback_and_placeholder_adaptation(monkeypatch):
    """驗證當 pymssql 未安裝時自動降級至 pyodbc，並啟用 ApplicationIntent=ReadOnly 與 ? 佔位符轉換。"""
    _set_full_mssql_env(monkeypatch)

    class FakePyodbcConn:
        __module__ = "pyodbc"

        def __init__(self, cursor):
            self._cursor = cursor
            self.rollback = MagicMock()
            self.close = MagicMock()

        def cursor(self):
            return self._cursor

    _, cursor = _make_fake_conn(rows=[("id", "int", "NO", None, 1)])
    pyodbc_conn = FakePyodbcConn(cursor)
    fake_pyodbc = SimpleNamespace(
        drivers=MagicMock(return_value=["ODBC Driver 18 for SQL Server"]),
        connect=MagicMock(return_value=pyodbc_conn),
    )

    def fake_import(name):
        if name == "pymssql":
            raise ImportError("No module named 'pymssql'")
        if name == "pyodbc":
            return fake_pyodbc
        raise ImportError(name)

    with patch("importlib.import_module", side_effect=fake_import):
        out = mssql_mcp.mssql_describe_schema("orders")

    assert "id (INT [PK] NOT NULL)" in out
    conn_str = fake_pyodbc.connect.call_args.args[0]
    assert "ApplicationIntent=ReadOnly;" in conn_str
    assert "DRIVER={ODBC Driver 18 for SQL Server};" in conn_str
    # 驗證 pyodbc 連線下將 %s 轉為 ?
    schema_sql = cursor.execute.call_args_list[-1].args[0]
    assert "WHERE c.TABLE_SCHEMA = ? AND c.TABLE_NAME = ?" in schema_sql
    assert "%s" not in schema_sql


def test_mcp_manager_strips_mssql_params_and_prompts_only_missing():
    """驗證 MCPManager 自動隱藏 mssql_* 連線參數並支援動態詢問與敏感標記。"""
    manager = MCPManager()
    manager._server_envs["mssql"] = {
        "MSSQL_HOST": "10.20.30.50",
        "MSSQL_PORT": "1433",
        "MSSQL_DATABASE": "erp_db",
    }
    fake_tools = [
        SimpleNamespace(
            name="mssql_list_tables",
            description="list tables",
            inputSchema={
                "type": "object",
                "properties": {
                    "mssql_host": {"type": "string"},
                    "mssql_port": {"type": "string"},
                    "mssql_user": {"type": "string"},
                    "mssql_password": {"type": "string"},
                    "mssql_database": {"type": "string"},
                },
            },
        ),
    ]
    fake_session = SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=fake_tools)),
        call_tool=AsyncMock(return_value=MagicMock(content=[MagicMock(text="customers")])),
    )
    asyncio.run(
        manager._register_tools(
            "mssql",
            fake_session,
            inject={},
            safe_read_tools=["mssql_list_tables"],
            is_sensitive=True,
        )
    )
    schemas = {s["name"]: s for s in manager.openai_tools()}
    assert schemas["mssql_list_tables"].get("_is_safe_read") is True
    assert schemas["mssql_list_tables"]["parameters"]["properties"] == {}
    assert manager.is_sensitive_tool("mssql_list_tables") is True

    prompt_calls = []

    def fake_prompt(server_name, missing_keys):
        prompt_calls.append((server_name, missing_keys))
        return {
            "MSSQL_USER": "sa_readonly",
            "MSSQL_PASSWORD": "MssqlPw!123",
        }

    manager.on_prompt_conn = fake_prompt
    asyncio.run(manager.call("mssql_list_tables", {}))

    assert prompt_calls == [("mssql", ["MSSQL_USER", "MSSQL_PASSWORD"])]
    _, sent_args = fake_session.call_tool.call_args.args
    assert sent_args == {
        "mssql_host": "10.20.30.50",
        "mssql_port": "1433",
        "mssql_user": "sa_readonly",
        "mssql_password": "MssqlPw!123",
        "mssql_database": "erp_db",
    }

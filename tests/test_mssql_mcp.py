"""單元測試：驗證 MSSQL MCP (mssql_mcp.py) 之唯讀保護、串流防爆、連線釋放、機密遮罩、互動詢問與 MCPManager 整合。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_servers import mssql_mcp
from mcp_servers._shared import DEFAULT_REDACTION_POLICY, format_cell_value
from mcp_servers.mssql import adapter as mssql_adapter
from pocket_agent.extensions.mcp import MCPManager


@pytest.fixture(autouse=True)
def _clean_mssql_env_and_cache(monkeypatch, tmp_path):
    """每次測試前清空 MSSQL MCP 執行期記憶體快取、環境變數並隔離測試用 agent.log。"""
    mssql_adapter.MSSQL_ADAPTER.clear_cache()
    monkeypatch.setenv("POCKET_AGENT_LOG_FILE", str(tmp_path / "agent.log"))
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
    mssql_adapter.MSSQL_ADAPTER.clear_cache()


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

    with patch("mcp_servers.mssql.adapter.load_mssql_driver", return_value=("pymssql", fake_driver)):
        out = mssql_mcp.mssql_list_databases()

    assert out == "erp_db\nhr_db"
    conn.rollback.assert_called_once()
    conn.close.assert_called_once()


def test_mssql_list_tables(monkeypatch):
    """驗證資料表清單檢索（含 dbo 簡化顯示與自訂 Schema 顯示）及唯讀交易隔離設定。"""
    _set_full_mssql_env(monkeypatch)
    conn, cursor = _make_fake_conn(rows=[("dbo", "customers"), ("sales", "invoices")])
    fake_driver = SimpleNamespace(connect=MagicMock(return_value=conn))

    with patch("mcp_servers.mssql.adapter.load_mssql_driver", return_value=("pymssql", fake_driver)):
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
    conn, cursor = _make_fake_conn()
    cursor.fetchall.side_effect = [schema_rows, [], []]
    fake_driver = SimpleNamespace(connect=MagicMock(return_value=conn))

    with patch("mcp_servers.mssql.adapter.load_mssql_driver", return_value=("pymssql", fake_driver)):
        out = mssql_mcp.mssql_describe_schema("sales.invoices")

    assert "id (INT [PK] NOT NULL)" in out
    assert "order_no (NVARCHAR NOT NULL)" in out
    assert "remark (NVARCHAR)" in out
    col_calls = [c for c in cursor.execute.call_args_list if "INFORMATION_SCHEMA.COLUMNS" in c.args[0]]
    assert len(col_calls) == 1
    assert col_calls[0].args[1] == ("sales", "invoices")

    # 測試查無資料表
    conn_empty, _ = _make_fake_conn(rows=[])
    fake_driver_empty = SimpleNamespace(connect=MagicMock(return_value=conn_empty))
    with patch("mcp_servers.mssql.adapter.load_mssql_driver", return_value=("pymssql", fake_driver_empty)):
        assert "找不到資料表" in mssql_mcp.mssql_describe_schema("missing_tbl")

    # 測試非法資料表名稱攔截
    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect") as mock_conn:
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
    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn):
        out = mssql_mcp.mssql_run_select_query("SELECT TOP 2 id, name FROM customers;")
    assert "id, name" in out
    assert "1, cust_a" in out
    assert "2, cust_b" in out

    conn_cte, _ = _make_fake_conn(
        rows=[(1, "cust_a")],
        description=[("id",), ("name",)],
    )
    cte_sql = "WITH top_c AS (SELECT id, name FROM customers) SELECT * FROM top_c;"
    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn_cte):
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
    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect") as mock_conn:
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
    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn):
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
    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn):
        out = mssql_mcp.mssql_run_select_query("SELECT id, title, detail FROM logs;")

    assert "查詢結果已截斷" in out
    assert "已呈現前" in out
    assert len(out) <= mssql_mcp.MAX_OUTPUT_CHARS + 500


@pytest.mark.parametrize("empty_val", ["", "   ", "null", "NULL", "None", "none", "~"])
def test_mssql_empty_or_null_env_prompts_and_caches(monkeypatch, empty_val):
    """驗證未設定或為空值/null 時透過終端機互動詢問（Database 為選填不詢問）並快取於記憶體。"""
    monkeypatch.setenv("MSSQL_HOST", empty_val)
    monkeypatch.setenv("MSSQL_DATABASE", empty_val)
    text_prompts = iter(["sql.corp.local", "", r"CORP\dbuser"])

    with (
        patch("mcp_servers.mssql.adapter.tty_prompt_text", side_effect=lambda *a, **kw: next(text_prompts)) as mock_t,
        patch("mcp_servers.mssql.adapter.tty_prompt_secret", return_value="PromptedSqlPass!1") as mock_s,
    ):
        resolved = mssql_adapter.MSSQL_ADAPTER.resolve_connection_params()
        assert resolved == ("sql.corp.local", "1433", r"CORP\dbuser", "PromptedSqlPass!1", "")
        assert mock_t.call_count == 3
        assert mock_s.call_count == 1

        # 第二次呼叫直接命中快取，不再詢問
        resolved2 = mssql_adapter.MSSQL_ADAPTER.resolve_connection_params()
        assert resolved2 == resolved
        assert mock_t.call_count == 3
        assert mock_s.call_count == 1


def test_mssql_missing_driver_preserves_cached_creds(monkeypatch):
    """驗證當 pymssql 與 pyodbc 均未安裝時，回傳清楚錯誤且不清除已快取的連線憑證。"""
    _set_full_mssql_env(monkeypatch)
    with patch("importlib.import_module", side_effect=ImportError("no mssql driver")):
        out = mssql_mcp.mssql_list_databases()
        assert "[CONN_AUTH]" not in out
        assert "pip install pymssql" in out
        assert mssql_adapter.MSSQL_ADAPTER.cache.get("MSSQL_HOST") == "10.20.30.50"


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

    _, cursor = _make_fake_conn()
    cursor.fetchall.side_effect = [[("id", "int", "NO", None, 1)], [], []]
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
    col_calls = [c for c in cursor.execute.call_args_list if "INFORMATION_SCHEMA.COLUMNS" in c.args[0]]
    assert len(col_calls) == 1
    schema_sql = col_calls[0].args[0]
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
    assert schemas["mcp__mssql__mssql_list_tables"].get("_is_safe_read") is True
    assert schemas["mcp__mssql__mssql_list_tables"]["parameters"]["properties"] == {}
    assert manager.is_sensitive_tool("mcp__mssql__mssql_list_tables") is True

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


def test_mssql_audit_log_records_queries_and_blocks_without_leaking_password(monkeypatch, tmp_path):
    """驗證 mssql_mcp.py 將執行的 T-SQL 指令與攔截事件寫入集中化 agent.log，且絕不洩漏密碼。"""
    _set_full_mssql_env(monkeypatch)
    log_file = tmp_path / "agent.log"
    conn, _ = _make_fake_conn(rows=[(1, "cust_a")], description=[("id",), ("name",)])

    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn):
        mssql_mcp.mssql_run_select_query("SELECT TOP 1 id, name FROM customers;")
        mssql_mcp.mssql_run_select_query("EXEC xp_cmdshell 'whoami';")

    content = log_file.read_text(encoding="utf-8")
    assert "[AUDIT] [MCP:mssql] tool=mssql_run_select_query status=OK" in content
    assert "target=CORP_sql_reader@10.20.30.50:1433/erp_db" in content
    assert "SELECT TOP 1 id, name FROM customers" in content
    assert "[AUDIT] [MCP:mssql] tool=mssql_run_select_query status=BLOCKED" in content
    assert "EXEC xp_cmdshell 'whoami';" in content
    assert "SqlSecret#2026" not in content


def test_resolve_connection_params_ipv6_env_host_and_invalid_env_port_tolerance(monkeypatch):
    """驗證 IPv6 環境變數正規化比對可讀取密碼，且無效的環境變數埠號不會干擾顯式參數。"""
    monkeypatch.setenv("MSSQL_HOST", "[fe80::1]")
    monkeypatch.setenv("MSSQL_PORT", "not_a_port")
    monkeypatch.setenv("MSSQL_USER", "sa_reader")
    monkeypatch.setenv("MSSQL_PASSWORD", "Ipv6Secret!99")

    with (
        patch("mcp_servers.mssql.adapter.tty_prompt_text") as mock_text,
        patch("mcp_servers.mssql.adapter.tty_prompt_secret") as mock_secret,
    ):
        host, port, user, pw, _ = mssql_adapter.MSSQL_ADAPTER.resolve_connection_params(
            mssql_host="fe80::1",
            mssql_port="1433",
        )
        assert host == "fe80::1"
        assert port == "1433"
        assert user == "sa_reader"
        assert pw == "Ipv6Secret!99"
        mock_text.assert_not_called()
        mock_secret.assert_not_called()


def test_redact_url_pem_and_format_cell_null_multiline():
    """驗證 URI 帳密、PEM 私鑰遮罩，以及 NULL 與多行文字單行化格式。"""
    uri_text = "conn=mssql://sa:TopSecret123@10.0.0.5:1433/prod"
    redacted_uri = DEFAULT_REDACTION_POLICY.redact(uri_text)
    assert "TopSecret123" not in redacted_uri
    assert "mssql://[REDACTED]@10.0.0.5:1433/prod" in redacted_uri

    pem_text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----"
    assert DEFAULT_REDACTION_POLICY.redact(pem_text) == "[REDACTED_PRIVATE_KEY]"

    assert format_cell_value(None) == "NULL"
    assert format_cell_value("raw_secret", is_sensitive=True) == "[REDACTED]"
    multiline_cell = "header_ok\npassword=MySecret789\nfooter_ok"
    formatted = format_cell_value(multiline_cell)
    assert "\n" not in formatted
    assert "MySecret789" not in formatted
    assert formatted == "header_ok\\npassword=[REDACTED]\\nfooter_ok"


def test_mssql_list_tables_marks_views(monkeypatch):
    """驗證 mssql_list_tables 支援顯示檢視表並標註 (VIEW)。"""
    _set_full_mssql_env(monkeypatch)
    conn, _ = _make_fake_conn(
        rows=[
            ("dbo", "Users", "BASE TABLE"),
            ("dbo", "v_ActiveUsers", "VIEW"),
            ("sales", "v_MonthlySummary", "VIEW"),
        ]
    )
    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn):
        out = mssql_mcp.mssql_list_tables()

    assert "Users" in out
    assert "v_ActiveUsers (VIEW)" in out
    assert "sales.v_MonthlySummary (VIEW)" in out


def test_mssql_describe_schema_includes_length_precision_default_indexes_and_fks(monkeypatch):
    """驗證 mssql_describe_schema 輸出欄位長度/精度、預設值、索引與外鍵。"""
    _set_full_mssql_env(monkeypatch)
    conn, cursor = _make_fake_conn()
    col_rows = [
        ("id", "int", "NO", None, 1, None, 10, 0),
        ("email", "nvarchar", "NO", None, 0, 255, None, None),
        ("notes", "nvarchar", "YES", "('')", 0, -1, None, None),
        ("amount", "decimal", "NO", "((0.00))", 0, None, 18, 2),
    ]
    idx_rows = [
        ("UQ_Orders_Email", 1, "email"),
    ]
    fk_rows = [
        ("user_id", "Users", "id"),
    ]
    cursor.fetchall.side_effect = [col_rows, idx_rows, fk_rows]

    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn):
        out = mssql_mcp.mssql_describe_schema("dbo.Orders")

    assert "id (INT [PK] NOT NULL)" in out
    assert "email (NVARCHAR(255) NOT NULL)" in out
    assert "notes (NVARCHAR(MAX) DEFAULT (''))" in out
    assert "amount (DECIMAL(18,2) NOT NULL DEFAULT ((0.00)))" in out
    assert "[Indexes]" in out
    assert "- UQ_Orders_Email: UNIQUE (email)" in out
    assert "[Foreign Keys]" in out
    assert "- user_id -> Users.id" in out


def test_mssql_inspect_health_detects_heap_missing_pk_disabled_index_and_untrusted_fk(monkeypatch):
    """驗證 mssql_inspect_health 能偵測 Heap 表、缺少主鍵、未用空間碎片、停用索引、未信任約束與 Identity 水位。"""
    _set_full_mssql_env(monkeypatch)
    conn, cursor = _make_fake_conn()
    mb = 1024 * 1024
    table_rows = [
        # (schema, table, has_pk, is_heap, row_count, reserved_bytes, used_bytes)
        ("dbo", "Orders", 1, 0, 12000, 100 * mb, 70 * mb),
        ("sales", "StagingLogs", 0, 1, 500, 2 * mb, 2 * mb),
    ]
    idx_rows = [
        ("dbo", "Orders", "IX_Orders_Cust", 0, 0, "CustomerId"),
        ("dbo", "Orders", "IX_Orders_Cust_Date", 0, 0, "CustomerId"),
        ("dbo", "Orders", "IX_Orders_Cust_Date", 0, 0, "OrderDate"),
        ("dbo", "Orders", "IX_Orders_OldDisabled", 0, 1, "LegacyCode"),
    ]
    cons_rows = [
        ("dbo", "Orders", "FK_Orders_Customers", "FOREIGN KEY", 0, 1),
    ]
    ident_rows = [
        ("dbo", "Orders", "OrderTypeId", "tinyint", 220),
    ]
    cursor.fetchall.side_effect = [table_rows, idx_rows, cons_rows, ident_rows]

    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn):
        out = mssql_mcp.mssql_inspect_health()

    assert "[MSSQL Health Summary]" in out
    assert "[Missing PK] sales.StagingLogs 缺少主鍵" in out
    assert "[Heap Table] sales.StagingLogs 為堆積表" in out
    assert "[Unused Space] Orders 未用保留空間達 30.00 MB (30.0%)" in out
    assert "[Redundant Index] Orders: IX_Orders_Cust (CustomerId) 為 IX_Orders_Cust_Date" in out
    assert "[Disabled Index] Orders.IX_Orders_OldDisabled 處於停用狀態" in out
    assert "[Constraint] Orders 的 FOREIGN KEY 約束 FK_Orders_Customers 處於 未受信任" in out
    assert "[Identity] Orders.OrderTypeId (TINYINT) 水位已達 86.3% (220/255)" in out
    conn.rollback.assert_called_once()
    conn.close.assert_called_once()


def test_mssql_inspect_health_dmv_permission_degradation_and_identifier_guard(monkeypatch):
    """驗證當無 VIEW DATABASE STATE 權限時自動降級至基本目錄查詢，並攔截非法識別字。"""
    _set_full_mssql_env(monkeypatch)

    # 1. 攔截非法識別字
    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect") as mock_conn:
        bad_out = mssql_mcp.mssql_inspect_health(table_name="Orders; DROP TABLE Users")
        assert bad_out.startswith("[ERROR]")
        mock_conn.assert_not_called()

    # 2. 模擬第一次查詢 sys.dm_db_partition_stats 拋出權限錯誤，自動降級第二次查詢
    conn, cursor = _make_fake_conn()
    call_idx = 0

    def fake_execute(sql, params=()):
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1 and "dm_db_partition_stats" in sql:
            raise RuntimeError("VIEW DATABASE STATE permission was denied")

    cursor.execute.side_effect = fake_execute
    cursor.fetchall.side_effect = [
        [("dbo", "Customers", 1, 0, None, 0, 0)],
        [],
        [],
        [],
    ]

    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn):
        out = mssql_mcp.mssql_inspect_health(table_name="dbo.Customers")

    assert "VIEW DATABASE STATE 權限，已自動降級" in out
    assert "- Customers: Structure=Clustered, PK=Yes" in out


# ── 安全加固回歸測試 (CR-001 ~ CR-005) ──────────────────────────────────


@pytest.mark.parametrize(
    "bad_query",
    [
        "SELECT * FROM Orders WITH (UPDLOCK)",
        "SELECT * FROM Orders WITH (TABLOCKX)",
        "SELECT * FROM Orders WITH (HOLDLOCK)",
        "SELECT * FROM sys.dm_os_enumerate_filesystem('C:\\')",
        "SELECT * FROM sys.fn_get_audit_file('C:\\audit\\*', DEFAULT, DEFAULT)",
        "SELECT /* unclosed comment FROM Orders",
        "SELECT [unclosed_bracket FROM Orders",
        'SELECT "unclosed_ident FROM Orders',
    ],
)
def test_mssql_blocks_lock_hints_filesystem_tvfs_and_unclosed_tokens(monkeypatch, bad_query):
    _set_full_mssql_env(monkeypatch)
    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect") as mock_conn:
        out = mssql_mcp.mssql_run_select_query(bad_query)
        assert out.startswith("[ERROR]")
        mock_conn.assert_not_called()


def test_mssql_sql_guard_allows_literals_double_quotes_and_escaped_brackets():
    from mcp_servers.mssql import sql_guard as mssql_sql_guard

    q = (
        "SELECT STRING_AGG([col]]name], ';') AS [out], \"user'col\" "
        "FROM [dbo].[audit_logs] WHERE msg = N'DROP TABLE /* keep */';"
    )
    cleaned = mssql_sql_guard.MSSQL_SQL_GUARD.validate_select(q)
    assert "STRING_AGG([col]]name], ';')" in cleaned
    assert "N'DROP TABLE /* keep */'" in cleaned


def test_mssql_adapt_placeholders_preserves_string_literals():
    class _FakePyodbcConn:
        __module__ = "pyodbc"

    sql = "SELECT * FROM t WHERE a LIKE '%s%%' AND b = %s AND c = %s;"
    adapted = mssql_adapter.adapt_placeholders(sql, _FakePyodbcConn())
    assert adapted == "SELECT * FROM t WHERE a LIKE '%s%%' AND b = ? AND c = ?;"


def test_mssql_pyodbc_timeout_lock_timeout_and_abort_on_truncation(monkeypatch):
    _set_full_mssql_env(monkeypatch)
    monkeypatch.setenv("MSSQL_QUERY_TIMEOUT_SEC", "22")
    monkeypatch.setenv("MSSQL_LOCK_TIMEOUT_MS", "4500")

    class _FakePyodbcConn:
        __module__ = "pyodbc"

        def __init__(self, cur):
            self._cur = cur
            self.timeout = 0
            self.rollback = MagicMock()
            self.close = MagicMock()

        def cursor(self):
            return self._cur

    huge_rows = [(i, "x" * 300) for i in range(200)]
    _, cursor = _make_fake_conn(rows=huge_rows, description=[("id",), ("payload",)])
    cursor.cancel = MagicMock()
    pyodbc_conn = _FakePyodbcConn(cursor)
    fake_pyodbc = SimpleNamespace(
        drivers=MagicMock(return_value=["ODBC Driver 18 for SQL Server"]),
        connect=MagicMock(return_value=pyodbc_conn),
    )

    def _fake_import(name):
        if name == "pymssql":
            raise ImportError("missing")
        if name == "pyodbc":
            return fake_pyodbc
        raise ImportError(name)

    with patch("importlib.import_module", side_effect=_fake_import):
        out = mssql_mcp.mssql_run_select_query("SELECT id, payload FROM big_table")

    assert pyodbc_conn.timeout == 22
    executed = [c.args[0] for c in cursor.execute.call_args_list]
    assert "SET LOCK_TIMEOUT 4500;" in executed
    assert "查詢結果已截斷" in out
    cursor.cancel.assert_called_once()
    pyodbc_conn.rollback.assert_not_called()
    pyodbc_conn.close.assert_called_once()


def test_mssql_describe_schema_include_filter_identity_and_redaction(monkeypatch):
    _set_full_mssql_env(monkeypatch)
    conn, cursor = _make_fake_conn()
    col_rows = [
        ("id", "bigint", "NO", None, 1, None, 19, 0, 1, 0, "流水號"),
        ("api_key", "nvarchar", "NO", "('sk-1234567890abcdef1234567890')", 0, 100, None, None, 0, 0, "金鑰"),
        ("full_name", "nvarchar", "YES", None, 0, 200, None, None, 0, 1, None),
    ]
    idx_rows = [
        ("IX_Orders_Active", 0, "status", 0, "([is_deleted]=(0))"),
        ("IX_Orders_Active", 0, "total", 1, "([is_deleted]=(0))"),
    ]
    fk_rows = [
        ("org_id", "core", "Orgs", "id"),
    ]
    cursor.fetchall.side_effect = [col_rows, idx_rows, fk_rows]

    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn):
        out = mssql_mcp.mssql_describe_schema("dbo.Orders")

    assert "id (BIGINT [PK] NOT NULL) [IDENTITY] -- 流水號" in out
    assert "sk-1234567890abcdef1234567890" not in out
    assert "[REDACTED_TOKEN]" in out
    assert "full_name (NVARCHAR(200)) [COMPUTED]" in out
    assert "- IX_Orders_Active: (status) INCLUDE (total) WHERE ([is_deleted]=(0))" in out
    assert "- org_id -> core.Orgs.id" in out


def test_mssql_inspect_health_skips_filtered_indexes_and_does_not_swallow_non_perm_error(monkeypatch):
    _set_full_mssql_env(monkeypatch)

    # 1. 非權限錯誤不應被誤降級，應直接回報錯誤
    conn_err, cursor_err = _make_fake_conn()
    cursor_err.execute.side_effect = RuntimeError("Database 'missing_db' does not exist")
    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn_err):
        err_out = mssql_mcp.mssql_inspect_health(database="missing_db")
    assert err_out.startswith("[ERROR]")
    assert "missing_db" in err_out

    # 2. Filtered Index (has_filter=1) 不應觸發冗餘索引警告
    conn_ok, cursor_ok = _make_fake_conn()
    table_rows = [("dbo", "Orders", 1, 0, 100, 1024 * 1024, 1024 * 1024)]
    idx_rows = [
        ("dbo", "Orders", "IX_Orders_Active", 0, 0, "CustomerId", 2, 1),
        ("dbo", "Orders", "IX_Orders_Cust_Date", 0, 0, "CustomerId", 2, 0),
        ("dbo", "Orders", "IX_Orders_Cust_Date", 0, 0, "OrderDate", 2, 0),
    ]
    cursor_ok.fetchall.side_effect = [table_rows, idx_rows, [], []]
    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn_ok):
        out_ok = mssql_mcp.mssql_inspect_health()
    assert "[Redundant Index]" not in out_ok


# ── P0 & P1 擴充工具測試 ────────────────────────────────────────────────


def test_mssql_get_object_definition_proc_and_redaction(monkeypatch):
    _set_full_mssql_env(monkeypatch)
    conn, cursor = _make_fake_conn()
    cursor.fetchone.return_value = (
        "P",
        "SQL_STORED_PROCEDURE",
        "CREATE PROCEDURE dbo.usp_Sync AS SELECT password='MySecretPassword123';",
    )
    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn):
        out = mssql_mcp.mssql_get_object_definition("dbo.usp_Sync")

    assert "[SQL_STORED_PROCEDURE: dbo.usp_Sync]" in out
    assert "MySecretPassword123" not in out
    assert "[REDACTED]" in out


def test_mssql_explain_query_showplan_warnings_and_cleanup(monkeypatch):
    _set_full_mssql_env(monkeypatch)
    conn, cursor = _make_fake_conn()
    cursor.fetchall.return_value = [
        ("  |--Key Lookup(OBJECT:([erp_db].[dbo].[Orders]))",),
        ("       |--Table Scan(OBJECT:([erp_db].[dbo].[Staging]))",),
        ("  |--Sort(ORDER BY:([created_at] DESC))",),
    ]
    cursor.nextset.return_value = False

    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn):
        out = mssql_mcp.mssql_explain_query("SELECT * FROM Orders ORDER BY created_at")

    executed = [c.args[0] for c in cursor.execute.call_args_list]
    assert "SET SHOWPLAN_TEXT ON;" in executed
    assert "SET SHOWPLAN_TEXT OFF;" in executed
    assert "[Execution Plan Warnings]" in out
    assert "Table Scan" in out
    assert "Key Lookup" in out
    assert "Sort" in out


def test_mssql_server_status_and_dmv_degradation(monkeypatch):
    _set_full_mssql_env(monkeypatch)
    conn, cursor = _make_fake_conn()
    cursor.fetchone.side_effect = [
        (
            "Microsoft SQL Server 2022",
            "16.0.4115.5",
            "CU12",
            "Enterprise Edition",
            "SQL_Latin1_General_CP1_CI_AS",
            1,
            0,
        ),
        RuntimeError("VIEW SERVER STATE permission was denied"),
        (16384,),
    ]

    def _exec(sql, params=()):
        if "dm_os_sys_info" in sql:
            raise RuntimeError("VIEW SERVER STATE permission was denied")

    cursor.execute.side_effect = _exec
    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn):
        out = mssql_mcp.mssql_server_status()

    assert "16.0.4115.5" in out
    assert "Enterprise Edition" in out
    assert "VIEW SERVER STATE" in out


def test_mssql_inspect_active_requests_and_missing_indexes(monkeypatch):
    _set_full_mssql_env(monkeypatch)
    assert mssql_mcp.mssql_inspect_active_requests(min_elapsed_ms=-1).startswith("[ERROR]")
    assert mssql_mcp.mssql_inspect_missing_indexes(top_n=0).startswith("[ERROR]")

    conn_req, _ = _make_fake_conn(
        rows=[
            (
                77,
                "app_user",
                "erp_db",
                "suspended",
                "SELECT",
                55,
                "LCK_M_S",
                4200,
                8500,
                120,
                "SELECT * FROM Orders WHERE token='sk-1234567890abcdef1234567890' AND x='" + ("z" * 300) + "'",
            )
        ]
    )
    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn_req):
        req_out = mssql_mcp.mssql_inspect_active_requests(blocking_only=True)
    assert "BLOCKED_BY=55" in req_out
    assert "sk-1234567890" not in req_out
    assert "[REDACTED]" in req_out
    assert "z" * 250 not in req_out

    conn_idx, _ = _make_fake_conn(
        rows=[
            (
                "erp_db",
                "[erp_db].[dbo].[Orders]",
                "[CustomerId]",
                "[OrderDate]",
                "[Total]",
                500,
                10,
                40.0,
                85.0,
                1734000.0,
            )
        ]
    )
    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn_idx):
        idx_out = mssql_mcp.mssql_inspect_missing_indexes(top_n=5)
    assert "[MSSQL Missing Index Suggestions]" in idx_out
    assert "score=1734000.0" in idx_out
    assert "include: [Total]" in idx_out


def test_mssql_backup_and_ag_status(monkeypatch):
    _set_full_mssql_env(monkeypatch)
    conn, cursor = _make_fake_conn()
    cursor.fetchall.side_effect = [
        [
            ("erp_db", "2026-11-10 02:00:00", None, "2026-11-27 12:00:00", 17),
            ("new_db", None, None, None, None),
        ],
        [
            ("AG_PROD", "erp_db", 1, "SYNCHRONIZED", "HEALTHY", 0, 0),
        ],
    ]
    cursor.fetchone.return_value = (1,)
    with patch.object(mssql_adapter.MSSQL_ADAPTER, "connect", return_value=conn):
        out = mssql_mcp.mssql_backup_and_ag_status()

    assert "17 天" in out
    assert "new_db" in out
    assert "[Always On Availability Groups]" in out
    assert "AG_PROD / erp_db (PRIMARY): state=SYNCHRONIZED, health=HEALTHY" in out

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_servers import mysql_mcp
from mcp_servers.mysql import adapter as mysql_adapter
from mcp_servers.mysql import sql_guard as mysql_sql_guard
from mcp_servers.mysql.tools import query as mysql_query_tools
from pocket_agent.extensions.mcp import MCPManager


@pytest.fixture(autouse=True)
def _clean_mysql_env_and_cache(monkeypatch, tmp_path):
    mysql_adapter.MYSQL_ADAPTER.clear_cache()
    monkeypatch.setenv("POCKET_AGENT_LOG_FILE", str(tmp_path / "agent.log"))
    for key in (
        "MYSQL_HOST",
        "MYSQL_PORT",
        "MYSQL_USER",
        "MYSQL_PASSWORD",
        "MYSQL_DATABASE",
        "MYSQL_CONNECT_TIMEOUT",
    ):
        monkeypatch.delenv(key, raising=False)


def _set_full_mysql_env(monkeypatch):
    monkeypatch.setenv("MYSQL_HOST", "10.10.20.30")
    monkeypatch.setenv("MYSQL_PORT", "3306")
    monkeypatch.setenv("MYSQL_USER", "readonly_user")
    monkeypatch.setenv("MYSQL_PASSWORD", "MySqlSecret!2026")
    monkeypatch.setenv("MYSQL_DATABASE", "app_db")


def _make_fake_conn(rows=None, description=None):
    cursor = MagicMock()
    cursor.description = description or [("col1",)]
    remaining = list(rows or [])

    def fake_fetchmany(size=100):
        nonlocal remaining
        batch = remaining[:size]
        remaining = remaining[size:]
        return batch

    cursor.fetchmany.side_effect = fake_fetchmany
    cursor.fetchall.return_value = list(rows or [])
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


def test_validate_select_query_supports_hash_comments_and_explain():
    q1 = "# MySQL comment\n-- Standard comment\n/* Block */ SELECT id, name FROM users;"
    assert mysql_sql_guard.MYSQL_SQL_GUARD.validate_select(q1) == "SELECT id, name FROM users"

    q2 = "EXPLAIN SELECT * FROM orders WHERE id = 1"
    assert mysql_sql_guard.MYSQL_SQL_GUARD.validate_select(q2) == q2

    q3 = "EXPLAIN FORMAT=JSON SELECT * FROM orders"
    assert mysql_sql_guard.MYSQL_SQL_GUARD.validate_select(q3) == q3


@pytest.mark.parametrize(
    "bad_query",
    [
        "",
        "DELETE FROM users",
        "EXPLAIN DELETE FROM users",
        "SELECT 1; DROP TABLE users",
        "SELECT * FROM users INTO OUTFILE '/tmp/a.txt'",
        "SELECT * FROM users FOR UPDATE",
        "SELECT SLEEP(5)",
        "SET GLOBAL max_connections = 1000",
    ],
)
def test_validate_select_query_blocks_dangerous_queries(bad_query):
    with pytest.raises(ValueError):
        mysql_sql_guard.MYSQL_SQL_GUARD.validate_select(bad_query)


def test_resolve_connection_params_ipv6_env_host_and_invalid_env_port(monkeypatch):
    monkeypatch.setenv("MYSQL_HOST", "[2001:db8::1]")
    monkeypatch.setenv("MYSQL_PORT", "invalid")
    monkeypatch.setenv("MYSQL_USER", "db_reader")
    monkeypatch.setenv("MYSQL_PASSWORD", "Ipv6MySqlPw!")

    with (
        patch("mcp_servers.mysql.adapter.tty_prompt_text") as mock_text,
        patch("mcp_servers.mysql.adapter.tty_prompt_secret") as mock_secret,
    ):
        host, port, user, pw, _ = mysql_adapter.MYSQL_ADAPTER.resolve_connection_params(
            mysql_host="2001:0db8::1",
            mysql_port="3306",
        )
        assert host == "2001:db8::1"
        assert port == "3306"
        assert user == "db_reader"
        assert pw == "Ipv6MySqlPw!"
        mock_text.assert_not_called()
        mock_secret.assert_not_called()


def test_readonly_connection_enforces_read_only_transaction_and_rollback(monkeypatch):
    _set_full_mysql_env(monkeypatch)
    conn, cursor = _make_fake_conn(rows=[("users", "BASE TABLE")])
    fake_pymysql = SimpleNamespace(connect=MagicMock(return_value=conn))

    with patch("mcp_servers.mysql.adapter.load_mysql_driver", return_value=("pymysql", fake_pymysql)):
        out = mysql_mcp.mysql_list_tables()

    assert out == "users"
    executed = [c.args[0] for c in cursor.execute.call_args_list]
    assert "SET SESSION TRANSACTION READ ONLY;" in executed
    assert "START TRANSACTION READ ONLY;" in executed
    conn.rollback.assert_called_once()
    conn.close.assert_called_once()


def test_mysql_list_tables_includes_views(monkeypatch):
    _set_full_mysql_env(monkeypatch)
    conn, _ = _make_fake_conn(
        rows=[
            ("orders", "BASE TABLE"),
            ("v_order_summary", "VIEW"),
        ]
    )
    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn):
        out = mysql_mcp.mysql_list_tables()

    assert out.splitlines() == ["orders", "v_order_summary (VIEW)"]


def test_mysql_describe_schema_includes_default_indexes_and_fks(monkeypatch):
    _set_full_mysql_env(monkeypatch)
    conn, cursor = _make_fake_conn()
    col_rows = [
        ("id", "bigint(20)", "NO", None, "PRI"),
        ("status", "varchar(32)", "NO", "pending", ""),
    ]
    idx_rows = [
        ("idx_status", 1, "status"),
        ("uk_id", 0, "id"),
    ]
    fk_rows = [
        ("customer_id", "customers", "id"),
    ]
    cursor.fetchall.side_effect = [col_rows, idx_rows, fk_rows]

    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn):
        out = mysql_mcp.mysql_describe_schema("orders")

    assert "id (BIGINT(20) [PK] NOT NULL)" in out
    assert "status (VARCHAR(32) NOT NULL DEFAULT pending)" in out
    assert "[Indexes]" in out
    assert "- idx_status: (status)" in out
    assert "- uk_id: UNIQUE (id)" in out
    assert "[Foreign Keys]" in out
    assert "- customer_id -> customers.id" in out


def test_mysql_run_select_query_redacts_sensitive_cols_urls_and_formats_null(monkeypatch):
    _set_full_mysql_env(monkeypatch)
    conn, _ = _make_fake_conn(
        rows=[
            (
                1,
                "alice",
                "$2b$12$rawHashWithoutKeyword",
                None,
                "mysql://root:PlainPass@10.0.0.1:3306/db\nline2",
            )
        ],
        description=[
            ("id",),
            ("username",),
            ("password_hash",),
            ("deleted_at",),
            ("config_note",),
        ],
    )
    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn):
        out = mysql_mcp.mysql_run_select_query("SELECT * FROM users")

    lines = out.splitlines()
    assert lines[0] == "id, username, password_hash, deleted_at, config_note"
    assert "$2b$12$rawHashWithoutKeyword" not in lines[1]
    assert "PlainPass" not in lines[1]
    assert lines[1] == "1, alice, [REDACTED], NULL, mysql://[REDACTED]@10.0.0.1:3306/db\\nline2"


def test_mcp_manager_strips_mysql_params():
    manager = MCPManager()
    manager._server_envs["mysql"] = {
        "MYSQL_HOST": "10.10.20.30",
        "MYSQL_PORT": "3306",
        "MYSQL_USER": "reader",
        "MYSQL_PASSWORD": "Secret",
    }
    fake_tools = [
        SimpleNamespace(
            name="mysql_list_databases",
            description="list dbs",
            inputSchema={
                "type": "object",
                "properties": {
                    "mysql_host": {"type": "string"},
                    "mysql_port": {"type": "string"},
                    "mysql_user": {"type": "string"},
                    "mysql_password": {"type": "string"},
                    "mysql_database": {"type": "string"},
                },
            },
        ),
    ]
    fake_session = SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=fake_tools)),
        call_tool=AsyncMock(return_value=MagicMock(content=[MagicMock(text="app_db")])),
    )
    asyncio.run(
        manager._register_tools(
            "mysql",
            fake_session,
            inject={},
            safe_read_tools=["mysql_list_databases"],
            is_sensitive=True,
        )
    )
    schemas = {s["name"]: s for s in manager.openai_tools()}
    assert schemas["mcp__mysql__mysql_list_databases"]["parameters"]["properties"] == {}


@pytest.mark.parametrize(
    "comment_bypass_query",
    [
        "SELECT * FROM users INTO/**/OUTFILE '/tmp/pwn.txt'",
        "SELECT * FROM users INTO/* comment */DUMPFILE '/tmp/pwn.bin'",
        "SELECT * FROM users INTO # hash comment\nOUTFILE '/tmp/pwn.txt'",
        "SELECT * FROM users INTO -- dash comment\nOUTFILE '/tmp/pwn.txt'",
        "SELECT /*!50000 1, 2 */ FROM users",
        "SELECT /*M!100000 1 */ FROM users",
        "SELECT * FROM users WHERE name = 'unclosed",
        "SELECT /* unclosed comment FROM users",
        r"SELECT * FROM users WHERE name = 'bad\'escape'",
        "SELECT 1; /* comment */ SELECT 2",
    ],
)
def test_mysql_blocks_comment_split_outfile_and_executable_comments_before_connect(monkeypatch, comment_bypass_query):
    _set_full_mysql_env(monkeypatch)
    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect") as mock_conn:
        out = mysql_mcp.mysql_run_select_query(comment_bypass_query)
        assert out.startswith("[ERROR]")
        mock_conn.assert_not_called()


def test_validate_select_query_preserves_comment_markers_inside_string_literals():
    q = "SELECT '/* keep */ -- also keep # hash', `col_name` /* strip me */ FROM users WHERE a = '''';"
    cleaned = mysql_sql_guard.MYSQL_SQL_GUARD.validate_select(q)
    assert "'/* keep */ -- also keep # hash'" in cleaned
    assert "a = ''''" in cleaned
    assert "strip me" not in cleaned


def test_mysql_run_select_query_uses_streaming_cursor_and_aborts_on_truncation(monkeypatch):
    import pymysql.cursors

    _set_full_mysql_env(monkeypatch)
    huge_rows = [(i, "x" * 200) for i in range(500)]
    conn, cursor = _make_fake_conn(rows=huge_rows, description=[("id",), ("payload",)])

    monkeypatch.setattr(mysql_query_tools, "MAX_OUTPUT_CHARS", 600)
    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn):
        out = mysql_mcp.mysql_run_select_query("SELECT id, payload FROM big_table")

    assert "查詢結果已截斷" in out
    conn.cursor.assert_called_once_with(pymysql.cursors.SSCursor)
    # 僅讀取第一批，不繼續 fetch，亦不呼叫會排空的 cursor.close() 或 rollback()
    assert cursor.fetchmany.call_count == 1
    cursor.close.assert_not_called()
    conn.rollback.assert_not_called()
    conn.close.assert_called_once()
    assert cursor._result is None
    assert cursor.connection is None


def test_mysql_run_select_query_mysql_connector_uses_unbuffered_cursor_and_normal_close(monkeypatch):
    _set_full_mysql_env(monkeypatch)
    conn, cursor = _make_fake_conn(rows=[(1, "ok")], description=[("id",), ("status",)])
    fake_driver = SimpleNamespace()

    with (
        patch("mcp_servers.mysql.adapter.load_mysql_driver", return_value=("mysql_connector", fake_driver)),
        patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn),
    ):
        out = mysql_mcp.mysql_run_select_query("SELECT id, status FROM items")

    assert "1, ok" in out
    conn.cursor.assert_called_once_with(buffered=False)
    cursor.close.assert_called_once()
    conn.rollback.assert_called_once()
    conn.close.assert_called_once()


# ── Fail Closed: _open_streaming_cursor 測試 ──────────────────────────────


def test_open_streaming_cursor_pymysql_returns_sscursor(monkeypatch):
    """PyMySQL 環境下應回傳 SSCursor 實例。"""
    import pymysql.cursors

    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor

    fake_pymysql = SimpleNamespace(
        cursors=SimpleNamespace(SSCursor=pymysql.cursors.SSCursor),
        connect=MagicMock(return_value=conn),
    )

    with patch("mcp_servers.mysql.adapter.load_mysql_driver", return_value=("pymysql", fake_pymysql)):
        result = mysql_adapter.MYSQL_ADAPTER.open_streaming_cursor(conn)

    conn.cursor.assert_called_once_with(pymysql.cursors.SSCursor)
    assert result is cursor


def test_open_streaming_cursor_mysql_connector_returns_unbuffered(monkeypatch):
    """mysql-connector-python 環境下應回傳 buffered=False 的游標。"""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor

    fake_driver = SimpleNamespace()
    with patch("mcp_servers.mysql.adapter.load_mysql_driver", return_value=("mysql_connector", fake_driver)):
        result = mysql_adapter.MYSQL_ADAPTER.open_streaming_cursor(conn)

    conn.cursor.assert_called_once_with(buffered=False)
    assert result is cursor


def test_open_streaming_cursor_pymysql_missing_sscursor_raises(monkeypatch):
    """PyMySQL 缺少 SSCursor 時應拋出 RuntimeError，不降級。"""
    conn = MagicMock()

    # PyMySQL 模組但無 cursors.SSCursor
    fake_pymysql = SimpleNamespace(
        cursors=SimpleNamespace(),
        connect=MagicMock(return_value=conn),
    )

    with patch("mcp_servers.mysql.adapter.load_mysql_driver", return_value=("pymysql", fake_pymysql)):
        with pytest.raises(RuntimeError, match="缺少 SSCursor"):
            mysql_adapter.MYSQL_ADAPTER.open_streaming_cursor(conn)

    # 確保未呼叫 conn.cursor() 降級
    conn.cursor.assert_not_called()


def test_open_streaming_cursor_unsupported_driver_raises(monkeypatch):
    """不支援的驅動類型應拋出 RuntimeError。"""
    conn = MagicMock()
    fake_driver = SimpleNamespace()

    with patch("mcp_servers.mysql.adapter.load_mysql_driver", return_value=("unknown_driver", fake_driver)):
        with pytest.raises(RuntimeError, match="不支援的 MySQL 驅動類型"):
            mysql_adapter.MYSQL_ADAPTER.open_streaming_cursor(conn)


def test_mysql_run_select_query_fail_closed_on_missing_sscursor(monkeypatch):
    """整合測試：缺少 SSCursor 時查詢工具回傳錯誤而非靜默執行。"""
    _set_full_mysql_env(monkeypatch)

    # 模擬 PyMySQL 環境但無 SSCursor
    fake_pymysql = SimpleNamespace(
        cursors=SimpleNamespace(),
        connect=MagicMock(),
    )

    with patch("mcp_servers.mysql.adapter.load_mysql_driver", return_value=("pymysql", fake_pymysql)):
        out = mysql_mcp.mysql_run_select_query("SELECT 1")

    assert out.startswith("[ERROR]")
    assert "缺少 SSCursor" in out


def test_mysql_inspect_health_detects_missing_pk_fragmentation_autoinc_and_redundant_index(monkeypatch):
    """驗證 mysql_inspect_health 能偵測缺少主鍵、高碎片率、非 InnoDB、AUTO_INCREMENT 溢位與冗餘前綴索引。"""
    _set_full_mysql_env(monkeypatch)
    conn, cursor = _make_fake_conn()
    mb = 1024 * 1024
    table_rows = [
        # (SCHEMA, NAME, TYPE, ENGINE, ROW_FORMAT, ROWS, DATA_LEN, IDX_LEN, DATA_FREE, AUTOINC, COLLATION, CHECK_TIME)
        (
            "app_db",
            "orders",
            "BASE TABLE",
            "InnoDB",
            "Dynamic",
            50000,
            40 * mb,
            10 * mb,
            20 * mb,
            110,
            "utf8mb4_0900",
            None,
        ),
        (
            "app_db",
            "legacy_logs",
            "BASE TABLE",
            "MyISAM",
            "Fixed",
            1000,
            2 * mb,
            1 * mb,
            0,
            None,
            "utf8mb4_0900",
            None,
        ),
        ("app_db", "v_orders", "VIEW", None, None, None, None, None, None, None, "utf8mb4_0900", None),
    ]
    col_rows = [
        # orders 有 PRI 與 tinyint (上限 127，110/127 = 86.6% > 75%)，legacy_logs 無 PRI
        ("app_db", "orders", "id", "tinyint(4)", "PRI", "auto_increment"),
        ("app_db", "legacy_logs", "msg", "varchar(255)", "", ""),
    ]
    idx_rows = [
        # idx_user (user_id) 為 idx_user_created (user_id, created_at) 的冗餘前導索引
        ("app_db", "orders", "idx_user", 1, "user_id"),
        ("app_db", "orders", "idx_user_created", 1, "user_id"),
        ("app_db", "orders", "idx_user_created", 1, "created_at"),
    ]
    cursor.fetchall.side_effect = [table_rows, col_rows, idx_rows]

    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn):
        out = mysql_mcp.mysql_inspect_health()

    assert "[MySQL Health Summary]" in out
    assert "2 張實體表, 1 個檢視表" in out
    assert "[Engine] legacy_logs 使用 MYISAM 引擎" in out
    assert "[Missing PK] legacy_logs 缺少主鍵" in out
    assert "[Fragmentation] orders 碎片空間達 20.00 MB" in out
    assert "[AutoIncrement] orders.id (tinyint(4)) 水位已達 86.6% (110/127)" in out
    assert (
        "[Redundant Index] orders: idx_user (user_id) 為 idx_user_created (user_id, created_at) 的冗餘前導索引" in out
    )
    conn.rollback.assert_called_once()
    conn.close.assert_called_once()


def test_mysql_check_table_quick_and_size_or_mode_guards(monkeypatch):
    """驗證 mysql_check_table 支援 QUICK/FAST，並攔截非法模式、VIEW 與超過 2GB 的大型資料表。"""
    _set_full_mysql_env(monkeypatch)

    # 1. 攔截危險或非白名單 check_mode（連線前即阻擋）
    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect") as mock_conn:
        for bad_mode in ("EXTENDED", "MEDIUM", "FOR UPGRADE", "QUICK; DROP TABLE users"):
            bad_out = mysql_mcp.mysql_check_table("orders", check_mode=bad_mode)
            assert bad_out.startswith("[ERROR]")
            mock_conn.assert_not_called()

    # 2. 攔截 VIEW（不執行 CHECK TABLE）
    conn_view, cursor_view = _make_fake_conn()
    cursor_view.fetchone.return_value = ("VIEW", 0)
    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn_view):
        out_view = mysql_mcp.mysql_check_table("v_orders")
    assert out_view.startswith("[SKIPPED]")
    assert cursor_view.execute.call_count == 1

    # 3. 攔截超過 2GB 的巨型資料表
    conn_huge, cursor_huge = _make_fake_conn()
    cursor_huge.fetchone.return_value = ("BASE TABLE", 3 * 1024 * 1024 * 1024)
    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn_huge):
        out_huge = mysql_mcp.mysql_check_table("huge_events")
    assert out_huge.startswith("[BLOCKED]")
    assert "3.00 GB" in out_huge
    assert cursor_huge.execute.call_count == 1

    # 4. 正常執行 QUICK 模式
    conn_ok, cursor_ok = _make_fake_conn()
    cursor_ok.fetchone.return_value = ("BASE TABLE", 5 * 1024 * 1024)
    cursor_ok.fetchall.return_value = [("app_db.orders", "check", "status", "OK")]
    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn_ok):
        out_ok = mysql_mcp.mysql_check_table("orders", check_mode="fast")
    assert "[MySQL CHECK TABLE Result (FAST)]" in out_ok
    assert "- [status] app_db.orders (check): OK" in out_ok
    executed_sqls = [c.args[0] for c in cursor_ok.execute.call_args_list]
    assert "CHECK TABLE `app_db`.`orders` FAST;" in executed_sqls


# ── 安全加固回歸測試 (CR-001 ~ CR-003) ──────────────────────────────────


@pytest.mark.parametrize(
    "bad_query",
    [
        "SELECT LOAD_FILE('/etc/passwd')",
        "SELECT * FROM orders FOR SHARE",
        "SELECT WAIT_FOR_EXECUTED_GTID_SET('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa:1')",
        "SELECT SOURCE_POS_WAIT('binlog.000001', 123)",
        "SELECT MASTER_POS_WAIT('binlog.000001', 123)",
        "SELECT IS_FREE_LOCK('my_lock')",
    ],
)
def test_mysql_blocks_load_file_for_share_and_wait_functions(monkeypatch, bad_query):
    _set_full_mysql_env(monkeypatch)
    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect") as mock_conn:
        out = mysql_mcp.mysql_run_select_query(bad_query)
        assert out.startswith("[ERROR]")
        mock_conn.assert_not_called()


def test_mysql_sql_guard_allows_semicolon_and_keywords_inside_string_literals():
    q = "SELECT GROUP_CONCAT(name SEPARATOR ';') AS names FROM audit_logs WHERE action = 'UPDATE' AND msg = 'DROP';"
    cleaned = mysql_sql_guard.MYSQL_SQL_GUARD.validate_select(q)
    assert "SEPARATOR ';'" in cleaned
    assert "action = 'UPDATE'" in cleaned


def test_mysql_connect_sets_max_execution_time_and_ssl_options(monkeypatch, tmp_path):
    import ssl

    _set_full_mysql_env(monkeypatch)
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("dummy-ca", encoding="utf-8")
    monkeypatch.setenv("MYSQL_QUERY_TIMEOUT_MS", "5000")
    monkeypatch.setenv("MYSQL_SSL_MODE", "verify_identity")
    monkeypatch.setenv("MYSQL_SSL_CA", str(ca_file))

    conn, cursor = _make_fake_conn()
    fake_pymysql = SimpleNamespace(connect=MagicMock(return_value=conn))
    with (
        patch("mcp_servers.mysql.adapter.load_mysql_driver", return_value=("pymysql", fake_pymysql)),
        patch("ssl.create_default_context", return_value=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)),
    ):
        mysql_adapter.MYSQL_ADAPTER.connect()

    executed = [c.args[0] for c in cursor.execute.call_args_list]
    assert "SET SESSION MAX_EXECUTION_TIME = 5000;" in executed
    call_kwargs = fake_pymysql.connect.call_args.kwargs
    assert isinstance(call_kwargs["ssl"], ssl.SSLContext)
    assert call_kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
    assert call_kwargs["ssl"].check_hostname is True


def test_mysql_describe_schema_separates_cross_schema_tables_and_redacts_defaults(monkeypatch):
    _set_full_mysql_env(monkeypatch)
    monkeypatch.delenv("MYSQL_DATABASE", raising=False)
    conn, cursor = _make_fake_conn()
    col_rows = [
        ("db_a", "id", "bigint", "NO", None, "PRI", "auto_increment", "主鍵"),
        ("db_a", "api_token", "varchar(64)", "NO", "sk-1234567890abcdef1234567890", "", "", "金鑰"),
        ("db_b", "id", "int", "NO", None, "PRI", "", ""),
    ]
    idx_rows = [
        ("db_a", "PRIMARY", 0, "id", "BTREE"),
        ("db_b", "PRIMARY", 0, "id", "BTREE"),
    ]
    fk_rows = [
        ("db_a", "org_id", "shared_db", "orgs", "id"),
    ]
    cursor.fetchall.side_effect = [col_rows, idx_rows, fk_rows]

    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn):
        out = mysql_mcp.mysql_describe_schema("users")

    assert "=== db_a.users ===" in out
    assert "=== db_b.users ===" in out
    assert "sk-1234567890abcdef1234567890" not in out
    assert "[REDACTED_TOKEN]" in out
    assert "[auto_increment]" in out
    assert "-- 主鍵" in out
    assert "org_id -> shared_db.orgs.id" in out


def test_mysql_inspect_health_filters_btree_indexes_and_scopes_to_tables(monkeypatch):
    _set_full_mysql_env(monkeypatch)
    conn, cursor = _make_fake_conn()
    mb = 1024 * 1024
    table_rows = [
        ("app_db", "articles", "BASE TABLE", "InnoDB", "Dynamic", 100, 1 * mb, 1 * mb, 0, None, "utf8mb4", None),
    ]
    col_rows = [("app_db", "articles", "id", "bigint", "PRI", "auto_increment")]
    idx_rows = [("app_db", "articles", "PRIMARY", 0, "id")]
    cursor.fetchall.side_effect = [table_rows, col_rows, idx_rows]

    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn):
        out = mysql_mcp.mysql_inspect_health()

    assert "[MySQL Health Summary]" in out
    executed_sqls = [c.args[0] for c in cursor.execute.call_args_list]
    assert any("INDEX_TYPE = 'BTREE'" in sql for sql in executed_sqls)
    assert any("(TABLE_SCHEMA, TABLE_NAME) IN ((%s, %s))" in sql for sql in executed_sqls)


# ── P0 & P1 擴充工具測試 ────────────────────────────────────────────────


def test_mysql_show_create_table_success_redaction_and_ambiguous(monkeypatch):
    _set_full_mysql_env(monkeypatch)
    monkeypatch.delenv("MYSQL_DATABASE", raising=False)

    conn_ambig, cursor_ambig = _make_fake_conn()
    cursor_ambig.fetchall.return_value = [("db_a",), ("db_b",)]
    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn_ambig):
        ambig_out = mysql_mcp.mysql_show_create_table("users")
    assert ambig_out.startswith("[ERROR]")
    assert "db_a, db_b" in ambig_out

    conn_ok, cursor_ok = _make_fake_conn()
    cursor_ok.fetchone.return_value = (
        "orders",
        "CREATE TABLE `orders` (`id` BIGINT PRIMARY KEY) COMMENT='token=sk-1234567890abcdef1234567890'",
    )
    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn_ok):
        ddl_out = mysql_mcp.mysql_show_create_table("app_db.orders")
    assert "CREATE TABLE `orders`" in ddl_out
    assert "sk-1234567890" not in ddl_out


def test_mysql_explain_query_formats_warnings_and_guards(monkeypatch):
    _set_full_mysql_env(monkeypatch)

    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect") as mock_conn:
        assert mysql_mcp.mysql_explain_query("SELECT 1", explain_format="ANALYZE").startswith("[ERROR]")
        assert mysql_mcp.mysql_explain_query("EXPLAIN SELECT 1").startswith("[ERROR]")
        mock_conn.assert_not_called()

    conn, cursor = _make_fake_conn(
        rows=[
            (
                1,
                "SIMPLE",
                "orders",
                None,
                "ALL",
                None,
                None,
                None,
                None,
                50000,
                100.0,
                "Using filesort; Using temporary",
            )
        ],
        description=[
            ("id",),
            ("select_type",),
            ("table",),
            ("partitions",),
            ("type",),
            ("possible_keys",),
            ("key",),
            ("key_len",),
            ("ref",),
            ("rows",),
            ("filtered",),
            ("Extra",),
        ],
    )
    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn):
        out = mysql_mcp.mysql_explain_query("SELECT * FROM orders ORDER BY created_at")

    assert "[Execution Plan Warnings]" in out
    assert "type=ALL" in out
    assert "Using filesort" in out
    assert "Using temporary" in out
    assert "key=NULL" in out


def test_mysql_server_status_summary(monkeypatch):
    _set_full_mysql_env(monkeypatch)
    conn, cursor = _make_fake_conn()
    cursor.fetchone.return_value = ("8.0.36",)
    cursor.fetchall.side_effect = [
        [
            ("Uptime", "90061"),
            ("Threads_connected", "25"),
            ("Threads_running", "3"),
            ("Slow_queries", "2"),
            ("Questions", "10000"),
            ("Innodb_buffer_pool_reads", "50"),
            ("Innodb_buffer_pool_read_requests", "10000"),
        ],
        [
            ("max_connections", "100"),
            ("read_only", "ON"),
            ("super_read_only", "OFF"),
            ("innodb_buffer_pool_size", str(1024 * 1024 * 1024)),
            ("sql_mode", "STRICT_TRANS_TABLES"),
            ("character_set_server", "utf8mb4"),
            ("transaction_isolation", "REPEATABLE-READ"),
        ],
    ]
    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn):
        out = mysql_mcp.mysql_server_status()

    assert "Version: 8.0.36" in out
    assert "Connections: 25 / 100 (25.0%)" in out
    assert "99.50%" in out


def test_mysql_inspect_processlist_clips_and_redacts_info(monkeypatch):
    _set_full_mysql_env(monkeypatch)
    assert mysql_mcp.mysql_inspect_processlist(min_time_sec=-1).startswith("[ERROR]")

    conn, _ = _make_fake_conn(
        rows=[
            (
                42,
                "app",
                "10.0.0.5:1234",
                "app_db",
                "Query",
                18,
                "Sending data",
                "SELECT * FROM users WHERE token='sk-1234567890abcdef1234567890' AND note='" + ("z" * 300) + "'",
            )
        ]
    )
    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn):
        out = mysql_mcp.mysql_inspect_processlist(only_active=True, min_time_sec=5)

    assert "[MySQL Processlist]" in out
    assert "sk-1234567890" not in out
    assert "[REDACTED]" in out
    assert "z" * 250 not in out


def test_mysql_replication_status_allowlist_fallback_and_permission_denied(monkeypatch):
    _set_full_mysql_env(monkeypatch)

    # 1. 白名單應嚴格排除 Source_User / Master_User 等敏感欄位，並支援 SHOW SLAVE STATUS 降級
    conn_ok, cursor_ok = _make_fake_conn(
        rows=[("", "repl_secret_user", "10.0.0.2", "Yes", "Yes", 0, "")],
        description=[
            ("Channel_Name",),
            ("Source_User",),
            ("Source_Host",),
            ("Replica_IO_Running",),
            ("Replica_SQL_Running",),
            ("Seconds_Behind_Source",),
            ("Last_SQL_Error",),
        ],
    )
    cursor_ok.execute.side_effect = [RuntimeError("Unknown command"), None]
    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn_ok):
        out_ok = mysql_mcp.mysql_replication_status()
    assert "Replica_IO_Running: Yes" in out_ok
    assert "Seconds_Behind_Source: 0" in out_ok
    assert "repl_secret_user" not in out_ok
    assert "Source_User" not in out_ok

    # 2. 權限不足時回傳友善錯誤且不清除憑證快取
    conn_perm, cursor_perm = _make_fake_conn()
    cursor_perm.execute.side_effect = RuntimeError("1227 Access denied; you need REPLICATION CLIENT privilege")
    with patch.object(mysql_adapter.MYSQL_ADAPTER, "connect", return_value=conn_perm):
        out_perm = mysql_mcp.mysql_replication_status()
    assert out_perm.startswith("[ERROR]")
    assert "REPLICATION CLIENT" in out_perm

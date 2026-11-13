import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_servers import mysql_mcp
from pocket_agent.extensions.mcp import MCPManager


@pytest.fixture(autouse=True)
def _clean_mysql_env_and_cache(monkeypatch, tmp_path):
    mysql_mcp._clear_creds_cache()
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
    assert mysql_mcp._validate_select_query(q1) == "SELECT id, name FROM users"

    q2 = "EXPLAIN SELECT * FROM orders WHERE id = 1"
    assert mysql_mcp._validate_select_query(q2) == q2

    q3 = "EXPLAIN FORMAT=JSON SELECT * FROM orders"
    assert mysql_mcp._validate_select_query(q3) == q3


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
        mysql_mcp._validate_select_query(bad_query)


def test_resolve_connection_params_ipv6_env_host_and_invalid_env_port(monkeypatch):
    monkeypatch.setenv("MYSQL_HOST", "[2001:db8::1]")
    monkeypatch.setenv("MYSQL_PORT", "invalid")
    monkeypatch.setenv("MYSQL_USER", "db_reader")
    monkeypatch.setenv("MYSQL_PASSWORD", "Ipv6MySqlPw!")

    with patch("mcp_servers.mysql_mcp._tty_prompt") as mock_prompt:
        host, port, user, pw, _ = mysql_mcp._resolve_connection_params(
            mysql_host="2001:0db8::1",
            mysql_port="3306",
        )
        assert host == "2001:db8::1"
        assert port == "3306"
        assert user == "db_reader"
        assert pw == "Ipv6MySqlPw!"
        mock_prompt.assert_not_called()


def test_readonly_connection_enforces_read_only_transaction_and_rollback(monkeypatch):
    _set_full_mysql_env(monkeypatch)
    conn, cursor = _make_fake_conn(rows=[("users", "BASE TABLE")])
    fake_pymysql = SimpleNamespace(connect=MagicMock(return_value=conn))

    with patch("mcp_servers.mysql_mcp._load_mysql_driver", return_value=("pymysql", fake_pymysql)):
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
    with patch("mcp_servers.mysql_mcp._get_readonly_connection", return_value=conn):
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

    with patch("mcp_servers.mysql_mcp._get_readonly_connection", return_value=conn):
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
    with patch("mcp_servers.mysql_mcp._get_readonly_connection", return_value=conn):
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
    with patch("mcp_servers.mysql_mcp._get_readonly_connection") as mock_conn:
        out = mysql_mcp.mysql_run_select_query(comment_bypass_query)
        assert out.startswith("[ERROR]")
        mock_conn.assert_not_called()


def test_validate_select_query_preserves_comment_markers_inside_string_literals():
    q = "SELECT '/* keep */ -- also keep # hash', `col_name` /* strip me */ FROM users WHERE a = '''';"
    cleaned = mysql_mcp._validate_select_query(q)
    assert "'/* keep */ -- also keep # hash'" in cleaned
    assert "a = ''''" in cleaned
    assert "strip me" not in cleaned


def test_mysql_run_select_query_uses_streaming_cursor_and_aborts_on_truncation(monkeypatch):
    import pymysql.cursors

    _set_full_mysql_env(monkeypatch)
    huge_rows = [(i, "x" * 200) for i in range(500)]
    conn, cursor = _make_fake_conn(rows=huge_rows, description=[("id",), ("payload",)])

    monkeypatch.setattr(mysql_mcp, "MAX_OUTPUT_CHARS", 600)
    with patch("mcp_servers.mysql_mcp._get_readonly_connection", return_value=conn):
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
        patch("mcp_servers.mysql_mcp._load_mysql_driver", return_value=("mysql_connector", fake_driver)),
        patch("mcp_servers.mysql_mcp._get_readonly_connection", return_value=conn),
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

    with patch("mcp_servers.mysql_mcp._load_mysql_driver", return_value=("pymysql", fake_pymysql)):
        result = mysql_mcp._open_streaming_cursor(conn)

    conn.cursor.assert_called_once_with(pymysql.cursors.SSCursor)
    assert result is cursor


def test_open_streaming_cursor_mysql_connector_returns_unbuffered(monkeypatch):
    """mysql-connector-python 環境下應回傳 buffered=False 的游標。"""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor

    fake_driver = SimpleNamespace()
    with patch("mcp_servers.mysql_mcp._load_mysql_driver", return_value=("mysql_connector", fake_driver)):
        result = mysql_mcp._open_streaming_cursor(conn)

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

    with patch("mcp_servers.mysql_mcp._load_mysql_driver", return_value=("pymysql", fake_pymysql)):
        with pytest.raises(RuntimeError, match="缺少 SSCursor"):
            mysql_mcp._open_streaming_cursor(conn)

    # 確保未呼叫 conn.cursor() 降級
    conn.cursor.assert_not_called()


def test_open_streaming_cursor_unsupported_driver_raises(monkeypatch):
    """不支援的驅動類型應拋出 RuntimeError。"""
    conn = MagicMock()
    fake_driver = SimpleNamespace()

    with patch("mcp_servers.mysql_mcp._load_mysql_driver", return_value=("unknown_driver", fake_driver)):
        with pytest.raises(RuntimeError, match="不支援的 MySQL 驅動類型"):
            mysql_mcp._open_streaming_cursor(conn)


def test_mysql_run_select_query_fail_closed_on_missing_sscursor(monkeypatch):
    """整合測試：缺少 SSCursor 時查詢工具回傳錯誤而非靜默執行。"""
    _set_full_mysql_env(monkeypatch)

    # 模擬 PyMySQL 環境但無 SSCursor
    fake_pymysql = SimpleNamespace(
        cursors=SimpleNamespace(),
        connect=MagicMock(),
    )

    with patch("mcp_servers.mysql_mcp._load_mysql_driver", return_value=("pymysql", fake_pymysql)):
        out = mysql_mcp.mysql_run_select_query("SELECT 1")

    assert out.startswith("[ERROR]")
    assert "缺少 SSCursor" in out

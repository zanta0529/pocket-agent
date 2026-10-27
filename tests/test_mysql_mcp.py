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
    assert schemas["mysql_list_databases"]["parameters"]["properties"] == {}

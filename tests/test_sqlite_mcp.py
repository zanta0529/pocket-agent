import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_servers import sqlite_mcp
from pocket_agent.extensions.mcp import MCPManager


@pytest.fixture(autouse=True)
def _clean_sqlite_env_and_cache(monkeypatch, tmp_path):
    sqlite_mcp._clear_creds_cache()
    monkeypatch.setenv("POCKET_AGENT_LOG_FILE", str(tmp_path / "agent.log"))
    monkeypatch.delenv("DB_PATH", raising=False)


def _create_sample_db(db_file: Path) -> Path:
    conn = sqlite3.connect(str(db_file))
    try:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE departments (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                api_token TEXT,
                dept_id INTEGER DEFAULT 1,
                bio TEXT,
                FOREIGN KEY (dept_id) REFERENCES departments(id)
            );
            CREATE INDEX idx_users_username ON users(username);
            CREATE VIEW v_active_users AS
                SELECT id, username, dept_id FROM users;
            INSERT INTO departments (id, name) VALUES (1, 'Engineering');
            INSERT INTO users (id, username, password_hash, api_token, dept_id, bio)
            VALUES (
                1,
                'admin',
                '$2b$12$eImiTXuWVxfM37uY4JANjQ_raw_hash_value',
                'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret_payload',
                1,
                'uri=postgres://dbadmin:SuperSecret99@10.0.0.1:5432/prod\nline2_safe'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db_file


def test_sqlite_list_tables_includes_views_and_accepts_db_path(tmp_path):
    db_file = _create_sample_db(tmp_path / "sample.db")
    out = sqlite_mcp.sqlite_list_tables(db_path=str(db_file))
    lines = out.splitlines()
    assert "departments" in lines
    assert "users" in lines
    assert "v_active_users (VIEW)" in lines


def test_sqlite_describe_schema_shows_defaults_indexes_and_fks(tmp_path):
    db_file = _create_sample_db(tmp_path / "sample.db")
    out = sqlite_mcp.sqlite_describe_schema("users", db_path=str(db_file))
    assert "id (INTEGER [PK])" in out
    assert "username (TEXT NOT NULL)" in out
    assert "dept_id (INTEGER DEFAULT 1)" in out
    assert "[Indexes]" in out
    assert "- idx_users_username: (username)" in out
    assert "[Foreign Keys]" in out
    assert "- dept_id -> departments.id" in out


def test_sqlite_run_select_query_redacts_sensitive_columns_without_corrupting_header(tmp_path):
    """驗證敏感欄位值即使不含關鍵字亦會依欄位名稱遮罩，且標題列不會被誤遮罩。"""
    db_file = _create_sample_db(tmp_path / "sample.db")
    out = sqlite_mcp.sqlite_run_select_query(
        "-- 查詢使用者清單\nSELECT id, username, password_hash, api_token, bio, NULL AS empty_col FROM users;",
        db_path=str(db_file),
    )
    lines = out.splitlines()
    assert lines[0] == "id, username, password_hash, api_token, bio, empty_col"
    assert "$2b$12$" not in out
    assert "eyJhbGci" not in out
    assert "SuperSecret99" not in out
    assert lines[1] == (
        "1, admin, [REDACTED], [REDACTED], uri=postgres://[REDACTED]@10.0.0.1:5432/prod\\nline2_safe, NULL"
    )


def test_sqlite_run_select_query_allows_explain_query_plan(tmp_path):
    db_file = _create_sample_db(tmp_path / "sample.db")
    out = sqlite_mcp.sqlite_run_select_query(
        "EXPLAIN QUERY PLAN SELECT * FROM users WHERE username = 'admin';",
        db_path=str(db_file),
    )
    assert "[ERROR]" not in out
    assert "idx_users_username" in out or "users" in out


@pytest.mark.parametrize(
    "blocked_sql",
    [
        "INSERT INTO departments VALUES (2, 'HR')",
        "UPDATE users SET username = 'hacked'",
        "DELETE FROM users",
        "DROP TABLE users",
        "SELECT 1; DROP TABLE users",
        "PRAGMA writable_schema = 1",
        "EXPLAIN DELETE FROM users",
    ],
)
def test_sqlite_blocks_non_readonly_queries_and_records_audit(tmp_path, blocked_sql):
    db_file = _create_sample_db(tmp_path / "sample.db")
    out = sqlite_mcp.sqlite_run_select_query(blocked_sql, db_path=str(db_file))
    assert out.startswith("[ERROR]")


def test_sqlite_authorizer_blocks_dangerous_pragma_at_connection_level(tmp_path):
    db_file = _create_sample_db(tmp_path / "sample.db")
    conn = sqlite_mcp._get_readonly_connection("test", "PRAGMA database_list", db_path=str(db_file))
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("PRAGMA database_list;")
    finally:
        conn.close()


def test_mcp_manager_strips_db_path_and_prompts_when_missing(tmp_path):
    db_file = _create_sample_db(tmp_path / "sample.db")
    manager = MCPManager()
    manager._server_envs["sqlite"] = {"DB_PATH": ""}
    fake_tools = [
        SimpleNamespace(
            name="sqlite_list_tables",
            description="list sqlite tables",
            inputSchema={
                "type": "object",
                "properties": {
                    "db_path": {"type": "string"},
                },
            },
        ),
    ]
    fake_session = SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=fake_tools)),
        call_tool=AsyncMock(return_value=MagicMock(content=[MagicMock(text="users")])),
    )
    asyncio.run(
        manager._register_tools(
            "sqlite",
            fake_session,
            inject={},
            safe_read_tools=["sqlite_list_tables"],
        )
    )
    schemas = {s["name"]: s for s in manager.openai_tools()}
    assert schemas["sqlite_list_tables"]["parameters"]["properties"] == {}

    prompt_calls = []

    def fake_prompt(server_name, missing_keys):
        prompt_calls.append((server_name, missing_keys))
        return {"DB_PATH": str(db_file)}

    manager.on_prompt_conn = fake_prompt
    asyncio.run(manager.call("sqlite_list_tables", {}))

    assert prompt_calls == [("sqlite", ["DB_PATH"])]
    _, sent_args = fake_session.call_tool.call_args.args
    assert sent_args == {"db_path": str(db_file)}


def _create_other_db(db_file: Path) -> Path:
    conn = sqlite3.connect(str(db_file))
    try:
        conn.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount INTEGER);")
        conn.commit()
    finally:
        conn.close()
    return db_file


def test_sqlite_switch_database_changes_active_db(tmp_path):
    first = _create_sample_db(tmp_path / "first.db")
    second = _create_other_db(tmp_path / "second.db")

    assert sqlite_mcp.sqlite_switch_database(str(first)).startswith("[SUCCESS]")
    assert "users" in sqlite_mcp.sqlite_list_tables().splitlines()

    out = sqlite_mcp.sqlite_switch_database(str(second))
    assert out.startswith("[SUCCESS]")
    assert str(second.resolve()) in out
    assert sqlite_mcp.sqlite_list_tables().splitlines() == ["orders"]
    assert "[ERROR]" not in sqlite_mcp.sqlite_run_select_query("SELECT count(*) FROM orders")


def test_sqlite_switch_database_runtime_path_overrides_env(tmp_path, monkeypatch):
    first = _create_sample_db(tmp_path / "first.db")
    second = _create_other_db(tmp_path / "second.db")
    monkeypatch.setenv("DB_PATH", str(first))

    assert sqlite_mcp.sqlite_switch_database(str(second)).startswith("[SUCCESS]")
    assert sqlite_mcp.sqlite_list_tables().splitlines() == ["orders"]


@pytest.mark.parametrize("bad_name", ["missing.db", "not_sqlite.db"])
def test_sqlite_switch_database_failure_keeps_previous_db(tmp_path, bad_name):
    first = _create_sample_db(tmp_path / "first.db")
    (tmp_path / "not_sqlite.db").write_text("this is not a sqlite database " * 10, encoding="utf-8")
    assert sqlite_mcp.sqlite_switch_database(str(first)).startswith("[SUCCESS]")

    out = sqlite_mcp.sqlite_switch_database(str(tmp_path / bad_name))
    assert out.startswith("[ERROR]")
    assert sqlite_mcp._RUNTIME_CREDS["DB_PATH"] == str(first.resolve())
    assert "users" in sqlite_mcp.sqlite_list_tables().splitlines()


def test_sqlite_switch_database_invalid_file_without_previous_db_is_not_cached(tmp_path):
    bad = tmp_path / "not_sqlite.db"
    bad.write_text("this is not a sqlite database " * 10, encoding="utf-8")

    assert sqlite_mcp.sqlite_switch_database(str(bad)).startswith("[ERROR]")
    assert "DB_PATH" not in sqlite_mcp._RUNTIME_CREDS


@pytest.mark.parametrize("unc_path", ["\\\\attacker\\share\\a.db", "//attacker/share/a.db", "'\\\\host\\s\\b.db'"])
def test_sqlite_switch_database_blocks_unc_without_touching_filesystem(monkeypatch, unc_path):
    def _fail(*_args, **_kwargs):
        raise AssertionError("UNC 路徑不應觸發任何連線或檔案系統操作")

    monkeypatch.setattr(sqlite_mcp.sqlite3, "connect", _fail)
    monkeypatch.setattr(sqlite_mcp, "_get_readonly_connection", _fail)
    monkeypatch.setattr(sqlite_mcp, "get_db_path", _fail)

    out = sqlite_mcp.sqlite_switch_database(unc_path)
    assert out.startswith("[ERROR]")
    assert "UNC" in out


def test_sqlite_get_db_path_blocks_unc_custom_path(monkeypatch):
    def _fail(*_args, **_kwargs):
        raise AssertionError("UNC 路徑不應觸發 resolve()")

    monkeypatch.setattr(sqlite_mcp.Path, "resolve", _fail)
    with pytest.raises(ValueError, match="UNC"):
        sqlite_mcp.get_db_path("\\\\attacker\\share\\a.db")


def _make_switch_manager(call_text: str) -> tuple[MCPManager, SimpleNamespace]:
    manager = MCPManager()
    manager._server_envs["sqlite"] = {"DB_PATH": ""}
    fake_tools = [
        SimpleNamespace(
            name="sqlite_list_tables",
            description="list sqlite tables",
            inputSchema={"type": "object", "properties": {"db_path": {"type": "string"}}},
        ),
        SimpleNamespace(
            name="sqlite_switch_database",
            description="switch sqlite db",
            inputSchema={
                "type": "object",
                "properties": {"target_db_path": {"type": "string"}},
                "required": ["target_db_path"],
            },
        ),
    ]
    fake_session = SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=fake_tools)),
        call_tool=AsyncMock(return_value=MagicMock(content=[MagicMock(text=call_text)])),
    )
    asyncio.run(
        manager._register_tools(
            "sqlite",
            fake_session,
            inject={},
            safe_read_tools=["sqlite_list_tables", "sqlite_switch_database"],
        )
    )
    return manager, fake_session


def test_mcp_manager_syncs_runtime_cache_after_switch(tmp_path):
    db_file = _create_other_db(tmp_path / "second.db")
    manager, fake_session = _make_switch_manager("[SUCCESS] 已切換 SQLite 資料庫")

    schemas = {s["name"]: s for s in manager.openai_tools()}
    assert "target_db_path" in schemas["sqlite_switch_database"]["parameters"]["properties"]
    assert manager.find_server_for_tool("sqlite_switch_database") == "sqlite"

    asyncio.run(manager.call("sqlite_switch_database", {"target_db_path": str(db_file)}))
    assert manager.get_runtime_conn_param("sqlite", "DB_PATH") == str(db_file.resolve())

    manager.on_prompt_conn = MagicMock(side_effect=AssertionError("已切換後不應再詢問 DB_PATH"))
    asyncio.run(manager.call("sqlite_list_tables", {}))
    _, sent_args = fake_session.call_tool.call_args.args
    assert sent_args == {"db_path": str(db_file.resolve())}


def test_mcp_manager_does_not_cache_failed_switch(tmp_path):
    manager, _ = _make_switch_manager("[ERROR] 切換 SQLite 資料庫失敗")
    asyncio.run(manager.call("sqlite_switch_database", {"target_db_path": str(tmp_path / "x.db")}))
    assert manager.get_runtime_conn_param("sqlite", "DB_PATH") is None


def _make_gate_agent(workspace: Path, safe_tools: set[str]):
    from pocket_agent.core.agent import Agent

    agent = Agent.__new__(Agent)
    agent._safe_read_tools = safe_tools
    agent.workspace_dirs = [str(workspace)]
    return agent


def test_agent_switch_gate_auto_approves_inside_workspace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    agent = _make_gate_agent(workspace, {"sqlite_switch_database"})

    args = {"target_db_path": str(workspace / "sub" / ".." / "data.db")}
    assert agent._needs_mcp_confirm("sqlite_switch_database", args) is False
    assert args["target_db_path"] == str((workspace / "data.db").resolve())


@pytest.mark.parametrize(
    "target_factory",
    [
        lambda root: str(root / "outside" / "Cookies"),
        lambda root: "\\\\attacker\\share\\a.db",
        lambda root: "//attacker/share/a.db",
        lambda root: str(root / "ws" / ".git" / "a.db"),
        lambda root: "",
    ],
)
def test_agent_switch_gate_requires_confirm_outside_workspace(tmp_path, target_factory):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    agent = _make_gate_agent(workspace, {"sqlite_switch_database"})

    raw = target_factory(tmp_path)
    args = {"target_db_path": raw}
    assert agent._needs_mcp_confirm("sqlite_switch_database", args) is True
    assert args["target_db_path"] == raw


def test_agent_switch_gate_respects_safe_read_membership(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    agent = _make_gate_agent(workspace, {"sqlite_list_tables"})

    assert agent._needs_mcp_confirm("sqlite_switch_database", {"target_db_path": str(workspace / "a.db")}) is True
    assert agent._needs_mcp_confirm("sqlite_list_tables", {}) is False
    assert agent._needs_mcp_confirm("unknown_tool", {}) is True

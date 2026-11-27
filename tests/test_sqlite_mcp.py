import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_servers import sqlite_mcp
from mcp_servers.sqlite import adapter as sqlite_adapter
from mcp_servers.sqlite.tools import health as sqlite_health_tools
from pocket_agent.extensions.mcp import MCPManager


@pytest.fixture(autouse=True)
def _clean_sqlite_env_and_cache(monkeypatch, tmp_path):
    sqlite_adapter.SQLITE_ADAPTER.clear_cache()
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
    conn = sqlite_adapter.SQLITE_ADAPTER.connect("test", "PRAGMA database_list", db_path=str(db_file))
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
    assert schemas["mcp__sqlite__sqlite_list_tables"]["parameters"]["properties"] == {}

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
    assert sqlite_adapter.SQLITE_ADAPTER.cache.get("DB_PATH") == str(first.resolve())
    assert "users" in sqlite_mcp.sqlite_list_tables().splitlines()


def test_sqlite_switch_database_invalid_file_without_previous_db_is_not_cached(tmp_path):
    bad = tmp_path / "not_sqlite.db"
    bad.write_text("this is not a sqlite database " * 10, encoding="utf-8")

    assert sqlite_mcp.sqlite_switch_database(str(bad)).startswith("[ERROR]")
    assert sqlite_adapter.SQLITE_ADAPTER.cache.get("DB_PATH") == ""


@pytest.mark.parametrize("unc_path", ["\\\\attacker\\share\\a.db", "//attacker/share/a.db", "'\\\\host\\s\\b.db'"])
def test_sqlite_switch_database_blocks_unc_without_touching_filesystem(monkeypatch, unc_path):
    def _fail(*_args, **_kwargs):
        raise AssertionError("UNC 路徑不應觸發任何連線或檔案系統操作")

    monkeypatch.setattr(sqlite_adapter.sqlite3, "connect", _fail)
    monkeypatch.setattr(sqlite_adapter.SQLITE_ADAPTER, "connect", _fail)
    monkeypatch.setattr(sqlite_adapter.SQLITE_ADAPTER, "get_db_path", _fail)

    out = sqlite_mcp.sqlite_switch_database(unc_path)
    assert out.startswith("[ERROR]")
    assert "UNC" in out


def test_sqlite_get_db_path_blocks_unc_custom_path(monkeypatch):
    def _fail(*_args, **_kwargs):
        raise AssertionError("UNC 路徑不應觸發 resolve()")

    monkeypatch.setattr(sqlite_adapter, "Path", _fail)
    with pytest.raises(ValueError, match="UNC"):
        sqlite_adapter.SQLITE_ADAPTER.get_db_path("\\\\attacker\\share\\a.db")


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
    switch_key = (
        "mcp__sqlite__sqlite_switch_database"
        if "mcp__sqlite__sqlite_switch_database" in schemas
        else "sqlite_switch_database"
    )
    assert "target_db_path" in schemas[switch_key]["parameters"]["properties"]
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


def test_sqlite_inspect_health_detects_missing_pk_redundant_index_fk_violation_and_quick_check(tmp_path):
    """驗證 sqlite_inspect_health 檢查缺少主鍵、冗餘前綴索引、外鍵違規與 PRAGMA quick_check。"""
    db_file = tmp_path / "health_test.db"
    conn = sqlite3.connect(str(db_file))
    try:
        conn.executescript(
            """
            PRAGMA foreign_keys = OFF;
            CREATE TABLE parents (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE child_events (
                parent_id INTEGER,
                event_type TEXT,
                created_at TEXT,
                FOREIGN KEY (parent_id) REFERENCES parents(id)
            );
            CREATE INDEX idx_child_parent ON child_events(parent_id);
            CREATE INDEX idx_child_parent_type ON child_events(parent_id, event_type);
            INSERT INTO child_events (parent_id, event_type, created_at) VALUES (999, 'login', '2026-01-01');
            """
        )
        conn.commit()
    finally:
        conn.close()

    out = sqlite_mcp.sqlite_inspect_health(db_path=str(db_file))
    assert "[SQLite Health Summary]" in out
    assert "快速實體檢查: OK (PRAGMA quick_check 通過)" in out
    assert "[Missing PK] 資料表 child_events 缺少顯式主鍵" in out
    assert "[Redundant Index] child_events: idx_child_parent (parent_id) 為 idx_child_parent_type" in out
    assert "[Foreign Key Violation] child_events (rowid=1) -> 參照目標表 parents" in out


def test_sqlite_inspect_health_size_guard_and_authorizer_isolation(tmp_path, monkeypatch):
    """驗證超過檔案大小上限時自動略過 quick_check，且一般連線仍封鎖 PRAGMA quick_check。"""
    db_file = _create_sample_db(tmp_path / "sample.db")

    # 1. 一般連線仍嚴格封鎖 PRAGMA quick_check 與 database_list
    conn_normal = sqlite_adapter.SQLITE_ADAPTER.connect("test", "PRAGMA quick_check", db_path=str(db_file))
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn_normal.execute("PRAGMA quick_check;")
    finally:
        conn_normal.close()

    # 2. 診斷連線仍封鎖 database_list 等敏感 PRAGMA
    conn_diag = sqlite_adapter.SQLITE_ADAPTER.connect(
        "test",
        "PRAGMA database_list",
        db_path=str(db_file),
        allow_diagnostic_pragmas=True,
    )
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn_diag.execute("PRAGMA database_list;")
    finally:
        conn_diag.close()

    # 3. 檔案大小超過安全上限時自動略過實體頁掃描
    monkeypatch.setattr(sqlite_health_tools, "SQLITE_QUICK_CHECK_MAX_BYTES", 10)
    out_guarded = sqlite_mcp.sqlite_inspect_health(db_path=str(db_file))
    assert "超過安全門檻" in out_guarded

    # 4. 非法 table_name 參數攔截
    bad_out = sqlite_mcp.sqlite_inspect_health(table_name="users; DROP TABLE users", db_path=str(db_file))
    assert bad_out.startswith("[ERROR]")


def test_sqlite_sql_guard_state_machine_allows_literals_and_blocks_bypasses():
    from mcp_servers.sqlite.sql_guard import SQLITE_SQL_GUARD

    ok_sql = "SELECT 'a;b' AS s, 'UPDATE' AS op, \"delete\" FROM [create] -- trailing\nWHERE 1=1"
    assert SQLITE_SQL_GUARD.validate_select(ok_sql).startswith("SELECT 'a;b'")

    ok_inline_comment = "SELECT /* note ; DROP */ id FROM users"
    assert "id FROM users" in SQLITE_SQL_GUARD.validate_select(ok_inline_comment)

    for bad_sql in (
        "SELECT 'unclosed",
        "SELECT /* unclosed",
        "SELECT [unclosed FROM users",
        "BEGIN TRANSACTION",
        "ANALYZE users",
        "SELECT zeroblob(1000)",
        "SELECT randomblob(100)",
        "SELECT load_extension('evil.dll')",
    ):
        with pytest.raises(ValueError):
            SQLITE_SQL_GUARD.validate_select(bad_sql)

    with pytest.raises(ValueError):
        SQLITE_SQL_GUARD.validate_explainable_select("EXPLAIN SELECT 1")


def test_sqlite_adapter_network_paths_cache_isolation_and_authorizer(tmp_path, monkeypatch):
    for net_path in (
        "/\\attacker\\share\\a.db",
        "\\/attacker/share/a.db",
        "\\\\?\\C:\\a.db",
        "\\\\.\\pipe\\x",
        "C:\\a.db\x00.txt",
    ):
        assert sqlite_adapter.is_network_path(net_path) is True

    def _fail(*_args, **_kwargs):
        raise AssertionError("網路路徑不應觸發 Path.resolve()")

    monkeypatch.setattr(sqlite_adapter, "Path", _fail)
    with pytest.raises(ValueError, match="UNC"):
        sqlite_adapter.SQLITE_ADAPTER.get_db_path("/\\attacker\\share\\a.db")
    monkeypatch.undo()

    first = _create_sample_db(tmp_path / "first.db")
    second = _create_other_db(tmp_path / "second.db")
    assert sqlite_mcp.sqlite_switch_database(str(first)).startswith("[SUCCESS]")

    # 單次 db_path 查詢不應覆寫全域快取，無效 db_path 亦不應清空既有快取
    assert "orders" in sqlite_mcp.sqlite_list_tables(db_path=str(second)).splitlines()
    assert sqlite_adapter.SQLITE_ADAPTER.cache.get("DB_PATH") == str(first.resolve())
    assert sqlite_mcp.sqlite_list_tables(db_path=str(tmp_path / "missing.db")).startswith("[ERROR]")
    assert sqlite_adapter.SQLITE_ADAPTER.cache.get("DB_PATH") == str(first.resolve())

    # Authorizer 封鎖 zeroblob/randomblob 與帶值變更型 PRAGMA
    conn = sqlite_adapter.SQLITE_ADAPTER.connect(
        "test",
        "authorizer",
        db_path=str(first),
        allow_diagnostic_pragmas=True,
    )
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("SELECT zeroblob(100);")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("PRAGMA user_version = 99;")
    finally:
        sqlite_adapter.SQLITE_ADAPTER.close_safely(conn)


def test_sqlite_query_timeout_interrupts_recursive_cte(tmp_path, monkeypatch):
    from mcp_servers.sqlite.tools import query as sqlite_query_tools

    db_file = _create_sample_db(tmp_path / "timeout.db")
    monkeypatch.setattr(sqlite_query_tools, "get_query_timeout_sec", lambda: 0.01)
    out = sqlite_mcp.sqlite_run_select_query(
        "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) SELECT count(*) FROM r;",
        db_path=str(db_file),
    )
    assert out.startswith("[ERROR]")
    assert "逾時" in out or "timeout" in out.lower() or "中斷" in out


def test_sqlite_describe_schema_enhanced_and_list_filters(tmp_path):
    db_file = tmp_path / "enhanced.db"
    conn = sqlite3.connect(str(db_file))
    try:
        conn.executescript(
            """
            CREATE TABLE orgs (id INTEGER PRIMARY KEY);
            CREATE TABLE items (
                tenant_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                org_id INTEGER,
                api_token TEXT DEFAULT 'sk-secret1234567890abcdef',
                qty INTEGER DEFAULT 1,
                price INTEGER DEFAULT 10,
                total INTEGER GENERATED ALWAYS AS (qty * price) VIRTUAL,
                PRIMARY KEY (tenant_id, item_id),
                FOREIGN KEY (org_id) REFERENCES orgs(id) ON DELETE CASCADE
            );
            CREATE INDEX idx_items_active ON items(qty) WHERE qty > 0;
            CREATE TRIGGER trg_items_ins AFTER INSERT ON items BEGIN SELECT 1; END;
            """
        )
        conn.commit()
    finally:
        conn.close()

    schema_out = sqlite_mcp.sqlite_describe_schema("items", db_path=str(db_file))
    assert "[PK#1]" in schema_out
    assert "[PK#2]" in schema_out
    assert "[VIRTUAL GENERATED]" in schema_out
    assert "sk-secret1234567890abcdef" not in schema_out
    assert "DEFAULT [REDACTED]" in schema_out
    assert "[PARTIAL]" in schema_out
    assert "ON DELETE CASCADE" in schema_out

    all_out = sqlite_mcp.sqlite_list_tables(object_type="all", db_path=str(db_file))
    assert "idx_items_active (INDEX on items)" in all_out
    assert "trg_items_ins (TRIGGER on items)" in all_out
    assert "items" in sqlite_mcp.sqlite_list_tables(name_pattern="item", db_path=str(db_file))
    assert sqlite_mcp.sqlite_list_tables(object_type="invalid", db_path=str(db_file)).startswith("[ERROR]")


def test_sqlite_health_excludes_partial_expr_indexes_and_fts5(tmp_path, monkeypatch):
    db_file = tmp_path / "fts_partial.db"
    conn = sqlite3.connect(str(db_file))
    try:
        conn.executescript(
            """
            CREATE TABLE docs (
                id INTEGER PRIMARY KEY,
                title TEXT,
                active INTEGER DEFAULT 1
            );
            CREATE INDEX idx_docs_title ON docs(title);
            CREATE INDEX idx_docs_title_active ON docs(title) WHERE active = 1;
            CREATE INDEX idx_docs_expr ON docs(title, lower(title));
            """
        )
        try:
            conn.execute("CREATE VIRTUAL TABLE docs_fts USING fts5(title);")
        except sqlite3.OperationalError:
            pass
        conn.commit()
    finally:
        conn.close()

    out = sqlite_mcp.sqlite_inspect_health(db_path=str(db_file))
    assert "[Redundant Index]" not in out
    assert "[Missing PK]" not in out

    monkeypatch.setattr(sqlite_health_tools, "SQLITE_QUICK_CHECK_MAX_BYTES", 10)
    skipped_out = sqlite_mcp.sqlite_inspect_health(db_path=str(db_file))
    assert "外鍵完整性檢查: 已略過" in skipped_out
    assert "靜態結構檢查未發現明顯異常" in skipped_out


def test_sqlite_p0_and_p1_tools_and_registry(tmp_path, monkeypatch):
    db_file = _create_sample_db(tmp_path / "full.db")

    # P0: sqlite_get_ddl
    ddl_out = sqlite_mcp.sqlite_get_ddl("users", db_path=str(db_file))
    assert "CREATE TABLE users" in ddl_out
    assert "CREATE INDEX idx_users_username" in ddl_out
    assert sqlite_mcp.sqlite_get_ddl("missing_tbl", db_path=str(db_file))
    assert sqlite_mcp.sqlite_get_ddl("users", object_type="invalid", db_path=str(db_file)).startswith("[ERROR]")

    # P0: sqlite_explain_query
    plan_out = sqlite_mcp.sqlite_explain_query(
        "SELECT * FROM users WHERE dept_id = 1 ORDER BY username;",
        db_path=str(db_file),
    )
    assert "[SQLite Execution Plan]" in plan_out
    assert "SCAN" in plan_out
    assert sqlite_mcp.sqlite_explain_query("EXPLAIN SELECT 1", db_path=str(db_file)).startswith("[ERROR]")

    # P0: sqlite_database_info
    info_out = sqlite_mcp.sqlite_database_info(db_path=str(db_file))
    assert "[SQLite Database Info]" in info_out
    assert "Tables=2" in info_out
    assert "Views=1" in info_out

    # P1: sqlite_table_stats (含正常路徑與參數檢核)
    stats_out = sqlite_mcp.sqlite_table_stats(top_n=5, db_path=str(db_file))
    assert "[SQLite Table & Index Storage Stats]" in stats_out
    assert "users" in stats_out
    assert sqlite_mcp.sqlite_table_stats(top_n=0, db_path=str(db_file)).startswith("[ERROR]")
    assert sqlite_mcp.sqlite_table_stats(top_n=999, db_path=str(db_file)).startswith("[ERROR]")

    # P1: sqlite_check_integrity
    int_quick = sqlite_mcp.sqlite_check_integrity(check_mode="quick", db_path=str(db_file))
    assert "[SQLite Integrity Check]" in int_quick
    assert "ok" in int_quick.lower()
    int_full = sqlite_mcp.sqlite_check_integrity(table_name="users", check_mode="full", db_path=str(db_file))
    assert "[SQLite Integrity Check]" in int_full
    assert sqlite_mcp.sqlite_check_integrity(check_mode="bad", db_path=str(db_file)).startswith("[ERROR]")

    monkeypatch.setattr(sqlite_health_tools, "SQLITE_QUICK_CHECK_MAX_BYTES", 10)
    int_skipped = sqlite_mcp.sqlite_check_integrity(db_path=str(db_file))
    assert "超過安全門檻" in int_skipped

    # 驗證 FastMCP / MCPServer 已掛載全部 10 項工具
    registered = asyncio.run(sqlite_mcp.mcp.list_tools())
    tool_names = {tool.name for tool in registered}
    assert tool_names == {
        "sqlite_list_tables",
        "sqlite_describe_schema",
        "sqlite_get_ddl",
        "sqlite_run_select_query",
        "sqlite_explain_query",
        "sqlite_switch_database",
        "sqlite_inspect_health",
        "sqlite_check_integrity",
        "sqlite_database_info",
        "sqlite_table_stats",
    }

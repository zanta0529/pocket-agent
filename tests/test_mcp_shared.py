"""Unit tests for mcp_servers._shared foundation package."""

import builtins
from unittest.mock import MagicMock

import pytest

from mcp_servers._shared import (
    AuditLogger,
    CredentialCache,
    CredentialFieldSpec,
    CredentialResolver,
    DefaultRedactionPolicy,
    FunctionRegistrar,
    McpConstants,
    ReadOnlyDatabaseAdapter,
    SqlGuardStrategy,
    SystemTtyPrompter,
    bootstrap,
    clean_param,
    create_mcp_server,
    find_redundant_prefix_indexes,
    format_bytes,
    format_cell_value,
    is_empty_or_null,
    stream_query_results,
    truncate_health_report,
    validate_host,
    validate_identifier,
    validate_port,
    validate_positive_int,
)
from pocket_agent.system.i18n import set_locale


class _FakeClock:
    def __init__(self, initial: float = 100.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakePrompter:
    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[str, bool, str]] = []

    def prompt(self, prompt_text: str, *, is_password: bool = False, hint: str = "") -> str:
        self.calls.append((prompt_text, is_password, hint))
        return self._answers.pop(0) if self._answers else ""


def test_bootstrap_fallback_when_pocket_agent_unavailable(monkeypatch):
    real_import = builtins.__import__

    def _blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith("pocket_agent"):
            raise ImportError("simulated standalone environment")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    fallback_consts = bootstrap.load_constants()
    assert fallback_consts == McpConstants()
    assert bootstrap.t("mcp_shared.invalid_port", env_name="MYSQL_PORT") == "mcp_shared.invalid_port"


def test_redaction_policy_masks_secrets_and_preserves_marker_boundary():
    policy = DefaultRedactionPolicy()
    pem = "-----BEGIN RSA PRIVATE KEY-----\nSECRETKEYDATA\n-----END RSA PRIVATE KEY-----"
    text = (
        f"url=https://admin:SuperPass123@db.internal/app "
        f"token=ghp_1234567890abcdefghij123456 "
        f"Authorization: Bearer my-bearer-secret "
        f"api_key=PlainApiSecret99 "
        f"custom=MyDynamicSecretValue\n{pem}"
    )

    redacted = policy.redact(text, extra_secret="MyDynamicSecretValue")
    assert "SuperPass123" not in redacted
    assert "ghp_1234567890abcdefghij123456" not in redacted
    assert "my-bearer-secret" not in redacted
    assert "PlainApiSecret99" not in redacted
    assert "MyDynamicSecretValue" not in redacted
    assert "SECRETKEYDATA" not in redacted
    assert "[REDACTED_PRIVATE_KEY]" in redacted
    assert "[REDACTED_TOKEN]" in redacted

    # Ensure clipping never splits a [REDACTED...] token in half
    sample = "prefix [REDACTED_TOKEN] suffix"
    clipped = policy.clip_text(sample, limit=12, suffix="...")
    assert clipped.startswith("prefix [REDACTED_TOKEN]")

    truncated, was_truncated = policy.truncate_output("a" * 50, max_chars=20, hint="narrow")
    assert was_truncated is True
    assert "20" in truncated


def test_audit_logger_writes_sanitized_single_line_record(monkeypatch, tmp_path):
    log_file = tmp_path / "agent.log"
    monkeypatch.setenv("POCKET_AGENT_LOG_FILE", str(log_file))

    logger = AuditLogger("test-mcp")
    logger.log(
        "test_tool",
        "SELECT 1\npassword=TopSecret123",
        target="user host=evil\nline",
        status="OK",
        detail="secret=DynamicToken999",
        extra_secret="DynamicToken999",
    )

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = lines[0]
    assert "[AUDIT] [MCP:test-mcp] tool=test_tool status=OK" in entry
    assert "TopSecret123" not in entry
    assert "DynamicToken999" not in entry
    assert "target=user_host_evil_line" in entry


def test_credential_cache_ttl_and_target_clear():
    clock = _FakeClock()
    cache = CredentialCache(ttl_seconds=60.0, clock=clock)

    cache.update({"HOST": "10.0.0.1", "PASSWORD": "pw1"})
    cache.set_target_secret(("10.0.0.1", "22", "ops"), "pw1")
    cache.set_target_secret(("10.0.0.2", "22", "ops"), "pw2")

    assert cache.get("HOST") == "10.0.0.1"
    assert cache.get_target_secret(("10.0.0.1", "22", "ops")) == "pw1"

    cache.clear(target_host="10.0.0.1", host_env_key="HOST")
    assert cache.get("HOST") == ""
    assert cache.get_target_secret(("10.0.0.1", "22", "ops")) == ""
    assert cache.get_target_secret(("10.0.0.2", "22", "ops")) == "pw2"

    clock.advance(61.0)
    assert cache.get_target_secret(("10.0.0.2", "22", "ops")) == ""


def test_credential_resolver_precedence(monkeypatch):
    clock = _FakeClock()
    cache = CredentialCache(ttl_seconds=600.0, clock=clock)
    prompter = _FakePrompter(["prompted-host", "prompted-pw"])
    resolver = CredentialResolver(cache, prompter=prompter, hint="DEMO_*")

    host_spec = CredentialFieldSpec(
        env_key="DEMO_HOST",
        prompt_text="Host: ",
        validator=lambda v: validate_host(v, "DEMO_HOST"),
    )
    pw_spec = CredentialFieldSpec(env_key="DEMO_PASS", prompt_text="Password: ", is_secret=True)

    monkeypatch.delenv("DEMO_HOST", raising=False)
    monkeypatch.delenv("DEMO_PASS", raising=False)

    # 1. Falls back to TTY prompt when nothing is set
    assert resolver.resolve_field(host_spec) == "prompted-host"
    assert resolver.resolve_field(pw_spec) == "prompted-pw"
    assert len(prompter.calls) == 2

    # 2. Environment variable beats TTY prompt
    monkeypatch.setenv("DEMO_HOST", "env.example.com")
    assert resolver.resolve_field(host_spec) == "env.example.com"

    # 3. Cache beats environment variable
    cache.set("DEMO_HOST", "cached.example.com")
    assert resolver.resolve_field(host_spec) == "cached.example.com"

    # 4. Injected argument beats cache
    assert resolver.resolve_field(host_spec, injected="injected.example.com") == "injected.example.com"


def test_validation_helpers_and_i18n():
    assert is_empty_or_null(None) is True
    assert is_empty_or_null("  null ") is True
    assert is_empty_or_null("~") is True
    assert clean_param(" 'app_db' ") == "app_db"

    assert validate_port("03306", "MYSQL_PORT") == "3306"
    with pytest.raises(ValueError, match="MYSQL_PORT"):
        validate_port("70000", "MYSQL_PORT")

    assert validate_identifier("my_table", "table_name") == "my_table"
    with pytest.raises(ValueError):
        validate_identifier("-flag", "table_name")

    assert validate_host("2001:0db8::1", bracket_ipv6=True) == "[2001:db8::1]"
    assert validate_host(r"sqlhost\SQLEXPRESS", allow_backslash=True) == r"sqlhost\SQLEXPRESS"
    with pytest.raises(ValueError):
        validate_host(r"sqlhost\SQLEXPRESS", allow_backslash=False)

    assert validate_positive_int(5, "page", max_val=10) == 5
    with pytest.raises(ValueError):
        validate_positive_int(True, "page")
    with pytest.raises(ValueError):
        validate_positive_int(11, "page", max_val=10)

    # Verify English locale translation
    set_locale("en_US")
    with pytest.raises(ValueError, match="Invalid MYSQL_PORT port value"):
        validate_port("0", "MYSQL_PORT")


def test_system_tty_prompter_fails_closed_without_tty(monkeypatch):
    prompter = SystemTtyPrompter()
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: False))
    monkeypatch.setattr("sys.stderr", MagicMock(isatty=lambda: False))

    with pytest.raises(ValueError):
        prompter.prompt("Password: ", is_password=True)


def test_create_mcp_server_with_registrars():
    registered: list[str] = []

    class _FakeMCPServer:
        def __init__(self, name: str, **kwargs) -> None:
            self.name = name
            self.kwargs = kwargs

        def tool(self):
            def _decorator(fn):
                registered.append(fn.__name__)
                return fn

            return _decorator

    def tool_alpha() -> str:
        return "a"

    def tool_beta() -> str:
        return "b"

    def custom_register(mcp) -> None:
        mcp.tool()(tool_beta)

    original = bootstrap.get_mcp_server_class
    try:
        from mcp_servers._shared import registry as registry_mod

        registry_mod.get_mcp_server_class = lambda: _FakeMCPServer
        server = create_mcp_server(
            "demo",
            [FunctionRegistrar([tool_alpha]), custom_register],
        )
        assert server.name == "demo"
        assert registered == ["tool_alpha", "tool_beta"]
    finally:
        registry_mod.get_mcp_server_class = original


def test_db_base_streaming_redaction_and_helpers():
    cursor = MagicMock()
    cursor.description = [("id",), ("password_hash",), ("note",)]
    batches = [
        [(1, "secret123", "line1\nline2"), (2, "secret456", None)],
        [(3, "secret789", "x" * 200)],
        [],
    ]
    cursor.fetchmany.side_effect = lambda _size=100: batches.pop(0) if batches else []

    res = stream_query_results(cursor, max_chars=80, batch_size=2)
    assert res.truncated is True
    assert res.stream_completed is False
    assert res.shown_rows == 2
    assert "secret123" not in res.output
    assert "[REDACTED]" in res.output
    assert "line1\\nline2" in res.output
    assert "NULL" in res.output

    # Empty description & empty rows
    empty_desc_cursor = MagicMock(description=None)
    assert stream_query_results(empty_desc_cursor).total_rows == 0

    empty_rows_cursor = MagicMock(description=[("id",)])
    empty_rows_cursor.fetchmany.return_value = []
    assert stream_query_results(empty_rows_cursor).total_rows == 0

    assert format_cell_value("secret", is_sensitive=True) == "[REDACTED]"
    assert format_bytes(1536) == "1.50 KB"
    assert format_bytes(None) == "0 B"

    findings = find_redundant_prefix_indexes(
        {
            "idx_user": (False, ["user_id"]),
            "idx_user_created": (False, ["user_id", "created_at"]),
            "idx_dup_a": (False, ["email"]),
            "idx_dup_b": (False, ["email"]),
            "uniq_code": (True, ["code"]),
            "idx_code_ts": (False, ["code", "ts"]),
        }
    )
    assert len(findings) == 2
    assert any("idx_user" in f and "idx_user_created" in f for f in findings)
    assert any("idx_dup_a" in f and "idx_dup_b" in f for f in findings)

    report = truncate_health_report(["line1", "password=HiddenSecret", "z" * 100], max_chars=40)
    assert "HiddenSecret" not in report
    assert "40" in report


def test_db_abstract_base_classes_enforce_contract():
    with pytest.raises(TypeError):
        SqlGuardStrategy()  # type: ignore[abstract]
    with pytest.raises(TypeError):
        ReadOnlyDatabaseAdapter()  # type: ignore[abstract]

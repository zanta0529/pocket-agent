import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from rich.console import Console

from mcp_servers import github_mcp
from pocket_agent.extensions.mcp import MCPManager
from pocket_agent.frontend.cli.render import CliRenderer


@pytest.fixture(autouse=True)
def _clean_github_env_and_cache(monkeypatch, tmp_path):
    github_mcp._clear_creds_cache()
    monkeypatch.setenv("POCKET_AGENT_LOG_FILE", str(tmp_path / "agent.log"))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_ALLOWED_REPOS", raising=False)


def _mock_http_client(
    *,
    json_data=None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
):
    req = httpx.Request("GET", "https://api.github.com/repos/octocat/hello/pulls")
    resp = httpx.Response(status_code, json=json_data, headers=headers or {}, request=req)
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.post = AsyncMock(return_value=resp)
    cm = AsyncMock()
    cm.__aenter__.return_value = client
    cm.__aexit__.return_value = False
    return cm, client


def test_github_list_prs_handles_null_user_and_redacts_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")
    cm, client = _mock_http_client(
        json_data=[
            {"number": 10, "title": "Fix bug for deleted account", "user": None},
            {"number": 11, "title": "Leak api_key=SuperSecretKey123", "user": {"login": "octocat"}},
        ],
    )
    with patch("mcp_servers.github_mcp._create_http_client", return_value=cm):
        out = asyncio.run(github_mcp.github_list_prs("octocat", "hello", state="open", per_page=20, page=2))

    assert "#10 Fix bug for deleted account (ghost)" in out
    assert "#11 Leak api_key=[REDACTED] (octocat)" in out
    assert "SuperSecretKey123" not in out
    client.get.assert_awaited_once()
    _, kwargs = client.get.call_args
    assert kwargs["params"] == {"state": "open", "per_page": 20, "page": 2}
    assert kwargs["headers"]["User-Agent"] == "PocketAgent-GitHub-MCP"

    audit_text = (tmp_path / "agent.log").read_text(encoding="utf-8")
    assert "[AUDIT] [MCP:github] tool=github_list_prs status=OK target=octocat/hello" in audit_text
    assert "test_pat_value_999" not in audit_text


def test_github_read_issue_handles_null_body_and_truncates_long_output(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")
    cm_null, _ = _mock_http_client(
        json_data={"title": "Empty body issue", "state": "open", "user": None, "body": None},
    )
    with patch("mcp_servers.github_mcp._create_http_client", return_value=cm_null):
        out_null = asyncio.run(github_mcp.github_read_issue("octocat", "hello", 5))

    assert "標題：Empty body issue" in out_null
    assert "作者：ghost" in out_null
    assert "None" not in out_null

    long_body = "X" * (github_mcp.MAX_OUTPUT_CHARS + 300)
    cm_long, _ = _mock_http_client(
        json_data={"title": "Huge issue", "state": "closed", "user": {"login": "dev"}, "body": long_body},
    )
    with patch("mcp_servers.github_mcp._create_http_client", return_value=cm_long):
        out_long = asyncio.run(github_mcp.github_read_issue("octocat", "hello", 6))

    assert "輸出已截斷" in out_long
    assert len(out_long) < len(long_body)


@pytest.mark.parametrize(
    ("owner", "repo", "issue_no", "state", "per_page", "page"),
    [
        ("..", "hello", 1, "open", 30, 1),
        ("octocat", "..", 1, "open", 30, 1),
        ("octocat/evil", "hello", 1, "open", 30, 1),
        ("octocat", "hello/pulls/1/merge#", 1, "open", 30, 1),
        ("octocat", "hello?admin=1", 1, "open", 30, 1),
        ("octocat", "hello", 0, "open", 30, 1),
        ("octocat", "hello", -3, "open", 30, 1),
        ("octocat", "hello", 1, "merged", 30, 1),
        ("octocat", "hello", 1, "open", 101, 1),
        ("octocat", "hello", 1, "open", 30, 0),
    ],
)
def test_github_blocks_path_traversal_and_invalid_params(
    monkeypatch,
    owner,
    repo,
    issue_no,
    state,
    per_page,
    page,
):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")
    with patch("mcp_servers.github_mcp._create_http_client") as mock_factory:
        if issue_no < 1:
            out = asyncio.run(github_mcp.github_read_issue(owner, repo, issue_no))
        else:
            out = asyncio.run(
                github_mcp.github_list_prs(owner, repo, state=state, per_page=per_page, page=page),
            )
    assert out.startswith("[ERROR]")
    mock_factory.assert_not_called()


def test_github_allowed_repos_whitelist_and_audit_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")
    monkeypatch.setenv("GITHUB_ALLOWED_REPOS", " MyOrg/Allowed-Repo , second/repo ")

    with patch("mcp_servers.github_mcp._create_http_client") as mock_factory:
        blocked = asyncio.run(github_mcp.github_list_prs("evil-org", "secret-repo"))
    assert blocked.startswith("[ERROR] 拒絕存取")
    mock_factory.assert_not_called()

    cm, _ = _mock_http_client(json_data=[])
    with patch("mcp_servers.github_mcp._create_http_client", return_value=cm):
        allowed_out = asyncio.run(github_mcp.github_list_prs("myorg", "allowed-repo"))
    assert "目前沒有狀態為 open 的 Pull Requests" in allowed_out

    audit_text = (tmp_path / "agent.log").read_text(encoding="utf-8")
    assert "status=BLOCKED target=evil-org/secret-repo" in audit_text


@pytest.mark.parametrize(
    "secret_body",
    [
        "Here is my token: ghp_1234567890abcdefghijklmnop",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
        "Conn: postgres://admin:SecretPass@10.0.0.5:5432/prod",
        "Leak active: current_active_pat_secret_123",
    ],
)
def test_github_create_comment_blocks_outbound_secrets(monkeypatch, secret_body):
    monkeypatch.setenv("GITHUB_TOKEN", "current_active_pat_secret_123")
    with patch("mcp_servers.github_mcp._create_http_client") as mock_factory:
        out = asyncio.run(github_mcp.github_create_comment("octocat", "hello", 12, secret_body))
    assert out.startswith("[ERROR] 安全攔截")
    mock_factory.assert_not_called()


def test_github_create_comment_allows_normal_prose_with_token_word(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "current_active_pat_secret_123")
    cm, client = _mock_http_client(json_data={"id": 99}, status_code=201)
    body_text = "LGTM! Fixed OAuth token refresh timeout in edge cases."
    with patch("mcp_servers.github_mcp._create_http_client", return_value=cm):
        out = asyncio.run(github_mcp.github_create_comment("octocat", "hello", 12, body_text))

    assert out == "成功在 octocat/hello#12 新增留言。"
    client.post.assert_awaited_once()


def test_github_http_401_clears_cache_and_403_rate_limit():
    cm_401, _ = _mock_http_client(json_data={"message": "Bad credentials"}, status_code=401)
    with patch("mcp_servers.github_mcp._create_http_client", return_value=cm_401):
        out_401 = asyncio.run(
            github_mcp.github_list_prs("octocat", "hello", github_token="expired_pat_token"),
        )
    assert "[CONN_AUTH]" in out_401
    assert github_mcp._RUNTIME_CREDS == {}

    cm_403, _ = _mock_http_client(
        json_data={"message": "API rate limit exceeded"},
        status_code=403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1799999999"},
    )
    with patch("mcp_servers.github_mcp._create_http_client", return_value=cm_403):
        out_403 = asyncio.run(
            github_mcp.github_list_prs("octocat", "hello", github_token="valid_pat_token"),
        )
    assert "Rate Limit Exceeded" in out_403
    assert "1799999999" in out_403


def test_mcp_manager_strips_github_token_and_clears_cache_on_conn_auth():
    manager = MCPManager()
    manager._server_envs["github"] = {"GITHUB_TOKEN": ""}
    manager.on_prompt_conn = MagicMock(return_value={"GITHUB_TOKEN": "prompted_pat_xyz"})
    fake_tools = [
        SimpleNamespace(
            name="github_list_prs",
            description="list prs",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "github_token": {"type": "string"},
                },
                "required": ["owner", "repo"],
            },
        ),
    ]
    fake_session = SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=fake_tools)),
        call_tool=AsyncMock(
            return_value=MagicMock(content=[MagicMock(text="[ERROR] [CONN_AUTH] GitHub 認證失敗")]),
        ),
    )
    asyncio.run(
        manager._register_tools(
            "github",
            fake_session,
            inject={},
            safe_read_tools=["github_list_prs"],
        ),
    )
    schemas = {s["name"]: s for s in manager.openai_tools()}
    assert "github_token" not in schemas["mcp__github__github_list_prs"]["parameters"]["properties"]

    res = asyncio.run(manager.call("mcp__github__github_list_prs", {"owner": "octocat", "repo": "hello"}))
    manager.on_prompt_conn.assert_called_once_with("github", ["GITHUB_TOKEN"])
    fake_session.call_tool.assert_awaited_once_with(
        "github_list_prs",
        {"owner": "octocat", "repo": "hello", "github_token": "prompted_pat_xyz"},
    )
    assert "[CONN_AUTH]" in res
    assert "github" not in manager._runtime_conn_cache


def test_cli_renderer_summarises_github_tool_args():
    renderer = CliRenderer(Console())
    raw_pr = '{"owner": "octocat", "repo": "hello"}'
    assert renderer._summarise_args("github_list_prs", raw_pr) == "octocat/hello"
    raw_comment = '{"owner": "octocat", "repo": "hello", "issue_number": 42, "body": "hi"}'
    assert renderer._summarise_args("github_create_comment", raw_comment) == "octocat/hello#42"

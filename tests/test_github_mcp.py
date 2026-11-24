import asyncio
import gzip
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from rich.console import Console

from mcp_servers import github_mcp
from mcp_servers._shared import ConnAuthError
from mcp_servers.github import client as gh_client
from mcp_servers.github import security as gh_security
from pocket_agent.extensions.mcp import MCPManager
from pocket_agent.frontend.cli.render import CliRenderer


@pytest.fixture(autouse=True)
def _clean_github_env_and_cache(monkeypatch, tmp_path):
    gh_client.clear_creds_cache()
    monkeypatch.setenv("POCKET_AGENT_LOG_FILE", str(tmp_path / "agent.log"))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_ALLOWED_REPOS", raising=False)


def _mock_http_client(
    *,
    json_data=None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    responses: list[httpx.Response] | None = None,
):
    recorded: list[httpx.Request] = []
    idx = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal idx
        recorded.append(request)
        if responses is not None:
            resp = responses[min(idx, len(responses) - 1)]
            idx += 1
            return resp
        return httpx.Response(status_code, json=json_data, headers=headers or {})

    return _transport_client_cm(handler), recorded


def test_github_list_prs_handles_null_user_and_redacts_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")
    cm, recorded = _mock_http_client(
        json_data=[
            {"number": 10, "title": "Fix bug for deleted account", "user": None},
            {"number": 11, "title": "Leak api_key=SuperSecretKey123", "user": {"login": "octocat"}},
        ],
    )
    with patch("mcp_servers.github.client.create_http_client", return_value=cm):
        out = asyncio.run(github_mcp.github_list_prs("octocat", "hello", state="open", per_page=20, page=2))

    assert "#10 Fix bug for deleted account (ghost)" in out
    assert "#11 Leak api_key=[REDACTED] (octocat)" in out
    assert "SuperSecretKey123" not in out
    assert len(recorded) == 1
    assert recorded[0].method == "GET"
    assert dict(recorded[0].url.params) == {"state": "open", "per_page": "20", "page": "2"}
    assert recorded[0].headers["User-Agent"] == "PocketAgent-GitHub-MCP"

    audit_text = (tmp_path / "agent.log").read_text(encoding="utf-8")
    assert "[AUDIT] [MCP:github] tool=github_list_prs status=OK target=octocat/hello" in audit_text
    assert "test_pat_value_999" not in audit_text


def test_github_read_issue_handles_null_body_and_truncates_long_output(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")
    cm_null, _ = _mock_http_client(
        json_data={"title": "Empty body issue", "state": "open", "user": None, "body": None},
    )
    with patch("mcp_servers.github.client.create_http_client", return_value=cm_null):
        out_null = asyncio.run(github_mcp.github_read_issue("octocat", "hello", 5))

    assert "標題：Empty body issue" in out_null
    assert "作者：ghost" in out_null
    assert "None" not in out_null

    long_body = "X" * (gh_security.MAX_OUTPUT_CHARS + 300)
    cm_long, _ = _mock_http_client(
        json_data={"title": "Huge issue", "state": "closed", "user": {"login": "dev"}, "body": long_body},
    )
    with patch("mcp_servers.github.client.create_http_client", return_value=cm_long):
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
    with patch("mcp_servers.github.client.create_http_client") as mock_factory:
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

    with patch("mcp_servers.github.client.create_http_client") as mock_factory:
        blocked = asyncio.run(github_mcp.github_list_prs("evil-org", "secret-repo"))
    assert blocked.startswith("[ERROR] 拒絕存取")
    mock_factory.assert_not_called()

    cm, _ = _mock_http_client(json_data=[])
    with patch("mcp_servers.github.client.create_http_client", return_value=cm):
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
    with patch("mcp_servers.github.client.create_http_client") as mock_factory:
        out = asyncio.run(github_mcp.github_create_comment("octocat", "hello", 12, secret_body))
    assert out.startswith("[ERROR] 安全攔截")
    mock_factory.assert_not_called()


def test_github_create_comment_allows_normal_prose_with_token_word(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "current_active_pat_secret_123")
    cm, recorded = _mock_http_client(json_data={"id": 99}, status_code=201)
    body_text = "LGTM! Fixed OAuth token refresh timeout in edge cases."
    with patch("mcp_servers.github.client.create_http_client", return_value=cm):
        out = asyncio.run(github_mcp.github_create_comment("octocat", "hello", 12, body_text))

    assert out == "成功在 octocat/hello#12 新增留言。"
    assert len(recorded) == 1
    assert recorded[0].method == "POST"


def test_github_http_401_clears_cache_and_403_rate_limit():
    cm_401, _ = _mock_http_client(json_data={"message": "Bad credentials"}, status_code=401)
    with patch("mcp_servers.github.client.create_http_client", return_value=cm_401):
        out_401 = asyncio.run(
            github_mcp.github_list_prs("octocat", "hello", github_token="expired_pat_token"),
        )
    assert "[CONN_AUTH]" in out_401
    assert gh_client.CREDS_CACHE.get("GITHUB_TOKEN") == ""

    cm_403, _ = _mock_http_client(
        json_data={"message": "API rate limit exceeded"},
        status_code=403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1799999999"},
    )
    with patch("mcp_servers.github.client.create_http_client", return_value=cm_403):
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
    raw_pr_files = '{"owner": "octocat", "repo": "hello", "pull_number": 88}'
    assert renderer._summarise_args("github_get_pr_files", raw_pr_files) == "octocat/hello#88"
    raw_checks = '{"owner": "octocat", "repo": "hello", "ref": "main"}'
    assert renderer._summarise_args("github_get_pr_checks", raw_checks) == "octocat/hello@main"


def test_tty_secret_prompt_fails_fast_when_not_tty(monkeypatch):
    fake_stream = SimpleNamespace(isatty=lambda: False)
    monkeypatch.setattr("sys.stdin", fake_stream)
    monkeypatch.setattr("sys.stderr", fake_stream)
    with pytest.raises(ConnAuthError, match="CONN_AUTH"):
        gh_client.resolve_token()


def test_redirect_validation_blocks_external_host_and_unallowed_repos(monkeypatch):
    monkeypatch.setenv("GITHUB_ALLOWED_REPOS", "myorg/allowed-repo")

    with pytest.raises(PermissionError, match="外部網域"):
        gh_security.validate_redirect_url(httpx.URL("https://evil.example.com/steal"))

    with pytest.raises(PermissionError, match="非 HTTPS"):
        gh_security.validate_redirect_url(httpx.URL("http://api.github.com/repos/myorg/allowed-repo"))

    with pytest.raises(PermissionError, match="GITHUB_ALLOWED_REPOS"):
        gh_security.validate_redirect_url(httpx.URL("https://api.github.com/repos/other-org/secret-repo/pulls"))

    with pytest.raises(PermissionError, match="/repositories/:id"):
        gh_security.validate_redirect_url(httpx.URL("https://api.github.com/repositories/123456/pulls"))

    gh_security.validate_redirect_url(httpx.URL("https://api.github.com/repos/MyOrg/Allowed-Repo/pulls?page=2"))


def test_response_size_limit_and_boundary_secret_redaction(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")

    def oversized_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"title": "big"},
            headers={"content-length": str(gh_security.MAX_RESPONSE_BYTES + 100)},
        )

    with patch("mcp_servers.github.client.create_http_client", lambda: _transport_client_cm(oversized_handler)):
        out_oversized = asyncio.run(github_mcp.github_read_issue("octocat", "hello", 1))
    assert out_oversized.startswith("[ERROR]")
    assert "回應內容過大" in out_oversized

    prefix = ("A" * (gh_security.MAX_OUTPUT_CHARS - 6)) + " "
    boundary_secret = "ghp_1234567890abcdefghijklmnop"
    raw = prefix + boundary_secret + " " + ("B" * 5000)
    out, truncated = gh_security.GITHUB_REDACTION.sanitize_and_truncate(raw, "test_pat_value_999", "hint")
    assert truncated is True
    assert "ghp_12345" not in out
    assert "[REDACTED_TOKEN]" in out


def test_audit_log_sanitizes_target_against_log_injection(tmp_path):
    malicious_owner = "octocat status=OK detail='forged'"
    out = asyncio.run(github_mcp.github_list_prs(malicious_owner, "hello", github_token="test_pat_value_999"))
    assert out.startswith("[ERROR]")
    audit_text = (tmp_path / "agent.log").read_text(encoding="utf-8")
    assert "status=OK detail='forged'" not in audit_text
    assert "target=octocat_status_OK_detail_forged_/hello" in audit_text


def test_shared_http_client_reuses_instance_in_same_event_loop():
    async def _run():
        async with gh_client.create_http_client() as c1:
            async with gh_client.create_http_client() as c2:
                assert c1 is c2
        await gh_client.close_shared_client()

    asyncio.run(_run())


def test_github_read_comments_and_get_pr_files(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")
    cm_comments, _ = _mock_http_client(
        json_data=[
            {"user": {"login": "alice"}, "created_at": "2026-11-16T10:00:00Z", "body": "First comment"},
            {"user": None, "created_at": "2026-11-16T10:05:00Z", "body": "token=my_secret_val_12345"},
        ],
    )
    with patch("mcp_servers.github.client.create_http_client", return_value=cm_comments):
        out_comments = asyncio.run(github_mcp.github_read_comments("octocat", "hello", 10))
    assert "作者：alice" in out_comments
    assert "First comment" in out_comments
    assert "my_secret_val_12345" not in out_comments

    cm_files, _ = _mock_http_client(
        json_data=[
            {
                "filename": "src/app.py",
                "status": "modified",
                "additions": 5,
                "deletions": 2,
                "patch": "@@ -1,2 +1,5 @@\n+print('hello')",
            },
        ],
    )
    with patch("mcp_servers.github.client.create_http_client", return_value=cm_files):
        out_files = asyncio.run(github_mcp.github_get_pr_files("octocat", "hello", 10))
    assert "檔案：src/app.py [modified] (+5 / -2)" in out_files
    assert "+print('hello')" in out_files


def test_github_list_and_search_issues(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")
    cm_issues, _ = _mock_http_client(
        json_data=[
            {"number": 1, "state": "open", "title": "Real bug", "user": {"login": "dev"}, "labels": [{"name": "bug"}]},
            {"number": 2, "state": "open", "title": "A PR item", "user": {"login": "dev"}, "pull_request": {}},
        ],
    )
    with patch("mcp_servers.github.client.create_http_client", return_value=cm_issues):
        out_issues = asyncio.run(github_mcp.github_list_issues("octocat", "hello", labels="bug"))
    assert "#1 [open] Real bug (dev) [bug]" in out_issues
    assert "#2" not in out_issues

    with patch("mcp_servers.github.client.create_http_client") as mock_factory:
        blocked_search = asyncio.run(github_mcp.github_search_issues("octocat", "hello", "bug repo:evil/other"))
    assert blocked_search.startswith("[ERROR] 拒絕存取")
    mock_factory.assert_not_called()

    cm_search, recorded_search = _mock_http_client(
        json_data={
            "total_count": 1,
            "items": [{"number": 7, "state": "open", "title": "Crash on startup", "user": {"login": "bob"}}],
        },
    )
    with patch("mcp_servers.github.client.create_http_client", return_value=cm_search):
        out_search = asyncio.run(github_mcp.github_search_issues("octocat", "hello", "Crash", state="open"))
    assert "#7 [Issue:open] Crash on startup (bob)" in out_search
    assert recorded_search[0].url.params["q"] == "repo:octocat/hello Crash state:open"


def test_github_get_pr_reviews_and_checks_and_create_issue(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")
    cm_rev, _ = _mock_http_client(
        json_data=[
            {"user": {"login": "reviewer1"}, "state": "APPROVED", "submitted_at": "2026-11-16T12:00:00Z", "body": "OK"},
        ],
    )
    with patch("mcp_servers.github.client.create_http_client", return_value=cm_rev):
        out_rev = asyncio.run(github_mcp.github_get_pr_reviews("octocat", "hello", 3))
    assert "審查者：reviewer1 | 狀態：APPROVED" in out_rev

    with patch("mcp_servers.github.client.create_http_client") as mock_factory:
        bad_ref = asyncio.run(github_mcp.github_get_pr_checks("octocat", "hello", "../evil"))
    assert bad_ref.startswith("[ERROR]")
    mock_factory.assert_not_called()

    cm_checks, _ = _mock_http_client(
        json_data={
            "total_count": 1,
            "check_runs": [
                {"name": "pytest", "status": "completed", "conclusion": "success", "output": {"title": "All passed"}},
            ],
        },
    )
    with patch("mcp_servers.github.client.create_http_client", return_value=cm_checks):
        out_checks = asyncio.run(github_mcp.github_get_pr_checks("octocat", "hello", "feature/branch-1"))
    assert "- pytest: [completed/success] — All passed" in out_checks

    with patch("mcp_servers.github.client.create_http_client") as mock_factory:
        blocked_issue = asyncio.run(
            github_mcp.github_create_issue("octocat", "hello", "Leak", body="api_key=SuperSecretValue12345"),
        )
    assert blocked_issue.startswith("[ERROR] 安全攔截")
    mock_factory.assert_not_called()

    cm_create, recorded_create = _mock_http_client(json_data={"number": 55}, status_code=201)
    with patch("mcp_servers.github.client.create_http_client", return_value=cm_create):
        out_create = asyncio.run(
            github_mcp.github_create_issue("octocat", "hello", "Bug report", body="Steps to repro", labels=["bug"]),
        )
    assert out_create == "成功在 octocat/hello 建立 Issue #55：Bug report"
    assert len(recorded_create) == 1
    assert recorded_create[0].method == "POST"


def test_github_prepare_pr_context_with_local_and_fallback_template(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")
    monkeypatch.chdir(tmp_path)

    req = httpx.Request("GET", "https://api.github.com/repos/octocat/hello")
    resp_repo = httpx.Response(200, json={"default_branch": "main"}, request=req)
    resp_404 = httpx.Response(404, json={"message": "Not Found"}, request=req)
    resp_compare = httpx.Response(
        200,
        json={
            "commits": [{"sha": "1234567890abcdef", "commit": {"message": "feat(cli): enhance input\n\ndetails"}}],
            "files": [
                {
                    "filename": "src/pocket_agent/frontend/cli/input.py",
                    "status": "modified",
                    "additions": 12,
                    "deletions": 3,
                    "patch": "@@ -1,3 +1,12 @@\n+new_line = True",
                },
            ],
        },
        request=req,
    )

    cm, _ = _mock_http_client(responses=[resp_repo, resp_404, resp_compare])

    with patch("mcp_servers.github.client.create_http_client", return_value=cm):
        out_fallback = asyncio.run(
            github_mcp.github_prepare_pr_context("octocat", "hello", head="enhance_cli_input"),
        )

    assert "合併方向：enhance_cli_input -> main" in out_fallback
    assert "預設業界標準範本 (Conventional PR Template)" in out_fallback
    assert "## Summary" in out_fallback
    assert "一律預設使用英文（English）" in out_fallback
    assert "- 1234567 feat(cli): enhance input" in out_fallback
    assert "src/pocket_agent/frontend/cli/input.py [modified] (+12 / -3)" in out_fallback

    gh_dir = tmp_path / ".github"
    gh_dir.mkdir()
    (gh_dir / "pull_request_template.md").write_text("## Custom Repo Template\n- [ ] Check A", encoding="utf-8")

    cm2, _ = _mock_http_client(responses=[resp_compare])

    with patch("mcp_servers.github.client.create_http_client", return_value=cm2):
        out_local = asyncio.run(
            github_mcp.github_prepare_pr_context("octocat", "hello", head="octocat:enhance_cli_input", base="dev"),
        )

    assert "合併方向：octocat:enhance_cli_input -> dev" in out_local
    assert "本機工作區範本 (.github/pull_request_template.md)" in out_local
    assert "## Custom Repo Template" in out_local


def test_github_create_pr_and_422_error_and_no_merge_tool(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")
    assert not hasattr(github_mcp, "github_merge_pr")

    with patch("mcp_servers.github.client.create_http_client") as mock_factory:
        blocked_pr = asyncio.run(
            github_mcp.github_create_pr(
                "octocat",
                "hello",
                title="feat: add pr",
                head="enhance_cli_input",
                base="main",
                body="Leak ghp_1234567890abcdefghijklmnop",
            ),
        )
    assert blocked_pr.startswith("[ERROR] 安全攔截")
    mock_factory.assert_not_called()

    cm_same, _ = _mock_http_client(json_data={})
    with patch("mcp_servers.github.client.create_http_client", return_value=cm_same):
        same_branch = asyncio.run(
            github_mcp.github_create_pr("octocat", "hello", title="feat: x", head="main", base="main"),
        )
    assert "不可相同" in same_branch

    cm_422, _ = _mock_http_client(
        json_data={"message": "Validation Failed", "errors": [{"message": "A pull request already exists"}]},
        status_code=422,
    )
    with patch("mcp_servers.github.client.create_http_client", return_value=cm_422):
        out_422 = asyncio.run(
            github_mcp.github_create_pr(
                "octocat",
                "hello",
                title="feat: test",
                head="enhance_cli_input",
                base="main",
            ),
        )
    assert "HTTP 422" in out_422
    assert "A pull request already exists" in out_422

    req = httpx.Request("POST", "https://api.github.com/repos/octocat/hello/pulls")
    resp_repo = httpx.Response(200, json={"default_branch": "main"}, request=req)
    resp_created = httpx.Response(
        201,
        json={"number": 108, "html_url": "https://github.com/octocat/hello/pull/108"},
        request=req,
    )
    cm_ok, recorded_ok = _mock_http_client(responses=[resp_repo, resp_created])

    with patch("mcp_servers.github.client.create_http_client", return_value=cm_ok):
        out_ok = asyncio.run(
            github_mcp.github_create_pr(
                "octocat",
                "hello",
                title="feat(cli): enhance input",
                head="enhance_cli_input",
                body="## Summary\nReady",
            ),
        )
    assert "Pull Request #108（enhance_cli_input -> main）" in out_ok
    assert "https://github.com/octocat/hello/pull/108" in out_ok
    assert "不會自動執行 Merge" in out_ok
    assert len(recorded_ok) == 2
    post_payload = json.loads(recorded_ok[1].content)
    assert post_payload["base"] == "main"
    assert post_payload["head"] == "enhance_cli_input"


def test_confirmation_bridge_formats_pr_preview_and_requires_explicit_approval():
    from pocket_agent.frontend.cli.confirmation import ConfirmationBridge

    formatted = ConfirmationBridge._format_command(
        (
            "mcp__github__github_create_pr",
            {
                "owner": "octocat",
                "repo": "hello",
                "head": "enhance_cli_input",
                "base": "main",
                "title": "feat(cli): enhance input",
                "body": "## Summary\n- item 1",
                "draft": False,
            },
        ),
    )
    assert "動作：建立 GitHub Pull Request（僅建立 PR 單，絕不自動執行 Merge）" in formatted
    assert "合併分支方向：enhance_cli_input -> main" in formatted
    assert "PR 標題：feat(cli): enhance input" in formatted
    assert "## Summary\n- item 1" in formatted

    dummy_bridge = SimpleNamespace(
        state=SimpleNamespace(accept_edits=True, session_approved_commands={formatted.strip()}),
    )
    assert ConfirmationBridge._is_auto_allowed(dummy_bridge, formatted, True) is False

    renderer = CliRenderer(Console())
    raw_prep = '{"owner": "octocat", "repo": "hello", "head": "enhance_cli_input", "base": "main"}'
    expected_target = "octocat/hello (enhance_cli_input -> main)"
    assert renderer._summarise_args("github_prepare_pr_context", raw_prep) == expected_target


@asynccontextmanager
async def _transport_client_cm(handler):
    client = gh_client.build_async_client()
    client._transport = httpx.MockTransport(handler)
    client._mounts = {}
    try:
        yield client
    finally:
        await client.aclose()


class _CountingChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunk: bytes, total_chunks: int) -> None:
        self._chunk = chunk
        self._total_chunks = total_chunks
        self.chunks_yielded = 0
        self.closed = False

    async def __aiter__(self):
        for _ in range(self._total_chunks):
            self.chunks_yielded += 1
            yield self._chunk

    async def aclose(self) -> None:
        self.closed = True


def _make_dummy_pem(body_len: int, marker: str) -> str:
    header = "-----BEGIN RSA PRIVATE KEY-----\n"
    footer = "\n-----END RSA PRIVATE KEY-----"
    target_body = max(len(marker) + 16, body_len - len(header) - len(footer))
    raw_body = marker + ("A" * (target_body - len(marker)))
    lines = [raw_body[i : i + 64] for i in range(0, len(raw_body), 64)]
    return header + "\n".join(lines) + footer


@pytest.mark.parametrize(
    "redirect_loc",
    [
        "../secret-repo/pulls",
        "/repos/myorg/allowed-repo/../../other-org/secret-repo/pulls",
        "https://api.github.com/user",
        "https://api.github.com/repos/myorg/allowed-repo%2F..%2F..%2Fother/secret",
        "https://user:pass@api.github.com/repos/myorg/allowed-repo/pulls",
        "https://api.github.com:8443/repos/myorg/allowed-repo/pulls",
        "//evil.example.com/repos/myorg/allowed-repo/pulls",
    ],
)
def test_cr001_redirect_blocks_relative_traversal_and_unknown_path(monkeypatch, tmp_path, redirect_loc):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")
    monkeypatch.setenv("GITHUB_ALLOWED_REPOS", "myorg/allowed-repo")

    seen_urls: list[str] = []

    def get_handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if len(seen_urls) == 1:
            return httpx.Response(302, headers={"location": redirect_loc})
        return httpx.Response(200, json=[])

    with patch("mcp_servers.github.client.create_http_client", lambda: _transport_client_cm(get_handler)):
        out = asyncio.run(github_mcp.github_list_prs("myorg", "allowed-repo"))

    assert out.startswith("[ERROR] 拒絕存取")
    assert len(seen_urls) == 1
    audit_text = (tmp_path / "agent.log").read_text(encoding="utf-8")
    assert "status=BLOCKED" in audit_text


def test_cr001_post_redirect_is_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")
    monkeypatch.setenv("GITHUB_ALLOWED_REPOS", "myorg/allowed-repo")

    post_urls: list[str] = []

    def post_handler(request: httpx.Request) -> httpx.Response:
        post_urls.append(str(request.url))
        if len(post_urls) == 1:
            return httpx.Response(
                307,
                headers={"location": "https://api.github.com/repos/myorg/allowed-repo/issues/99/comments"},
            )
        return httpx.Response(201, json={"id": 99})

    with patch("mcp_servers.github.client.create_http_client", lambda: _transport_client_cm(post_handler)):
        post_out = asyncio.run(github_mcp.github_create_comment("myorg", "allowed-repo", 12, "hi"))

    assert post_out.startswith("[ERROR] 拒絕存取")
    assert len(post_urls) == 1
    audit_text = (tmp_path / "agent.log").read_text(encoding="utf-8")
    assert "status=BLOCKED" in audit_text


def test_cr001_get_follows_valid_allowed_redirect_and_respects_max_redirects(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")
    monkeypatch.setenv("GITHUB_ALLOWED_REPOS", "myorg/allowed-repo")

    ok_urls: list[str] = []

    def ok_handler(request: httpx.Request) -> httpx.Response:
        ok_urls.append(str(request.url))
        if len(ok_urls) == 1:
            return httpx.Response(
                302,
                headers={"location": "https://api.github.com/repos/MyOrg/Allowed-Repo/pulls?page=2"},
            )
        return httpx.Response(
            200,
            json=[{"number": 42, "title": "Redirected PR", "user": {"login": "octocat"}}],
        )

    with patch("mcp_servers.github.client.create_http_client", lambda: _transport_client_cm(ok_handler)):
        out_ok = asyncio.run(github_mcp.github_list_prs("myorg", "allowed-repo"))

    assert "#42 Redirected PR (octocat)" in out_ok
    assert len(ok_urls) == 2

    loop_urls: list[str] = []

    def loop_handler(request: httpx.Request) -> httpx.Response:
        loop_urls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://api.github.com/repos/myorg/allowed-repo/pulls"},
        )

    with patch("mcp_servers.github.client.create_http_client", lambda: _transport_client_cm(loop_handler)):
        out_loop = asyncio.run(github_mcp.github_list_prs("myorg", "allowed-repo"))

    assert out_loop.startswith("[ERROR]")
    assert len(loop_urls) <= gh_client.MAX_REDIRECTS + 1


def test_cr002_diff_redacts_secrets_before_truncation_in_pr_files_and_context(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")
    monkeypatch.chdir(tmp_path)

    file_patch = ("+x" * 30 + "\n") * 45 + _make_dummy_pem(1700, "SENSITIVE_PEM_FILE_CHUNK_12345")

    def files_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "filename": "src/secrets.py",
                    "status": "modified",
                    "additions": 50,
                    "deletions": 0,
                    "patch": file_patch,
                },
            ],
        )

    with patch("mcp_servers.github.client.create_http_client", lambda: _transport_client_cm(files_handler)):
        out_files = asyncio.run(github_mcp.github_get_pr_files("octocat", "hello", 10))

    assert "SENSITIVE_PEM_FILE_CHUNK_12345" not in out_files
    assert "[REDACTED_PRIVATE_KEY]" in out_files

    ctx_patch = _make_dummy_pem(1700, "SENSITIVE_PEM_CTX_CHUNK_67890")

    def ctx_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/contents/.github/pull_request_template.md"):
            return httpx.Response(404, json={"message": "Not Found"})
        if "/compare/" in path:
            return httpx.Response(
                200,
                json={
                    "commits": [{"sha": "abcdef1234567", "commit": {"message": "fix: clean"}}],
                    "files": [
                        {
                            "filename": "key.pem",
                            "status": "added",
                            "additions": 30,
                            "deletions": 0,
                            "patch": ctx_patch,
                        },
                    ],
                },
            )
        return httpx.Response(200, json={"default_branch": "main"})

    with patch("mcp_servers.github.client.create_http_client", lambda: _transport_client_cm(ctx_handler)):
        out_ctx = asyncio.run(github_mcp.github_prepare_pr_context("octocat", "hello", head="feat/key"))

    assert "SENSITIVE_PEM_CTX_CHUNK_67890" not in out_ctx
    assert "[REDACTED_PRIVATE_KEY]" in out_ctx


def test_cr002_sanitize_and_truncate_handles_long_pem_and_preslice_shrinkage(monkeypatch):
    long_pem = _make_dummy_pem(3400, "SENSITIVE_4096_PEM_99999")
    raw_boundary = ("A" * (gh_security.MAX_OUTPUT_CHARS - 100)) + "\n" + long_pem + "\n" + ("B" * 500)
    out_b, trunc_b = gh_security.GITHUB_REDACTION.sanitize_and_truncate(raw_boundary, "test_pat_value_999", "hint")
    assert trunc_b is True
    assert "SENSITIVE_4096_PEM_99999" not in out_b
    assert "[REDACTED_PRIVATE_KEY]" in out_b

    head_pems = _make_dummy_pem(3400, "PEM_ONE") + "\n" + _make_dummy_pem(3400, "PEM_TWO") + "\n"
    pad_len = (gh_security.MAX_OUTPUT_CHARS + 2048 - 14) - len(head_pems) - 1
    raw_shrink = head_pems + ("C" * pad_len) + " ghp_1234567890abcdefghij " + ("Z" * 6000)
    out_s, trunc_s = gh_security.GITHUB_REDACTION.sanitize_and_truncate(raw_shrink, "test_pat_value_999", "hint")
    assert trunc_s is True
    assert "ghp_1234567890" not in out_s
    assert "[REDACTED_TOKEN]" in out_s

    monkeypatch.setattr(gh_security, "MAX_RESPONSE_BYTES", 35000)
    pad_bytes = (35000 - 14) - len(head_pems) - 1
    raw_over = head_pems + ("D" * pad_bytes) + " ghp_9876543210abcdefghij " + ("W" * 3000)
    out_o, trunc_o = gh_security.GITHUB_REDACTION.sanitize_and_truncate(raw_over, "test_pat_value_999", "hint")
    assert trunc_o is True
    assert "[REDACTED_PRIVATE_KEY]" in out_o
    assert "ghp_9876543210" not in out_o


def test_cr003_streaming_stops_and_closes_when_200_exceeds_max_bytes(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")
    monkeypatch.setattr(gh_security, "MAX_RESPONSE_BYTES", 1024)
    stream = _CountingChunkStream(b"a" * 256, total_chunks=20)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    with patch("mcp_servers.github.client.create_http_client", lambda: _transport_client_cm(handler)):
        out = asyncio.run(github_mcp.github_list_prs("octocat", "hello"))

    assert out.startswith("[ERROR]")
    assert "回應內容過大" in out
    assert stream.closed is True
    assert stream.chunks_yielded < 20


def test_cr003_streaming_caps_oversized_error_body_and_preserves_http_status(monkeypatch):
    monkeypatch.setattr(gh_security, "MAX_RESPONSE_BYTES", 1024)

    s401 = _CountingChunkStream(b"e" * 256, total_chunks=20)

    def handler_401(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, stream=s401)

    with patch("mcp_servers.github.client.create_http_client", lambda: _transport_client_cm(handler_401)):
        out_401 = asyncio.run(github_mcp.github_list_prs("octocat", "hello", github_token="expired_pat"))

    assert "[CONN_AUTH]" in out_401
    assert gh_client.CREDS_CACHE.get("GITHUB_TOKEN") == ""
    assert s401.closed is True
    assert s401.chunks_yielded < 20

    s403 = _CountingChunkStream(b"r" * 256, total_chunks=20)

    def handler_403(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1799999999"},
            stream=s403,
        )

    with patch("mcp_servers.github.client.create_http_client", lambda: _transport_client_cm(handler_403)):
        out_403 = asyncio.run(github_mcp.github_list_prs("octocat", "hello", github_token="valid_pat"))

    assert "Rate Limit Exceeded" in out_403
    assert "1799999999" in out_403
    assert s403.closed is True
    assert s403.chunks_yielded < 20

    gz_payload = gzip.compress(
        json.dumps({"message": "Validation Failed", "errors": [{"message": "gzip error detail"}]}).encode("utf-8"),
    )
    s422_gz = _CountingChunkStream(gz_payload, total_chunks=1)

    def handler_422_gz(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, headers={"content-encoding": "gzip"}, stream=s422_gz)

    with patch("mcp_servers.github.client.create_http_client", lambda: _transport_client_cm(handler_422_gz)):
        out_gz = asyncio.run(github_mcp.github_list_prs("octocat", "hello", github_token="valid_pat"))

    assert "HTTP 422" in out_gz
    assert "gzip error detail" in out_gz
    assert s422_gz.closed is True


@pytest.mark.parametrize(
    "secret_body",
    [
        '{"password": "MySecretPassword123"}',
        "{'api_key': 'MySecretKey12345'}",
        '{"token": "my_opaque_secret_12345"}',
        "token=my_opaque_secret_12345",
        'token: "my_opaque_secret_12345"',
        "  token: my_opaque_secret_12345",
        "access_token=my_opaque_secret_12345",
    ],
)
def test_cr004_outbound_blocks_quoted_keys_and_structured_token(monkeypatch, secret_body):
    monkeypatch.setenv("GITHUB_TOKEN", "current_active_pat_secret_123")
    with patch("mcp_servers.github.client.create_http_client") as mock_factory:
        out = asyncio.run(github_mcp.github_create_comment("octocat", "hello", 12, secret_body))
    assert out.startswith("[ERROR] 安全攔截")
    mock_factory.assert_not_called()


def test_cr004_outbound_allows_prose_and_placeholders_and_inbound_redacts_quoted_keys():
    safe_prose = "Note about OAuth token: authentication fails on timeout."
    assert gh_security.GITHUB_REDACTION.check_outbound_content(safe_prose) is None

    safe_json = '{"token": "changeme", "password": "example"}'
    assert gh_security.GITHUB_REDACTION.check_outbound_content(safe_json) is None

    safe_tpl = "token=${GITHUB_TOKEN}\ntoken=<placeholder>\ntoken=........"
    assert gh_security.GITHUB_REDACTION.check_outbound_content(safe_tpl) is None

    inbound_json = (
        '{"password": "MySecretPassword123", "api_key": "MySecretKey12345", "access_token": "MyAccessTok12345"}'
    )
    redacted = gh_security.GITHUB_REDACTION.redact(inbound_json)
    assert "MySecretPassword123" not in redacted
    assert "MySecretKey12345" not in redacted
    assert "MyAccessTok12345" not in redacted
    assert "[REDACTED]" in redacted
    assert "]]" not in redacted
    assert gh_security.GITHUB_REDACTION.redact(redacted) == redacted

    assert gh_security.GITHUB_REDACTION.redact("token=ghp_1234567890abcdefghijklmnop") == "token=[REDACTED_TOKEN]"
    assert gh_security.GITHUB_REDACTION.redact("token=[REDACTED_TOKEN]") == "token=[REDACTED_TOKEN]"
    non_url_repeat = ".a" * 5000
    assert gh_security.GITHUB_REDACTION.redact(non_url_repeat) == non_url_repeat


def test_cr005_zero_hit_search_does_not_echo_query(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test_pat_value_999")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total_count": 0, "items": []})

    with patch("mcp_servers.github.client.create_http_client", lambda: _transport_client_cm(handler)):
        out = asyncio.run(
            github_mcp.github_search_issues("octocat", "hello", "SENSITIVE_QUERY_MARKER_" + ("Q" * 500)),
        )

    assert "SENSITIVE_QUERY_MARKER_" not in out
    assert len(out) < 300


def test_cr005_errors_and_audit_redact_active_token_sanitize_fields_and_bound_length(tmp_path):
    active_tok = "opaque_custom_secret_tok_987654321"
    log_path = tmp_path / "agent.log"

    def handler_422(_request: httpx.Request) -> httpx.Response:
        errors = [{"field": f"field_{active_tok}_{i}", "code": f"code_{active_tok}_{i}"} for i in range(30)]
        return httpx.Response(
            422,
            json={"message": f"Validation failed for {active_tok}", "errors": errors},
        )

    with patch("mcp_servers.github.client.create_http_client", lambda: _transport_client_cm(handler_422)):
        out_422 = asyncio.run(github_mcp.github_list_prs("octocat", "hello", github_token=active_tok))

    assert out_422.startswith("[ERROR]")
    assert active_tok not in out_422
    assert len(out_422) <= 2000
    assert active_tok not in log_path.read_text(encoding="utf-8")

    def handler_403(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": f"leak_{active_tok}"},
        )

    with patch("mcp_servers.github.client.create_http_client", lambda: _transport_client_cm(handler_403)):
        out_403 = asyncio.run(github_mcp.github_list_prs("octocat", "hello", github_token=active_tok))

    assert "Rate Limit Exceeded" in out_403
    assert active_tok not in out_403
    assert "leak_" not in out_403

    def handler_err(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"boom {active_tok} " + ("X" * 5000))

    with patch("mcp_servers.github.client.create_http_client", lambda: _transport_client_cm(handler_err)):
        out_err = asyncio.run(github_mcp.github_list_prs("octocat", "hello", github_token=active_tok))

    assert out_err.startswith("[ERROR]")
    assert active_tok not in out_err
    assert len(out_err) <= 2000
    assert active_tok not in log_path.read_text(encoding="utf-8")


def test_cr006_clear_creds_cache_preserves_shared_client_and_clears_cookies():
    async def _run() -> None:
        c1 = None
        c3 = None
        try:
            async with gh_client.create_http_client() as client1:
                c1 = client1
                c1.cookies.set("gh_sess", "secret", domain="api.github.com")
                gh_client.clear_creds_cache()
                assert gh_client._SHARED_CLIENT is c1
                assert not c1.is_closed
                assert len(c1.cookies) == 0

            async with gh_client.create_http_client() as c2:
                assert c2 is c1

            assert hasattr(gh_client, "close_shared_client")
            await gh_client.close_shared_client()
            assert c1.is_closed is True
            assert gh_client._SHARED_CLIENT is None

            async with gh_client.server_lifespan(github_mcp.mcp):
                async with gh_client.create_http_client() as client3:
                    c3 = client3
                    assert not c3.is_closed
            assert c3 is not None and c3.is_closed is True
            assert gh_client._SHARED_CLIENT is None
        finally:
            for client in (c1, c3):
                if client is not None and not client.is_closed:
                    await client.aclose()
            gh_client._SHARED_CLIENT = None
            gh_client._SHARED_CLIENT_LOOP = None

    asyncio.run(_run())

    async def _run_cross_loop():
        async with gh_client.create_http_client() as client_loop:
            return client_loop

    loop1_client = asyncio.run(_run_cross_loop())
    try:
        assert not loop1_client.is_closed
        loop2_client = asyncio.run(_run_cross_loop())
        assert loop2_client is not loop1_client
        assert loop1_client.is_closed is True
        assert not loop2_client.is_closed
    finally:
        asyncio.run(gh_client.close_shared_client())
        gh_client._SHARED_CLIENT = None
        gh_client._SHARED_CLIENT_LOOP = None


def test_github_resolve_token_rejects_and_resolves_placeholders(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "USE_ENCRYPTED")
    with pytest.raises(ConnAuthError, match="USE_ENCRYPTED"):
        gh_client.resolve_token()

    monkeypatch.setenv("GITHUB_TOKEN", "USE_KEYRING")
    with patch("mcp_servers.github.client._try_load_standalone_keyring_token", return_value=""):
        with pytest.raises(ConnAuthError, match="USE_KEYRING"):
            gh_client.resolve_token()

    with patch("mcp_servers.github.client._try_load_standalone_keyring_token", return_value="standalone_pat_123"):
        assert gh_client.resolve_token() == "standalone_pat_123"
        assert gh_client.CREDS_CACHE.get_secret("GITHUB_TOKEN") == "standalone_pat_123"

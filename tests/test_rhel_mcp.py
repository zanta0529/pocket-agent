import asyncio
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from mcp_servers import rhel_mcp
from pocket_agent.extensions.mcp import MCPManager


def test_missing_rhel_host_returns_error(monkeypatch):
    monkeypatch.delenv("RHEL_HOST", raising=False)
    out = rhel_mcp.rhel_system_overview()
    assert out.startswith("[ERROR]")
    assert "RHEL_HOST" in out


def test_build_ssh_args_and_execution(monkeypatch):
    monkeypatch.setenv("RHEL_HOST", "rhel9-prod.internal")
    monkeypatch.setenv("RHEL_SSH_USER", "mcp-inspector")
    monkeypatch.setenv("RHEL_SSH_PORT", "2222")
    monkeypatch.setenv("RHEL_CONNECT_TIMEOUT", "12")
    monkeypatch.setenv("RHEL_SSH_KEY_PATH", "/path/to/id_ed25519")

    args, timeout = rhel_mcp._build_ssh_args("uptime")
    assert timeout == 12
    assert "BatchMode=yes" in args
    assert "ConnectTimeout=12" in args
    assert "StrictHostKeyChecking=yes" in args
    assert "-p" in args and "2222" in args
    assert "-i" in args and "/path/to/id_ed25519" in args
    assert args[-3:] == ["--", "mcp-inspector@rhel9-prod.internal", "uptime"]


@pytest.mark.parametrize(
    "bad_service",
    [
        "nginx; rm -rf /",
        "sshd && cat /etc/shadow",
        "$(reboot)",
        "-oProxyCommand=evil",
    ],
)
def test_service_status_blocks_command_injection(monkeypatch, bad_service):
    monkeypatch.setenv("RHEL_HOST", "192.168.1.10")
    with patch("subprocess.run") as mock_run:
        res = rhel_mcp.rhel_service_status(service_name=bad_service)
        assert res.startswith("[ERROR]")
        mock_run.assert_not_called()


def test_query_journal_validation_and_clamping(monkeypatch):
    monkeypatch.setenv("RHEL_HOST", "192.168.1.10")

    with patch("subprocess.run") as mock_run:
        res_bad_prio = rhel_mcp.rhel_query_journal(unit="sshd", priority="invalid_prio")
        assert res_bad_prio.startswith("[ERROR]")
        res_bad_since = rhel_mcp.rhel_query_journal(unit="sshd", since="1 hour ago; id")
        assert res_bad_since.startswith("[ERROR]")
        mock_run.assert_not_called()

    fake_proc = SimpleNamespace(returncode=0, stdout="journal line 1", stderr="")
    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        out = rhel_mcp.rhel_query_journal(unit="sshd.service", priority="warning", since="2 hours ago", lines=999)
        assert out == "journal line 1"
        called_args = mock_run.call_args.args[0]
        remote_cmd = called_args[-1]
        assert "--no-pager" in remote_cmd
        assert "-u sshd.service" in remote_cmd
        assert "-p warning" in remote_cmd
        assert "-n 200" in remote_cmd


@pytest.mark.parametrize(
    "blocked_path",
    [
        "etc/nginx/nginx.conf",
        "/etc/../root/.bashrc",
        "/etc/shadow",
        "/etc/gshadow",
        "/etc/sudoers",
        "/etc/pki/tls/private/server.key",
        "/etc/myapp/.env",
        "/etc/kubernetes/admin.conf",
        "/home/admin/config.yaml",
    ],
)
def test_read_config_blocks_sensitive_and_unallowed_paths(monkeypatch, blocked_path):
    monkeypatch.setenv("RHEL_HOST", "192.168.1.10")
    with patch("subprocess.run") as mock_run:
        res = rhel_mcp.rhel_read_config(file_path=blocked_path)
        assert res.startswith("[ERROR]")
        mock_run.assert_not_called()


def test_read_config_allows_safe_etc_path_and_redacts_secrets(monkeypatch):
    monkeypatch.setenv("RHEL_HOST", "192.168.1.10")
    raw_config = (
        "server_name localhost;\n"
        "password = super_secret_db_pass\n"
        'api_key: "abcdef123456"\n'
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----\n"
    )
    fake_proc = SimpleNamespace(returncode=0, stdout=raw_config, stderr="")
    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        out = rhel_mcp.rhel_read_config("/etc/nginx/nginx.conf", max_lines=50)
        assert "server_name localhost;" in out
        assert "super_secret_db_pass" not in out
        assert "abcdef123456" not in out
        assert "MIIEpAIBAAKCAQEA" not in out
        assert "[REDACTED]" in out
        assert "[REDACTED PRIVATE KEY]" in out
        mock_run.assert_called_once()


def test_remote_timeout_and_truncation(monkeypatch):
    monkeypatch.setenv("RHEL_HOST", "192.168.1.10")
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=15)):
        res = rhel_mcp.rhel_storage_and_fs()
        assert "逾時" in res

    long_stdout = "A" * (rhel_mcp.MAX_OUTPUT_CHARS + 500)
    fake_proc = SimpleNamespace(returncode=0, stdout=long_stdout, stderr="")
    with patch("subprocess.run", return_value=fake_proc):
        res2 = rhel_mcp.rhel_network_status()
        assert "輸出過長已截斷" in res2
        assert len(res2) < len(long_stdout)


def test_mcp_manager_safe_read_tools_registration():
    manager = MCPManager()
    fake_tools = [
        SimpleNamespace(
            name="rhel_system_overview",
            description="overview",
            inputSchema={"type": "object", "properties": {}},
        ),
        SimpleNamespace(
            name="rhel_dangerous_op",
            description="danger",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]
    fake_session = SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=fake_tools)),
    )
    asyncio.run(
        manager._register_tools(
            "rhel_ops",
            fake_session,
            inject={},
            safe_read_tools=["rhel_system_overview"],
        )
    )
    schemas = {s["name"]: s for s in manager.openai_tools()}
    assert schemas["rhel_system_overview"].get("_is_safe_read") is True
    assert "_is_safe_read" not in schemas["rhel_dangerous_op"]

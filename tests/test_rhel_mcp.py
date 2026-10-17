import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_servers import rhel_mcp
from pocket_agent.extensions.mcp import MCPManager


@pytest.fixture(autouse=True)
def _clean_rhel_env_and_cache(monkeypatch):
    rhel_mcp._RUNTIME_CREDS.clear()
    for key in ("RHEL_HOST", "RHEL_SSH_PORT", "RHEL_SSH_USER", "RHEL_SSH_PASSWORD", "RHEL_SSH_KEY_PATH"):
        monkeypatch.delenv(key, raising=False)


def _set_full_creds(monkeypatch):
    monkeypatch.setenv("RHEL_HOST", "192.168.1.10")
    monkeypatch.setenv("RHEL_SSH_PORT", "22")
    monkeypatch.setenv("RHEL_SSH_USER", "mcp-inspector")
    monkeypatch.setenv("RHEL_SSH_PASSWORD", "SecretPass!123")


def test_missing_params_prompts_tty_and_rejects_empty_password(monkeypatch):
    monkeypatch.setenv("RHEL_HOST", "192.168.1.10")
    monkeypatch.setenv("RHEL_SSH_PORT", "22")
    monkeypatch.setenv("RHEL_SSH_USER", "admin")
    with patch("mcp_servers.rhel_mcp._tty_prompt", return_value="") as mock_prompt:
        out = rhel_mcp.rhel_system_overview()
        assert out.startswith("[ERROR]")
        assert "RHEL_SSH_PASSWORD" in out
        mock_prompt.assert_called_once()


def test_partial_config_prompts_only_missing_fields_and_caches(monkeypatch):
    monkeypatch.setenv("RHEL_HOST", "172.17.3.57")
    monkeypatch.setenv("RHEL_SSH_PORT", "")

    prompts = iter(["2222", "opsuser", "MyPassword99"])
    with patch("mcp_servers.rhel_mcp._tty_prompt", side_effect=lambda *a, **kw: next(prompts)) as mock_prompt:
        args, pw, timeout = rhel_mcp._build_ssh_args("uptime")
        assert mock_prompt.call_count == 3
        assert pw == "MyPassword99"
        assert timeout == 10
        assert "BatchMode=no" in args
        assert "PubkeyAuthentication=no" in args
        assert "GSSAPIAuthentication=no" in args
        assert "IdentitiesOnly=yes" in args
        assert "IdentityAgent=none" in args
        assert "-i" not in args
        assert "-p" in args and "2222" in args
        assert args[-3:] == ["--", "opsuser@172.17.3.57", "uptime"]

        # 第二次呼叫應直接使用記憶體快取，不再觸發 _tty_prompt
        args2, pw2, _ = rhel_mcp._build_ssh_args("df -h")
        assert mock_prompt.call_count == 3
        assert pw2 == "MyPassword99"
        assert args2[-2] == "opsuser@172.17.3.57"


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
    _set_full_creds(monkeypatch)
    with patch("subprocess.run") as mock_run:
        res = rhel_mcp.rhel_service_status(service_name=bad_service)
        assert res.startswith("[ERROR]")
        mock_run.assert_not_called()


def test_query_journal_validation_and_clamping(monkeypatch):
    _set_full_creds(monkeypatch)

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
        "/usr/bin/bash",
    ],
)
def test_read_config_blocks_sensitive_and_unallowed_paths(monkeypatch, blocked_path):
    _set_full_creds(monkeypatch)
    with patch("subprocess.run") as mock_run:
        res = rhel_mcp.rhel_read_config(file_path=blocked_path)
        assert res.startswith("[ERROR]")
        mock_run.assert_not_called()


def test_read_config_allows_safe_etc_path_and_redacts_secrets(monkeypatch):
    _set_full_creds(monkeypatch)
    raw_config = (
        "server_name localhost;\n"
        "password = super_secret_db_pass\n"
        'api_key: "abcdef123456"\n'
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----\n"
    )
    fake_proc = SimpleNamespace(returncode=0, stdout=raw_config, stderr="")
    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        out = rhel_mcp.rhel_read_config("/usr/share/nginx/nginx.conf", max_lines=50)
        assert "server_name localhost;" in out
        assert "super_secret_db_pass" not in out
        assert "abcdef123456" not in out
        assert "MIIEpAIBAAKCAQEA" not in out
        assert "[REDACTED]" in out
        assert "[REDACTED PRIVATE KEY]" in out
        mock_run.assert_called_once()
        remote_cmd = mock_run.call_args.args[0][-1]
        assert "/srv/*" in remote_cmd
        assert "/usr/share/*" in remote_cmd


def test_remote_timeout_and_truncation(monkeypatch):
    _set_full_creds(monkeypatch)
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=15)):
        res = rhel_mcp.rhel_storage_and_fs()
        assert "逾時" in res

    long_stdout = "A" * (rhel_mcp.MAX_OUTPUT_CHARS + 500)
    fake_proc = SimpleNamespace(returncode=0, stdout=long_stdout, stderr="")
    with patch("subprocess.run", return_value=fake_proc):
        res2 = rhel_mcp.rhel_network_status()
        assert "輸出過長已截斷" in res2
        assert len(res2) < len(long_stdout)


def test_mcp_manager_strips_ssh_params_and_prompts_only_missing():
    manager = MCPManager()
    manager._server_envs["rhel_ops"] = {
        "RHEL_HOST": "172.17.3.57",
        "RHEL_SSH_PORT": "",
    }
    fake_tools = [
        SimpleNamespace(
            name="rhel_system_overview",
            description="overview",
            inputSchema={
                "type": "object",
                "properties": {
                    "ssh_host": {"type": "string"},
                    "ssh_port": {"type": "string"},
                    "ssh_user": {"type": "string"},
                    "ssh_password": {"type": "string"},
                },
            },
        ),
    ]
    fake_session = SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=fake_tools)),
        call_tool=AsyncMock(return_value=MagicMock(content=[MagicMock(text="OS OK")])),
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
    assert schemas["rhel_system_overview"]["parameters"]["properties"] == {}

    prompt_calls = []

    def fake_prompt(server_name, missing_keys):
        prompt_calls.append((server_name, missing_keys))
        return {
            "RHEL_SSH_PORT": "22",
            "RHEL_SSH_USER": "root",
            "RHEL_SSH_PASSWORD": "pw123",
        }

    manager.on_prompt_conn = fake_prompt
    asyncio.run(manager.call("rhel_system_overview", {}))

    # RHEL_HOST 已在設定檔提供，僅應詢問缺少的 PORT、USER、PASSWORD
    assert prompt_calls == [("rhel_ops", ["RHEL_SSH_PORT", "RHEL_SSH_USER", "RHEL_SSH_PASSWORD"])]
    _, sent_args = fake_session.call_tool.call_args.args
    assert sent_args == {
        "ssh_host": "172.17.3.57",
        "ssh_port": "22",
        "ssh_user": "root",
        "ssh_password": "pw123",
    }

    # 第二次呼叫直接沿用記憶體快取，不再呼叫 on_prompt_conn
    asyncio.run(manager.call("rhel_system_overview", {}))
    assert len(prompt_calls) == 1

    # 一般工具錯誤（如檔案不存在或查詢條件錯誤）不應清除記憶體帳密快取
    fake_session.call_tool = AsyncMock(return_value=MagicMock(content=[MagicMock(text="[ERROR] 檔案不存在或無法存取")]))
    asyncio.run(manager.call("rhel_system_overview", {}))
    fake_session.call_tool = AsyncMock(return_value=MagicMock(content=[MagicMock(text="OS OK")]))
    asyncio.run(manager.call("rhel_system_overview", {}))
    assert len(prompt_calls) == 1

    # 唯有發生連線或認證失敗 ([CONN_AUTH] / exit 255) 時，才會清除快取並於下次重新詢問
    auth_err_msg = "[ERROR] [CONN_AUTH] 遠端連線或認證失敗 (exit 255): Permission denied"
    fake_session.call_tool = AsyncMock(return_value=MagicMock(content=[MagicMock(text=auth_err_msg)]))
    asyncio.run(manager.call("rhel_system_overview", {}))
    fake_session.call_tool = AsyncMock(return_value=MagicMock(content=[MagicMock(text="OS OK")]))
    asyncio.run(manager.call("rhel_system_overview", {}))
    assert len(prompt_calls) == 2


def test_resolve_stdio_params_from_external_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = MCPManager()
    params = manager._resolve_stdio_params(
        {
            "command": "python",
            "args": ["mcp_servers/rhel_mcp.py"],
            "env": {"RHEL_HOST": "192.168.1.100", "RHEL_SSH_PORT": 22},
        }
    )
    assert params.command == sys.executable
    assert Path(params.args[0]).is_absolute()
    assert Path(params.args[0]).name == "rhel_mcp.py"
    assert params.env["RHEL_SSH_PORT"] == "22"
    assert params.env["PYTHONUTF8"] == "1"

    with patch("pocket_agent.extensions.mcp.is_frozen", return_value=True):
        frozen_params = manager._resolve_stdio_params(
            {
                "command": "python",
                "args": ["mcp_servers/rhel_mcp.py"],
            }
        )
        assert frozen_params.command == sys.executable
        assert frozen_params.args[0] == "--internal-mcp-server"
        assert Path(frozen_params.args[1]).name == "rhel_mcp.py"

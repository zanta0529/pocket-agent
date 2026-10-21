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
    for key in (
        "RHEL_HOST",
        "RHEL_SSH_PORT",
        "RHEL_SSH_USER",
        "RHEL_SSH_PASSWORD",
        "RHEL_SSH_KEY_PATH",
        "RHEL_SSH_KEY_PASSPHRASE",
    ):
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
    with patch("mcp_servers.rhel_mcp._capture_ssh") as mock_run:
        res = rhel_mcp.rhel_service_status(service_name=bad_service)
        assert res.startswith("[ERROR]")
        mock_run.assert_not_called()


def test_query_journal_validation_and_clamping(monkeypatch):
    _set_full_creds(monkeypatch)

    with patch("mcp_servers.rhel_mcp._capture_ssh") as mock_run:
        res_bad_prio = rhel_mcp.rhel_query_journal(unit="sshd", priority="invalid_prio")
        assert res_bad_prio.startswith("[ERROR]")
        res_bad_since = rhel_mcp.rhel_query_journal(unit="sshd", since="1 hour ago; id")
        assert res_bad_since.startswith("[ERROR]")
        mock_run.assert_not_called()

    fake_proc = SimpleNamespace(returncode=0, stdout="journal line 1", stderr="")
    with patch("mcp_servers.rhel_mcp._capture_ssh", return_value=fake_proc) as mock_run:
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
        "/etc/security/opasswd",
        "/etc/pki/tls/private/server.key",
        "/etc/myapp/.env",
        "/etc/kubernetes/admin.conf",
        "/home/admin/config.yaml",
        "/usr/bin/bash",
    ],
)
def test_read_config_blocks_sensitive_and_unallowed_paths(monkeypatch, blocked_path):
    _set_full_creds(monkeypatch)
    with patch("mcp_servers.rhel_mcp._capture_ssh") as mock_run:
        res = rhel_mcp.rhel_read_config(file_path=blocked_path)
        assert res.startswith("[ERROR]")
        mock_run.assert_not_called()


def test_read_config_allows_safe_etc_path_and_redacts_secrets(monkeypatch):
    _set_full_creds(monkeypatch)
    raw_config = (
        "server_name localhost;\n"
        "password = super_secret_db_pass\n"
        'api_key: "abcdef123456"\n'
        "private_key = hidden_private_key_content\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----\n"
    )
    fake_proc = SimpleNamespace(returncode=0, stdout=raw_config, stderr="")
    with patch("mcp_servers.rhel_mcp._capture_ssh", return_value=fake_proc) as mock_run:
        out = rhel_mcp.rhel_read_config("/usr/share/nginx/nginx.conf", max_lines=50)
        assert "server_name localhost;" in out
        assert "super_secret_db_pass" not in out
        assert "abcdef123456" not in out
        assert "hidden_private_key_content" not in out
        assert "MIIEpAIBAAKCAQEA" not in out
        assert "[REDACTED]" in out
        assert "[REDACTED PRIVATE KEY]" in out
        mock_run.assert_called_once()
        remote_cmd = mock_run.call_args.args[0][-1]
        assert "/srv/*" in remote_cmd
        assert "/usr/share/*" in remote_cmd


def test_remote_timeout_and_truncation(monkeypatch):
    _set_full_creds(monkeypatch)
    with patch("mcp_servers.rhel_mcp._capture_ssh", side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=15)):
        res = rhel_mcp.rhel_storage_and_fs()
        assert "逾時" in res

    long_stdout = "A" * (rhel_mcp.MAX_OUTPUT_CHARS + 500)
    fake_proc = SimpleNamespace(returncode=0, stdout=long_stdout, stderr="")
    with patch("mcp_servers.rhel_mcp._capture_ssh", return_value=fake_proc):
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


def test_new_paths_allowed_and_sensitive_logs_blocked(monkeypatch):
    _set_full_creds(monkeypatch)
    fake_proc = SimpleNamespace(returncode=0, stdout="OK", stderr="")

    # 允許 /opt, /srv, /var/log/nginx
    with patch("mcp_servers.rhel_mcp._capture_ssh", return_value=fake_proc) as mock_run:
        out = rhel_mcp.rhel_read_config("/opt/myapp/app.conf")
        assert out == "OK"
        out2 = rhel_mcp.rhel_read_config("/srv/web/config.ini")
        assert out2 == "OK"
        out3 = rhel_mcp.rhel_read_config("/var/log/nginx/access.log")
        assert out3 == "OK"
        assert mock_run.call_count == 3

    # 阻擋 /var/log/secure, /var/log/audit/audit.log, /var/log/wtmp
    with patch("mcp_servers.rhel_mcp._capture_ssh") as mock_run:
        res_sec = rhel_mcp.rhel_read_config("/var/log/secure")
        assert res_sec.startswith("[ERROR]")
        res_audit = rhel_mcp.rhel_read_config("/var/log/audit/audit.log")
        assert res_audit.startswith("[ERROR]")
        res_wtmp = rhel_mcp.rhel_read_config("/var/log/wtmp")
        assert res_wtmp.startswith("[ERROR]")
        mock_run.assert_not_called()


def test_list_directory_and_selinux_context(monkeypatch):
    _set_full_creds(monkeypatch)
    fake_proc = SimpleNamespace(returncode=0, stdout="drwxr-xr-x file.conf", stderr="")

    with patch("mcp_servers.rhel_mcp._capture_ssh", return_value=fake_proc) as mock_run:
        # 測試一般列出與 show_selinux
        out = rhel_mcp.rhel_list_directory("/etc/systemd/system", show_selinux=True)
        assert out == "drwxr-xr-x file.conf"
        called_cmd = mock_run.call_args.args[0][-1]
        assert "ls -laZ" in called_cmd
        assert "/srv/*" in called_cmd
        assert "/opt/*" in called_cmd

        # 測試 SELinux context 查詢
        out_ctx = rhel_mcp.rhel_selinux_context("/srv/web/index.html")
        assert out_ctx == "drwxr-xr-x file.conf"
        called_cmd_ctx = mock_run.call_args.args[0][-1]
        assert "ls -dZ -- /srv/web/index.html" in called_cmd_ctx
        assert "matchpathcon /srv/web/index.html" in called_cmd_ctx


def test_process_inspect_and_validation(monkeypatch):
    _set_full_creds(monkeypatch)
    fake_proc = SimpleNamespace(returncode=0, stdout="1234 root python", stderr="")

    # PID 查詢與注入防禦
    with patch("mcp_servers.rhel_mcp._capture_ssh", return_value=fake_proc) as mock_run:
        out = rhel_mcp.rhel_process_inspect(pid="1234")
        assert out == "1234 root python"
        called_cmd = mock_run.call_args.args[0][-1]
        assert "ps -fp 1234" in called_cmd

    with patch("mcp_servers.rhel_mcp._capture_ssh") as mock_run:
        bad_pid = rhel_mcp.rhel_process_inspect(pid="1234; reboot")
        assert bad_pid.startswith("[ERROR]")
        mock_run.assert_not_called()

    # 關鍵字搜尋與注入防禦
    with patch("mcp_servers.rhel_mcp._capture_ssh", return_value=fake_proc) as mock_run:
        out_search = rhel_mcp.rhel_process_inspect(search="nginx", show_selinux=True)
        assert out_search == "1234 root python"
        called_cmd_search = mock_run.call_args.args[0][-1]
        assert "label" in called_cmd_search
        assert "grep -i -- nginx" in called_cmd_search

    with patch("mcp_servers.rhel_mcp._capture_ssh") as mock_run:
        bad_search = rhel_mcp.rhel_process_inspect(search="nginx && cat /etc/shadow")
        assert bad_search.startswith("[ERROR]")
        mock_run.assert_not_called()

    # 排序欄位白名單與指令注入防禦
    with patch("mcp_servers.rhel_mcp._capture_ssh", return_value=fake_proc) as mock_run:
        out_sort_mem = rhel_mcp.rhel_process_inspect(sort_by="mem")
        assert out_sort_mem == "1234 root python"
        called_cmd_mem = mock_run.call_args.args[0][-1]
        assert "--sort=-%mem" in called_cmd_mem
        assert "sorted by mem" in called_cmd_mem

    with patch("mcp_servers.rhel_mcp._capture_ssh") as mock_run:
        bad_sort = rhel_mcp.rhel_process_inspect(sort_by="cpu' && reboot && echo '")
        assert bad_sort.startswith("[ERROR]")
        assert "非法的 sort_by" in bad_sort
        mock_run.assert_not_called()


def test_network_connections_and_validation(monkeypatch):
    _set_full_creds(monkeypatch)
    fake_proc = SimpleNamespace(returncode=0, stdout="tcp ESTAB 0 0", stderr="")

    with patch("mcp_servers.rhel_mcp._capture_ssh", return_value=fake_proc) as mock_run:
        out = rhel_mcp.rhel_network_connections(protocol="tcp", state="established", port="443")
        assert out == "tcp ESTAB 0 0"
        called_cmd = mock_run.call_args.args[0][-1]
        assert "ss -tan -p state established" in called_cmd
        assert "sport = :443 or dport = :443" in called_cmd

    with patch("mcp_servers.rhel_mcp._capture_ssh") as mock_run:
        bad_state = rhel_mcp.rhel_network_connections(state="invalid_state")
        assert bad_state.startswith("[ERROR]")
        bad_port = rhel_mcp.rhel_network_connections(port="999999")
        assert bad_port.startswith("[ERROR]")
        mock_run.assert_not_called()


def test_package_and_journal_grep(monkeypatch):
    _set_full_creds(monkeypatch)
    fake_proc = SimpleNamespace(returncode=0, stdout="OK", stderr="")

    with patch("mcp_servers.rhel_mcp._capture_ssh", return_value=fake_proc) as mock_run:
        out_pkg = rhel_mcp.rhel_package_info("nginx", verify_integrity=True)
        assert out_pkg == "OK"
        called_pkg_cmd = mock_run.call_args.args[0][-1]
        assert "rpm -V nginx" in called_pkg_cmd

        out_j = rhel_mcp.rhel_query_journal(unit="sshd", grep="Failed password")
        assert out_j == "OK"
        called_j_cmd = mock_run.call_args.args[0][-1]
        assert "-g 'Failed password'" in called_j_cmd

    with patch("mcp_servers.rhel_mcp._capture_ssh") as mock_run:
        bad_pkg = rhel_mcp.rhel_package_info("nginx; id")
        assert bad_pkg.startswith("[ERROR]")
        bad_grep = rhel_mcp.rhel_query_journal(grep="pass; rm -rf /")
        assert bad_grep.startswith("[ERROR]")
        mock_run.assert_not_called()


def test_run_with_askpass_zero_disk_leak():
    fake_proc = SimpleNamespace(returncode=0, stdout="pong", stderr="")
    captured_env = {}

    def fake_subprocess_run(cmd, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        # 驗證執行期 temp 目錄中不存在任何 pw.txt 明文密碼檔，且存在獨立的 askpass.py
        askpass_path = Path(kwargs["env"]["SSH_ASKPASS"])
        assert not (askpass_path.parent / "pw.txt").exists()
        assert (askpass_path.parent / "askpass.py").exists()
        return fake_proc

    with patch("mcp_servers.rhel_mcp._capture_ssh", side_effect=fake_subprocess_run):
        res = rhel_mcp._run_with_askpass(["ssh", "-V"], "SuperSecret!Pw", timeout=5, creationflags=0)
        assert res.stdout == "pong"
        assert captured_env.get("RHEL_SSH_ASKPASS_PW") == "SuperSecret!Pw"
        assert captured_env.get("SSH_ASKPASS_REQUIRE") == "force"


def test_cross_host_credentials_not_leaked(monkeypatch):
    rhel_mcp._RUNTIME_CREDS["RHEL_HOST"] = "192.168.1.10"
    rhel_mcp._RUNTIME_CREDS["RHEL_SSH_PORT"] = "22"
    rhel_mcp._RUNTIME_CREDS["RHEL_SSH_USER"] = "admin"
    rhel_mcp._RUNTIME_CREDS["RHEL_SSH_PASSWORD"] = "OldHostPassword!123"

    # 目標主機變更為不同主機時，禁止沿用舊主機密碼快取
    prompts = iter(["NewHostPass!456"])
    with patch("mcp_servers.rhel_mcp._tty_prompt", side_effect=lambda *a, **kw: next(prompts)) as mock_prompt:
        host, port, user, pw, key = rhel_mcp._resolve_connection_params(ssh_host="192.168.1.20")
        assert host == "192.168.1.20"
        assert pw == "NewHostPass!456"
        assert key == ""
        assert mock_prompt.call_count == 1
        assert "密碼" in mock_prompt.call_args_list[0].args[0]


def test_key_mode_generates_pubkey_ssh_args(tmp_path, monkeypatch):
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("dummy-private-key-data", encoding="utf-8")

    monkeypatch.setenv("RHEL_HOST", "192.168.1.50")
    monkeypatch.setenv("RHEL_SSH_PORT", "22")
    monkeypatch.setenv("RHEL_SSH_USER", "opsuser")
    monkeypatch.setenv("RHEL_SSH_KEY_PATH", str(key_file))

    args, pw, timeout = rhel_mcp._build_ssh_args("uname -a")
    assert "PubkeyAuthentication=yes" in args
    assert "PubkeyAuthentication=no" not in args
    assert "IdentitiesOnly=yes" in args
    assert "IdentityAgent=none" in args
    assert "-i" in args
    assert str(key_file.resolve()) in args
    assert "PreferredAuthentications=keyboard-interactive,password" not in args


def test_key_mode_invalid_path_raises_error(monkeypatch):
    monkeypatch.setenv("RHEL_HOST", "192.168.1.50")
    monkeypatch.setenv("RHEL_SSH_PORT", "22")
    monkeypatch.setenv("RHEL_SSH_USER", "opsuser")
    monkeypatch.setenv("RHEL_SSH_KEY_PATH", "/non/existent/id_ed25519")

    with pytest.raises(ValueError, match="指定的 SSH 私鑰檔案不存在或無法存取"):
        rhel_mcp._build_ssh_args("id")


def test_key_mode_passphrase_passed_via_askpass(tmp_path, monkeypatch):
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("dummy-key", encoding="utf-8")

    monkeypatch.setenv("RHEL_HOST", "192.168.1.50")
    monkeypatch.setenv("RHEL_SSH_PORT", "22")
    monkeypatch.setenv("RHEL_SSH_USER", "opsuser")
    monkeypatch.setenv("RHEL_SSH_KEY_PATH", str(key_file))
    monkeypatch.setenv("RHEL_SSH_KEY_PASSPHRASE", "MyPassphrase!99")

    args, pw, timeout = rhel_mcp._build_ssh_args("uptime")
    assert pw == "MyPassphrase!99"
    assert "PubkeyAuthentication=yes" in args


def test_mcp_manager_maps_ssh_key_path():
    from pocket_agent.extensions.mcp import _CONN_PARAM_MAP

    assert _CONN_PARAM_MAP.get("ssh_key_path") == "RHEL_SSH_KEY_PATH"


def test_mcp_manager_does_not_prompt_for_optional_key_path():
    manager = MCPManager()
    manager._server_envs["rhel_ops"] = {
        "RHEL_HOST": "192.168.1.10",
        "RHEL_SSH_PORT": "22",
        "RHEL_SSH_USER": "admin",
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
                    "ssh_key_path": {"type": "string"},
                },
            },
        ),
    ]
    fake_session = SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=fake_tools)),
        call_tool=AsyncMock(return_value=MagicMock(content=[MagicMock(text="OK")])),
    )
    asyncio.run(
        manager._register_tools(
            "rhel_ops",
            fake_session,
            inject={},
            safe_read_tools=["rhel_system_overview"],
        )
    )
    prompt_calls = []

    def fake_prompt(server_name, missing_keys):
        prompt_calls.append((server_name, missing_keys))
        return {"RHEL_SSH_PASSWORD": "mypassword"}

    manager.on_prompt_conn = fake_prompt
    asyncio.run(manager.call("rhel_system_overview", {}))

    # 關鍵驗證：未配置金鑰時，絕不詢問 RHEL_SSH_KEY_PATH，僅詢問 RHEL_SSH_PASSWORD
    assert prompt_calls == [("rhel_ops", ["RHEL_SSH_PASSWORD"])]


def test_capture_ssh_exceeded_quota_kills_process():
    """驗證當 stdout/stderr 串流累積超過 MAX_CAPTURE_BYTES (1MB) 時，觸發 proc.kill 並回傳中止訊息"""
    import io

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        chunk = b"X" * (rhel_mcp.MAX_CAPTURE_BYTES + 4096)
        mock_proc.stdout = io.BytesIO(chunk)
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.wait = MagicMock(return_value=0)
        mock_proc.kill = MagicMock()
        mock_popen.return_value.__enter__.return_value = mock_proc

        res = rhel_mcp._capture_ssh(["ssh", "test"], timeout=5, creationflags=0, env={})
        mock_proc.kill.assert_called()
        assert res.returncode == 1
        assert "輸出超過安全位元組上限" in res.stderr

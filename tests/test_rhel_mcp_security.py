"""RHEL 行為邊界回歸；不連線，也不使用真實憑證。"""

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_servers import rhel_mcp


@pytest.mark.parametrize(
    "content",
    [
        "psk=synthetic-value",
        "password: |\n  synthetic-value\npublic: yes",
        'password: "synthetic-value\ncontinued',
        "password\nsynthetic-value",
        "password: first-part\n  synthetic-value",
        "-----BEGIN PRIVATE KEY-----\nsynthetic-value",
        "https://user:synthetic-value@example.invalid/",
    ],
)
def test_sensitive_values_never_leave_output(content: str) -> None:
    assert "synthetic-value" not in rhel_mcp._redact_and_truncate(content)


@pytest.mark.parametrize("content", ["public\x00value", "public\ufffdvalue", "public\x1bvalue"])
def test_non_text_output_is_discarded(content: str) -> None:
    result = rhel_mcp._redact_and_truncate(content)
    assert result.startswith("[ERROR]")
    assert "public" not in result


def test_closed_quoted_value_does_not_discard_public_fields() -> None:
    result = rhel_mcp._redact_and_truncate('host=example\npassword="synthetic-value"\nport=22')
    assert "host=example" in result
    assert "port=22" in result
    assert "synthetic-value" not in result


def test_same_path_policy_for_file_and_directory() -> None:
    file_guard = rhel_mcp._remote_path_guard("/srv/app/config", directory=False)
    dir_guard = rhel_mcp._remote_path_guard("/srv/app", directory=True)
    for guard in (file_guard, dir_guard):
        assert 'case "$lower"' in guard
        assert "LC_ALL=C tr" in guard
        assert "*secret*" in guard
        assert 'case "$real"' in guard
    assert '[ -f "$real" ]' in file_guard
    assert '[ -d "$real" ]' in dir_guard


def test_netrc_rejected_before_connection() -> None:
    with patch.object(rhel_mcp, "_run_remote") as remote:
        assert rhel_mcp.rhel_read_config("/srv/app/.netrc").startswith("[ERROR]")
    remote.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    ["credential = synthetic-value", "redis-cli -a synthetic-value", "SELINUX=synthetic-value"],
)
def test_config_summary_rejects_unapproved_output(payload: str) -> None:
    with patch.object(rhel_mcp, "_run_remote", return_value=payload):
        result = rhel_mcp.rhel_read_config("/etc/selinux/config", max_lines=1)
    assert result.startswith("[ERROR]")
    assert "synthetic-value" not in result


def test_permission_denied_preserves_credentials() -> None:
    previous = dict(rhel_mcp._RUNTIME_CREDS)
    try:
        rhel_mcp._RUNTIME_CREDS["RHEL_SSH_PASSWORD"] = "synthetic-value"
        with (
            patch.object(rhel_mcp, "_build_ssh_args", return_value=(["ssh"], "", 3)),
            patch.object(
                rhel_mcp,
                "_run_with_askpass",
                return_value=subprocess.CompletedProcess([], 1, "", "Permission denied"),
            ),
        ):
            result = rhel_mcp._run_remote("query")
        assert "CONN_AUTH" not in result
        assert rhel_mcp._RUNTIME_CREDS["RHEL_SSH_PASSWORD"] == "synthetic-value"
    finally:
        rhel_mcp._RUNTIME_CREDS.clear()
        rhel_mcp._RUNTIME_CREDS.update(previous)


def test_unexpected_exception_does_not_echo_raw_payload() -> None:
    with patch.object(rhel_mcp, "_build_ssh_args", side_effect=RuntimeError("synthetic-value")):
        assert "synthetic-value" not in rhel_mcp._run_remote("query")


def test_diagnostic_wrapper_drains_instead_of_early_pipe_close() -> None:
    with patch.object(rhel_mcp, "_run_remote", return_value="OK") as remote:
        rhel_mcp.rhel_network_connections()
    script = shlex.split(rhel_mcp._diagnostic_command(remote.call_args.args[0]))[2]
    assert "set -E -o pipefail" in script
    assert "sed -n '1,60p'" in script
    assert "head -n" not in script
    assert "trap " in script
    assert " ERR;" in script


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="需 POSIX Bash")
def test_shell_pipeline_failure_is_reported_even_if_last_section_succeeds() -> None:
    command = rhel_mcp._diagnostic_command("false | sed -n '1,20p'; printf public")
    result = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=3)
    assert result.stdout == "public"
    assert "[ERROR]" in result.stderr


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="需 POSIX Bash")
def test_real_path_guard_rejects_uppercase_sensitive_symlink(tmp_path: Path, monkeypatch) -> None:
    sensitive = tmp_path / "SECRET"
    sensitive.mkdir()
    (sensitive / "config").write_text("synthetic-value", encoding="utf-8")
    alias = tmp_path / "public.conf"
    alias.symlink_to(sensitive / "config")
    monkeypatch.setattr(rhel_mcp, "_ALLOWED_CONFIG_PREFIXES", (str(tmp_path) + "/",))
    command = rhel_mcp._remote_path_guard(str(alias), directory=False) + 'cat -- "$real"'
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=3)
    assert result.returncode != 0
    assert "synthetic-value" not in result.stdout


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "mkfifo") or shutil.which("bash") is None,
    reason="需 POSIX FIFO 與 Bash",
)
def test_real_path_guard_rejects_fifo_before_open(tmp_path: Path, monkeypatch) -> None:
    fifo = tmp_path / "stream"
    os.mkfifo(fifo)
    monkeypatch.setattr(rhel_mcp, "_ALLOWED_CONFIG_PREFIXES", (str(tmp_path) + "/",))
    command = rhel_mcp._remote_path_guard(str(fifo), directory=False) + 'cat -- "$real"'
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=3)
    assert result.returncode != 0
    assert "不支援的檔案類型" in result.stdout


@pytest.mark.parametrize(
    "payload",
    [
        '{"PRIORITY":"3","MESSAGE":"synthetic-value"}',
        '{"PRIORITY":"3","EXTRA":"synthetic-value"}',
    ],
)
def test_journal_only_returns_counts(payload: str) -> None:
    with patch.object(rhel_mcp, "_run_remote", return_value=payload) as remote:
        result = rhel_mcp.rhel_query_journal()
    assert "synthetic-value" not in result
    assert '"sample_count": 1' in result
    assert "--output-fields=PRIORITY" in remote.call_args.args[0]


@pytest.mark.parametrize("payload", ["synthetic-value", "[]", '{"PRIORITY":[]}', "[ERROR] synthetic-value"])
def test_journal_rejects_invalid_output_without_echo(payload: str) -> None:
    with patch.object(rhel_mcp, "_run_remote", return_value=payload):
        result = rhel_mcp.rhel_query_journal()
    assert result.startswith("[ERROR]")
    assert "synthetic-value" not in result


def test_service_command_excludes_logs_and_execution_properties() -> None:
    with patch.object(rhel_mcp, "_run_remote", return_value="ActiveState=active") as remote:
        rhel_mcp.rhel_service_status("sshd.service")
    command = remote.call_args.args[0]
    assert "systemctl show" in command
    assert "systemctl status" not in command
    assert "ExecStart" not in command
    assert "Environment" not in command


def test_cron_and_security_commands_do_not_return_raw_sources() -> None:
    with patch.object(rhel_mcp, "_run_remote", return_value="OK") as remote:
        rhel_mcp.rhel_cron_and_timers()
        cron_command = remote.call_args.args[0]
        rhel_mcp.rhel_security_status()
        security_command = remote.call_args.args[0]
    assert "cat /etc/crontab" not in cron_command
    assert "system_crontab_entries=" in cron_command
    assert "ls -la" not in cron_command
    assert "ausearch" not in security_command
    assert "journalctl -t" not in security_command
    assert "dnf updateinfo" not in security_command
    assert "[UNSUPPORTED] 安全更新查詢已停用" in security_command


def test_rpm_verification_never_executes_scripts_or_returns_paths() -> None:
    with patch.object(rhel_mcp, "_run_remote", return_value="OK") as remote:
        rhel_mcp.rhel_package_info("synthetic-package", verify_integrity=True)
    command = remote.call_args.args[0]
    assert "rpm -V --noscripts synthetic-package" in command
    assert "rpm -ql" not in command
    assert "if rpm -V" in command


def test_empty_journal_has_zero_sample_count() -> None:
    with patch.object(rhel_mcp, "_run_remote", return_value="(沒有輸出)"):
        result = rhel_mcp.rhel_query_journal()
    assert '"sample_count": 0' in result


def test_directory_listing_is_rejected_before_connection() -> None:
    with patch.object(rhel_mcp, "_run_remote") as remote:
        result = rhel_mcp.rhel_list_directory("/etc", show_selinux=True)
    assert result.startswith("[ERROR]")
    remote.assert_not_called()


def test_container_status_is_disabled_before_connection() -> None:
    with patch.object(rhel_mcp, "_run_remote") as remote:
        result = rhel_mcp.rhel_container_status()
    assert result.startswith("[ERROR] [UNSUPPORTED]")
    remote.assert_not_called()


def test_package_info_excludes_dnf_fallback() -> None:
    with patch.object(rhel_mcp, "_run_remote", return_value="OK") as remote:
        rhel_mcp.rhel_package_info("nginx", verify_integrity=False)
    command = remote.call_args.args[0]
    assert "dnf" not in command
    assert "rpm -qi" in command


def test_ssh_args_enforces_strict_host_key_and_dedicated_known_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    args, _, _ = rhel_mcp._build_ssh_args(
        "uptime",
        ssh_host="192.168.1.50",
        ssh_port="22",
        ssh_user="testuser",
        ssh_password="password",
    )
    assert "StrictHostKeyChecking=accept-new" in args
    assert any("UserKnownHostsFile=" in a for a in args)
    assert "StrictHostKeyChecking=no" not in args

    # 驗證透過環境變數可切換為 yes
    monkeypatch.setenv("RHEL_STRICT_HOST_KEY", "yes")
    strict_args, _, _ = rhel_mcp._build_ssh_args(
        "uptime",
        ssh_host="192.168.1.50",
        ssh_port="22",
        ssh_user="testuser",
        ssh_password="password",
    )
    assert "StrictHostKeyChecking=yes" in strict_args


def test_host_validation_supports_ipv6_and_rejects_options() -> None:
    assert rhel_mcp._validate_host("192.168.1.1") == "192.168.1.1"
    assert rhel_mcp._validate_host("2001:db8::1") == "[2001:db8::1]"
    assert rhel_mcp._validate_host("[::1]") == "[::1]"
    assert rhel_mcp._validate_host("rhel.example.com") == "rhel.example.com"
    with pytest.raises(ValueError):
        rhel_mcp._validate_host("-oProxyCommand=calc")


def test_credentials_ttl_clears_after_timeout() -> None:
    rhel_mcp._RUNTIME_CREDS["RHEL_HOST"] = "192.168.1.1"
    rhel_mcp._CREDS_UPDATED_AT = 100.0
    with patch.object(rhel_mcp.time, "monotonic", return_value=100.0 + 700.0):
        rhel_mcp._check_creds_ttl()
    assert rhel_mcp._RUNTIME_CREDS == {}

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


def test_read_uses_byte_limit_and_displays_lines_after_redaction() -> None:
    with patch.object(rhel_mcp, "_run_remote", return_value="public\n[REDACTED]\nlast") as remote:
        result = rhel_mcp.rhel_read_config("/srv/app/custom.conf", max_lines=1)
    command = remote.call_args.args[0]
    assert f"head -c {rhel_mcp.MAX_CAPTURE_BYTES + 1}" in command
    assert "head -n" not in command
    assert result == "public\n[提示：已達 max_lines 顯示上限]"


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

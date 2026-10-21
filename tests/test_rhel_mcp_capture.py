"""隔離的擷取生命週期測試；不啟動 SSH。"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from mcp_servers import rhel_mcp


def _process() -> MagicMock:
    process = MagicMock()
    process.stdout.fileno.return_value = 10
    process.stderr.fileno.return_value = 11
    process.poll.return_value = 0
    process.returncode = 0
    return process


def test_exited_parent_without_pipe_eof_still_times_out() -> None:
    process = _process()
    with (
        patch.object(rhel_mcp.subprocess, "Popen", return_value=process),
        patch.object(rhel_mcp.os, "set_blocking"),
        patch.object(rhel_mcp.os, "read", side_effect=BlockingIOError),
        patch.object(rhel_mcp.time, "monotonic", side_effect=[0, 0, 0, 2]),
        patch.object(rhel_mcp.time, "sleep"),
    ):
        with pytest.raises(subprocess.TimeoutExpired):
            rhel_mcp._capture_ssh(["ssh"], timeout=1, creationflags=0, env={})
    process.wait.assert_called_once_with(timeout=1)
    process.stdout.close.assert_called_once()
    process.stderr.close.assert_called_once()


def test_capture_preserves_both_streams() -> None:
    process = _process()
    with (
        patch.object(rhel_mcp.subprocess, "Popen", return_value=process),
        patch.object(rhel_mcp.os, "set_blocking") as blocking,
        patch.object(rhel_mcp.os, "read", side_effect=[b"public", b"diagnostic", b"", b""]),
    ):
        result = rhel_mcp._capture_ssh(["ssh"], timeout=1, creationflags=0, env={})
    assert result.stdout == "public"
    assert result.stderr == "diagnostic"
    assert blocking.call_count == 2


def test_decode_error_discards_all_content() -> None:
    process = _process()
    with (
        patch.object(rhel_mcp.subprocess, "Popen", return_value=process),
        patch.object(rhel_mcp.os, "set_blocking"),
        patch.object(rhel_mcp.os, "read", side_effect=[b"synthetic-value\xff", b"", b""]),
    ):
        result = rhel_mcp._capture_ssh(["ssh"], timeout=1, creationflags=0, env={})
    assert result.returncode == 1
    assert result.stdout == ""
    assert "synthetic-value" not in result.stderr


def test_nonblocking_setup_failure_cleans_up_without_fallback() -> None:
    process = _process()
    process.poll.return_value = None
    with (
        patch.object(rhel_mcp.subprocess, "Popen", return_value=process),
        patch.object(rhel_mcp.os, "set_blocking", side_effect=OSError("unsupported")),
        patch.object(rhel_mcp.os, "read") as read,
    ):
        with pytest.raises(OSError):
            rhel_mcp._capture_ssh(["ssh"], timeout=1, creationflags=0, env={})
    read.assert_not_called()
    process.kill.assert_called_once()
    process.wait.assert_called_once_with(timeout=1)


def test_read_failure_never_returns_partial_output() -> None:
    process = _process()
    with (
        patch.object(rhel_mcp.subprocess, "Popen", return_value=process),
        patch.object(rhel_mcp.os, "set_blocking"),
        patch.object(rhel_mcp.os, "read", side_effect=[b"synthetic-value", OSError("read failure")]),
    ):
        with pytest.raises(OSError):
            rhel_mcp._capture_ssh(["ssh"], timeout=1, creationflags=0, env={})
    process.stdout.close.assert_called_once()
    process.stderr.close.assert_called_once()


@pytest.mark.parametrize("payload", ["public", "X" * 4096])
def test_real_local_process_capture(payload: str) -> None:
    """由使用者執行測試時驗證真實平台管線，不使用 SSH 或檔案。"""
    import os
    import sys

    result = rhel_mcp._capture_ssh(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", payload],
        timeout=5,
        creationflags=0,
        env=dict(os.environ),
    )
    assert result.returncode == 0
    assert result.stdout == payload


def test_real_child_holding_pipe_has_bounded_capture() -> None:
    """孫行程自然結束；測試證明有界返回，不宣稱已清除後代行程。"""
    import os
    import sys
    import time

    script = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'])"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        rhel_mcp._capture_ssh([sys.executable, "-c", script], timeout=0.2, creationflags=0, env=dict(os.environ))
    assert time.monotonic() - started < 1.8

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# 確保可以 import 根目錄的套件
sys.path.insert(0, str(Path(__file__).parent.parent))

from pocket_agent.tools.shell import call_shell_tool


def test_shell_tool_no_sandbox():
    """測試預設情況下（無沙盒），指令會以 shell=True 執行"""
    args = {"command": "echo hello"}

    with patch("pocket_agent.tools.shell.subprocess.run") as mock_run:
        with patch.dict(os.environ, {"USE_SANDBOX": "none"}):
            # mock 回傳值，避免 _decode 拋出例外
            mock_run.return_value = MagicMock(stdout=b"hello\n", stderr=b"", returncode=0)

            result = call_shell_tool("run_command", args)

            # 檢查傳遞給 subprocess.run 的參數
            mock_run.assert_called_once()
            call_args, call_kwargs = mock_run.call_args

            assert call_args[0] == "echo hello"
            assert call_kwargs["shell"] is True
            assert "hello" in result


def test_shell_tool_wsl_sandbox():
    """測試 WSL 沙盒模式會將指令包裝成 wsl -- sh -c 且不使用 shell=True"""
    args = {"command": "ls -l"}

    with patch("pocket_agent.tools.shell.subprocess.run") as mock_run:
        with patch.dict(os.environ, {"USE_SANDBOX": "wsl"}):
            mock_run.return_value = MagicMock(stdout=b"list of files\n", stderr=b"", returncode=0)

            result = call_shell_tool("run_command", args)

            mock_run.assert_called_once()
            call_args, call_kwargs = mock_run.call_args

            assert call_args[0] == ["wsl", "--", "sh", "-c", "ls -l"]
            assert call_kwargs["shell"] is False
            assert "list of files" in result


def test_shell_tool_docker_sandbox():
    """測試 Docker 沙盒模式會將指令包裝成 docker exec"""
    args = {"command": "npm test"}

    with patch("pocket_agent.tools.shell.subprocess.run") as mock_run:
        with patch.dict(os.environ, {"USE_SANDBOX": "docker", "SANDBOX_CONTAINER": "my_sandbox"}):
            mock_run.return_value = MagicMock(stdout=b"tests passed\n", stderr=b"", returncode=0)

            result = call_shell_tool("run_command", args)

            mock_run.assert_called_once()
            call_args, call_kwargs = mock_run.call_args

            assert call_args[0] == ["docker", "exec", "my_sandbox", "sh", "-c", "npm test"]
            assert call_kwargs["shell"] is False
            assert "tests passed" in result

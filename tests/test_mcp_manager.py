import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# 確保可以 import 根目錄的套件
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio

import pytest
import yaml

from pocket_agent.extensions.mcp import MCPManager, MCPStartupResult, ToolEntry


def test_mcp_artifact_interception():
    """測試 MCP Manager 的 ArtifactMetadata 攔截機制，驗證檔案路徑是否被成功重新導向至 artifacts 目錄"""

    async def run_test():
        manager = MCPManager(config_path="dummy.yaml")

        # 建立一個 Mock 的 ToolEntry 與 Session
        mock_session = AsyncMock()
        mock_session.call_tool.return_value = MagicMock(content=[MagicMock(text="success")])

        entry = ToolEntry(name="write_to_file", server_name="test_server", session=mock_session, schema={}, inject={})
        manager._tools["write_to_file"] = entry

        # 模擬模型發起的工具呼叫參數
        args = {
            "TargetFile": "/dummy/project/path/test_doc.md",
            "CodeContent": "# Title",
            "ArtifactMetadata": {"UserFacing": True, "Summary": "A test artifact"},
        }

        import tempfile

        temp_dir = Path(tempfile.mkdtemp())

        # 攔截 user_dir 以避免影響真實的 ~/.pocket-agent
        with patch("pocket_agent.system.paths.user_dir") as mock_user_dir:
            mock_user_dir.return_value = temp_dir

            await manager.call("write_to_file", args)

            # 驗證 session.call_tool 被正確呼叫
            mock_session.call_tool.assert_called_once()
            called_name, called_args = mock_session.call_tool.call_args[0]

            assert called_name == "write_to_file"

            # 驗證 ArtifactMetadata 被移除
            assert "ArtifactMetadata" not in called_args

            # 驗證路徑被替換成 artifacts 隔離區
            expected_path = str(temp_dir / "artifacts" / "test_doc.md")
            assert called_args["TargetFile"] == expected_path

    asyncio.run(run_test())


def test_mcp_artifact_interception_no_user_facing():
    """測試若非 UserFacing 的檔案（例如 scratch files）不應被重新導向（或視具體實作邏輯）"""

    async def run_test():
        manager = MCPManager(config_path="dummy.yaml")

        mock_session = AsyncMock()
        mock_session.call_tool.return_value = MagicMock(content=[MagicMock(text="success")])

        entry = ToolEntry(name="write_to_file", server_name="test_server", session=mock_session, schema={}, inject={})
        manager._tools["write_to_file"] = entry

        args = {
            "TargetFile": "/dummy/project/path/scratch.txt",
            "CodeContent": "temp data",
            "ArtifactMetadata": {"UserFacing": False, "Summary": "Temporary data"},
        }

        import tempfile

        temp_dir = Path(tempfile.mkdtemp())

        with patch("pocket_agent.system.paths.user_dir") as mock_user_dir:
            mock_user_dir.return_value = temp_dir

            await manager.call("write_to_file", args)

            called_name, called_args = mock_session.call_tool.call_args[0]

            # 驗證 ArtifactMetadata 被移除
            assert "ArtifactMetadata" not in called_args

            # 驗證路徑【沒有】被替換
            assert called_args["TargetFile"] == "/dummy/project/path/scratch.txt"

    asyncio.run(run_test())


@pytest.mark.parametrize("content", ["", "{}", "servers: null", "servers: {}", "servers:\n  disabled: null"])
def test_start_without_servers(tmp_path, content):
    config = tmp_path / "mcp.yaml"
    config.write_text(content, encoding="utf-8")
    manager = MCPManager(config)
    callback = MagicMock()
    with (
        patch.object(manager, "_load_config", wraps=manager._load_config) as load,
        patch.object(manager, "_connect", new_callable=AsyncMock) as connect,
        patch.object(manager, "_register_tools", new_callable=AsyncMock) as register,
    ):
        assert asyncio.run(manager.start(on_start=callback)) == MCPStartupResult(0, 0, 0, 0)
    load.assert_called_once_with()
    callback.assert_not_called()
    connect.assert_not_awaited()
    register.assert_not_awaited()


@pytest.mark.parametrize("tool_names", [[], ["one"], ["shared", "shared"]])
def test_start_counts_ready_servers_and_registry(tool_names):
    manager = MCPManager("dummy.yaml")
    events = []
    sessions = []
    for name in tool_names or [None]:
        session = AsyncMock()
        tools = []
        if name is not None:
            tool = MagicMock()
            tool.name = name
            tool.description = "test"
            tool.inputSchema = {"type": "object", "properties": {}}
            tools.append(tool)
        session.list_tools.return_value = MagicMock(tools=tools)
        sessions.append(session)

    async def connect(name, spec):
        events.append(name)
        return sessions[int(name)]

    servers = {str(index): {"command": "fake"} for index in range(len(sessions))}
    servers["disabled"] = None
    with (
        patch.object(manager, "_load_config", return_value={"servers": servers}) as load,
        patch.object(manager, "_connect", side_effect=connect),
    ):
        result = asyncio.run(manager.start(on_start=lambda count: events.append(count)))
    assert result == MCPStartupResult(len(sessions), len(sessions), 0, len(tool_names))
    assert events == [len(sessions), *map(str, range(len(sessions)))]
    load.assert_called_once_with()
    for session in sessions:
        session.list_tools.assert_awaited_once_with()


@pytest.mark.parametrize("failure_stage", ["connect", "list_tools"])
@pytest.mark.parametrize("all_fail", [False, True])
def test_start_reports_failures_and_continues(failure_stage, all_fail):
    manager = MCPManager("dummy.yaml")
    attempted = []

    async def connect(name, spec):
        attempted.append(name)
        fails = name == "first" or all_fail
        if fails and failure_stage == "connect":
            raise RuntimeError("offline")
        session = AsyncMock()
        if fails:
            session.list_tools.side_effect = RuntimeError("unavailable")
        else:
            session.list_tools.return_value = MagicMock(tools=[])
        return session

    with (
        patch.object(manager, "_load_config", return_value={"servers": {"first": {}, "second": {}}}),
        patch.object(manager, "_connect", side_effect=connect),
    ):
        result = asyncio.run(manager.start())
    assert attempted == ["first", "second"]
    assert result == MCPStartupResult(2, 0 if all_fail else 1, 2 if all_fail else 1, 0)


def test_start_empty_spec_is_failure_not_skipped():
    manager = MCPManager("dummy.yaml")
    callback = MagicMock()
    with patch.object(manager, "_load_config", return_value={"servers": {"invalid": {}}}):
        assert asyncio.run(manager.start(on_start=callback)) == MCPStartupResult(1, 0, 1, 0)
    callback.assert_called_once_with(1)


@pytest.mark.parametrize("error", [OSError("unreadable"), yaml.YAMLError("invalid YAML")])
def test_start_config_errors_propagate(error):
    manager = MCPManager("dummy.yaml")
    callback = MagicMock()
    with (
        patch.object(manager, "_load_config", side_effect=error),
        patch.object(manager, "_connect", new_callable=AsyncMock) as connect,
        pytest.raises(type(error)),
    ):
        asyncio.run(manager.start(on_start=callback))
    callback.assert_not_called()
    connect.assert_not_awaited()


@pytest.mark.parametrize("stage", ["connect", "register"])
def test_start_cancellation_propagates(stage):
    manager = MCPManager("dummy.yaml")
    with (
        patch.object(manager, "_load_config", return_value={"servers": {"test": {}}}),
        patch.object(manager, "_connect", new_callable=AsyncMock) as connect,
        patch.object(manager, "_register_tools", new_callable=AsyncMock) as register,
    ):
        if stage == "connect":
            connect.side_effect = asyncio.CancelledError()
        else:
            register.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(manager.start())

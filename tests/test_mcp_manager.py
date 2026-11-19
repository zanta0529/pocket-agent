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


def test_parallel_calls_serialize_conn_prompt():
    import time
    from types import SimpleNamespace

    manager = MCPManager("dummy.yaml")
    fake_tools = [
        SimpleNamespace(
            name=f"rhel_tool_{i}",
            description=f"tool {i}",
            inputSchema={
                "type": "object",
                "properties": {
                    "ssh_host": {"type": "string"},
                    "ssh_port": {"type": "string"},
                    "ssh_user": {"type": "string"},
                    "ssh_password": {"type": "string"},
                },
            },
        )
        for i in range(5)
    ]
    fake_session = SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=fake_tools)),
        call_tool=AsyncMock(return_value=MagicMock(content=[MagicMock(text="OK")])),
    )
    asyncio.run(manager._register_tools("rhel_ops", fake_session, inject={}, safe_read_tools="*"))

    prompt_calls = []

    def slow_prompt(server_name, missing_keys):
        time.sleep(0.02)
        prompt_calls.append((server_name, list(missing_keys)))
        return {
            "RHEL_HOST": "10.0.0.10",
            "RHEL_SSH_PORT": "22",
            "RHEL_SSH_USER": "ops",
            "RHEL_SSH_PASSWORD": "secret",
        }

    manager.on_prompt_conn = slow_prompt

    async def _run_parallel():
        return await asyncio.gather(*(manager.call(f"rhel_tool_{i}", {}) for i in range(5)))

    results = asyncio.run(_run_parallel())
    assert len(results) == 5
    assert len(prompt_calls) == 1
    assert fake_session.call_tool.await_count == 5


def test_start_skips_disabled_servers():
    from pocket_agent.extensions.mcp import is_mcp_server_enabled

    assert is_mcp_server_enabled({"command": "fake"}) is True
    assert is_mcp_server_enabled({"command": "fake", "enabled": True}) is True
    assert is_mcp_server_enabled({"command": "fake", "enabled": False}) is False

    manager = MCPManager("dummy.yaml")
    connected = []
    session = AsyncMock()
    session.list_tools.return_value = MagicMock(tools=[])

    async def connect(name, spec):
        connected.append(name)
        return session

    servers = {
        "default_on": {"command": "fake"},
        "explicit_on": {"command": "fake", "enabled": True},
        "explicit_off": {"command": "fake", "enabled": False},
        "null_entry": None,
    }
    callback = MagicMock()
    with (
        patch.object(manager, "_load_config", return_value={"servers": servers}),
        patch.object(manager, "_connect", side_effect=connect),
    ):
        result = asyncio.run(manager.start(on_start=callback))

    assert connected == ["default_on", "explicit_on"]
    callback.assert_called_once_with(2)
    assert result == MCPStartupResult(2, 2, 0, 0, disabled_servers=1)


def test_start_all_disabled_servers():
    manager = MCPManager("dummy.yaml")
    servers = {
        "off_one": {"command": "fake", "enabled": False},
        "off_two": {"command": "fake", "enabled": False},
    }
    callback = MagicMock()
    with (
        patch.object(manager, "_load_config", return_value={"servers": servers}),
        patch.object(manager, "_connect", new_callable=AsyncMock) as connect,
    ):
        result = asyncio.run(manager.start(on_start=callback))

    callback.assert_not_called()
    connect.assert_not_awaited()
    assert result == MCPStartupResult(0, 0, 0, 0, disabled_servers=2)


@pytest.mark.parametrize("bad_value", ["false", "true", "invalid", 0, 1, None])
def test_start_non_bool_enabled_defaults_to_disabled(bad_value, caplog):
    manager = MCPManager("dummy.yaml")
    servers = {"bad": {"command": "fake", "enabled": bad_value}}
    with (
        caplog.at_level("WARNING", logger="pocket-agent"),
        patch.object(manager, "_load_config", return_value={"servers": servers}),
        patch.object(manager, "_connect", new_callable=AsyncMock) as connect,
    ):
        result = asyncio.run(manager.start())

    connect.assert_not_awaited()
    assert result == MCPStartupResult(0, 0, 0, 0, disabled_servers=1)
    assert "enabled" in caplog.text

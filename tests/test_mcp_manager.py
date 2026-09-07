import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# 確保可以 import 根目錄的套件
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio

from agent.mcp_manager import MCPManager, ToolEntry


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
        with patch("agent.paths.user_dir") as mock_user_dir:
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

        with patch("agent.paths.user_dir") as mock_user_dir:
            mock_user_dir.return_value = temp_dir

            await manager.call("write_to_file", args)

            called_name, called_args = mock_session.call_tool.call_args[0]

            # 驗證 ArtifactMetadata 被移除
            assert "ArtifactMetadata" not in called_args

            # 驗證路徑【沒有】被替換
            assert called_args["TargetFile"] == "/dummy/project/path/scratch.txt"

    asyncio.run(run_test())

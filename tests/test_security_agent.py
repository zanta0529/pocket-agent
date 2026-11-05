from unittest.mock import MagicMock

from pocket_agent.core.agent import Agent
from pocket_agent.extensions.mcp import MCPManager


def test_agent_instructions_contains_security_boundaries():
    """測試主代理與子代理的 system prompt 是否成功注入外部資料防禦條款。"""
    mock_client = MagicMock()
    mock_mcp = MagicMock(spec=MCPManager)

    agent = Agent(
        client=mock_client,
        mcp=mock_mcp,
        system_prompt="Base prompt for testing.",
    )

    instructions = agent._instructions(query="test_query")

    # 驗證提示詞注入的防護宣告存在
    assert "[資安防禦原則]" in instructions
    assert "<external_content>" in instructions
    assert "不可信資料（Untrusted Data）" in instructions
    assert "絕對不可" in instructions

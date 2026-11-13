"""MCP 強制命名空間、精簡顯示與動態欄寬單元測試。"""

import asyncio
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from rich.console import Console

from pocket_agent.core.agent import Agent
from pocket_agent.extensions.mcp import (
    MCPManager,
    format_mcp_tool_name,
    normalize_mcp_server_name,
    parse_mcp_tool_name,
)
from pocket_agent.frontend.cli.commands.core import CommandContext
from pocket_agent.frontend.cli.commands.misc import McpCommand, _compute_mcp_grid_layout
from pocket_agent.frontend.cli.render import CliRenderer
from pocket_agent.frontend.cli.state import CliState


def test_mcp_name_helpers():
    assert normalize_mcp_server_name("my.server 01") == "my_server_01"
    assert format_mcp_tool_name("my.server", "list_tables") == "mcp__my_server__list_tables"
    assert parse_mcp_tool_name("mcp__sqlite__sqlite_list_tables") == ("sqlite", "sqlite_list_tables")
    assert parse_mcp_tool_name("read_file") == (None, "read_file")


def test_register_tools_always_applies_mcp_prefix():
    mgr = MCPManager()
    session = MagicMock()
    t1 = SimpleNamespace(name="search_skills", description="Search", inputSchema={"type": "object", "properties": {}})
    t2 = SimpleNamespace(
        name="sqlite_list_tables", description="List", inputSchema={"type": "object", "properties": {}}
    )
    session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[t1, t2]))

    asyncio.run(mgr._register_tools("my.sqlite", session, inject={}, safe_read_tools=["sqlite_list_tables"]))

    assert mgr.list_tools() == [
        "mcp__my_sqlite__search_skills",
        "mcp__my_sqlite__sqlite_list_tables",
    ]
    assert mgr.find_entry("mcp__my_sqlite__sqlite_list_tables") is not None
    assert mgr.find_entry("sqlite_list_tables") is not None
    assert mgr._tools["mcp__my_sqlite__sqlite_list_tables"].schema.get("_is_safe_read") is True


def test_agent_allowed_tools_strict_matching_without_fallback():
    mcp = MCPManager()
    mcp._tools["mcp__sqlite__sqlite_list_tables"] = MagicMock(
        schema={"type": "function", "name": "mcp__sqlite__sqlite_list_tables", "description": "", "parameters": {}}
    )
    agent = Agent(
        client=MagicMock(),
        mcp=mcp,
        is_subagent=True,
        allowed_tools=["sqlite_list_tables"],
    )
    tool_names = [t.get("name") for t in agent._build_tools()]
    assert "mcp__sqlite__sqlite_list_tables" not in tool_names

    agent_exact = Agent(
        client=MagicMock(),
        mcp=mcp,
        is_subagent=True,
        allowed_tools=["mcp__sqlite__sqlite_list_tables"],
    )
    tool_names_exact = [t.get("name") for t in agent_exact._build_tools()]
    assert "mcp__sqlite__sqlite_list_tables" in tool_names_exact


def test_renderer_extract_tool_target_ignores_server_name_keywords():
    renderer = CliRenderer(Console(file=StringIO(), force_terminal=False))
    # server_name 含 "web"，但真實工具名稱不是 web 工具，不應誤取 url 欄位而忽略主要參數
    target = renderer._extract_tool_target("mcp__web_ops__get_item", '{"id": "item-42"}')
    assert target == "item-42"


def test_mcp_command_compact_display_and_dynamic_columns():
    grouped = {
        "rhel_ops": [
            {"name": "mcp__rhel_ops__rhel_network_connections", "original_name": "rhel_network_connections"},
            {"name": "mcp__rhel_ops__rhel_system_overview", "original_name": "rhel_system_overview"},
        ]
    }
    num_cols_narrow, col_w_narrow = _compute_mcp_grid_layout(grouped, term_width=50)
    assert num_cols_narrow == 1
    assert col_w_narrow >= len("• rhel_network_connections") + 3

    num_cols_wide, col_w_wide = _compute_mcp_grid_layout(grouped, term_width=120)
    assert num_cols_wide >= 2
    assert col_w_wide >= len("• rhel_network_connections") + 3

    out = StringIO()
    renderer = CliRenderer(Console(file=out, force_terminal=False, width=100))
    mcp = SimpleNamespace(
        get_grouped_tools=lambda: {
            "agensi": [{"name": "mcp__agensi__search_skills", "original_name": "search_skills", "description": "desc"}]
        },
        find_server_for_tool=lambda _name: None,
    )
    ctx = CommandContext(
        agent=SimpleNamespace(mcp=mcp),
        state=CliState("openai", "model"),
        reader=MagicMock(),
        renderer=renderer,
        tools=[],
    )
    asyncio.run(McpCommand().execute(ctx, ""))
    rendered = out.getvalue()
    assert "search_skills" in rendered
    assert "mcp__agensi__search_skills" not in rendered

"""單元測試：驗證系統參數集中管理常數 (constants.py) 的完整性與各模組一致性。"""

from pocket_agent.core import compaction, context_budget, spillover
from pocket_agent.core.agent import DEFAULT_MAX_TOOL_ROUNDS
from pocket_agent.system import config, constants
from pocket_agent.tools import fs, repo_map, shell, web


def test_constants_values_and_types():
    """驗證 constants.py 中的基礎數值與型別符合設計意圖。"""
    assert constants.DEFAULT_TOOL_OUTPUT_CHARS == 12000
    assert constants.DEFAULT_PROCESS_BUFFER_BYTES == 1024 * 1024
    assert constants.MAX_READ_FILE_BYTES == 10 * 1024 * 1024
    assert constants.MAX_FILE_SEARCH_SIZE_BYTES == 5 * 1024 * 1024
    assert constants.MAX_RESPONSE_BYTES == 2 * 1024 * 1024
    assert constants.DEFAULT_READ_FILE_LINES == 500
    assert constants.DEFAULT_SEARCH_MAX_RESULTS == 100
    assert constants.DEFAULT_NETWORK_TIMEOUT_SEC == 15.0
    assert constants.DEFAULT_MAX_REDIRECTS == 5
    assert constants.DEFAULT_WEB_CONTENT_MAX_CHARS == 15000
    assert constants.DEFAULT_CONTEXT_WINDOW == 128_000
    assert constants.DEFAULT_COMPACT_RATIO == 0.7
    assert constants.DEFAULT_COMPACT_TARGET_RATIO == 0.6
    assert constants.DEFAULT_TOOL_OUTPUT_RATIO == 0.08
    assert constants.CHARS_PER_TOKEN_ESTIMATE == 3
    assert constants.DEFAULT_KEEP_RECENT_OUTPUTS == 2
    assert constants.DEFAULT_SPILLOVER_HEAD_CHARS == 1500
    assert constants.DEFAULT_SPILLOVER_TAIL_CHARS == 1500
    assert constants.DEFAULT_COMPACTION_KEEP_MESSAGES == 6
    assert constants.DEFAULT_REPO_MAP_BUDGET_TOKENS == 1024
    assert constants.DEFAULT_CLI_TIMEOUT_SEC == 60
    assert constants.MAX_CLI_TIMEOUT_SEC == 600

    # AI 模型供應商識別常數
    assert constants.PROVIDER_GEMINI == "gemini"
    assert constants.PROVIDER_GEMINI_OAUTH == "gemini_oauth"
    assert constants.PROVIDER_GROQ == "groq"
    assert constants.PROVIDER_MISTRAL == "mistral"
    assert constants.PROVIDER_OLLAMA == "ollama"
    assert constants.PROVIDER_OLLAMACLOUD == "ollamacloud"
    assert constants.PROVIDER_OPENAI == "openai"
    assert constants.PROVIDER_OPENAI_API == "openai-api"
    assert constants.PROVIDER_OPENROUTER == "openrouter"
    assert constants.PROVIDER_SAMBANOVA == "sambanova"


def test_system_constants_container():
    """驗證 SystemConstants 容器類別預設值正確裝載。"""
    c = constants.SystemConstants()
    assert c.tool_output_chars == 12000
    assert c.process_buffer_bytes == 1024 * 1024
    assert c.read_file_lines == 500
    assert c.network_timeout_sec == 15.0


def test_default_config_maps_to_constants():
    """驗證 config.py 的 DEFAULT_CONFIG 正確引用 constants.py 的常數。"""
    sys_cfg = config.DEFAULT_CONFIG["system"]
    assert sys_cfg["tool_output_chars"] == constants.DEFAULT_TOOL_OUTPUT_CHARS
    assert sys_cfg["process_buffer_bytes"] == constants.DEFAULT_PROCESS_BUFFER_BYTES
    assert sys_cfg["read_file_lines"] == constants.DEFAULT_READ_FILE_LINES
    assert sys_cfg["cli_timeout_sec"] == constants.DEFAULT_CLI_TIMEOUT_SEC
    assert sys_cfg["spillover_head_chars"] == constants.DEFAULT_SPILLOVER_HEAD_CHARS
    assert sys_cfg["spillover_tail_chars"] == constants.DEFAULT_SPILLOVER_TAIL_CHARS
    assert sys_cfg["repo_map_budget"] == constants.DEFAULT_REPO_MAP_BUDGET_TOKENS
    assert sys_cfg["max_tool_rounds"] == constants.DEFAULT_MAX_TOOL_ROUNDS


def test_module_aliases_backward_compatibility():
    """驗證各業務與工具模組的舊變數別名正確對齊 constants.py。"""
    # tools/shell.py
    assert shell.MAX_OUTPUT == constants.DEFAULT_TOOL_OUTPUT_CHARS
    assert shell.MAX_OUTPUT_BYTES == constants.DEFAULT_PROCESS_BUFFER_BYTES

    # tools/fs.py
    assert fs.MAX_READ_LINES == constants.DEFAULT_READ_FILE_LINES
    assert fs.MAX_READ_CHARS == constants.DEFAULT_TOOL_OUTPUT_CHARS
    assert fs.SEARCH_MAX_RESULTS == constants.DEFAULT_SEARCH_MAX_RESULTS

    # tools/web.py
    assert web.MAX_REDIRECTS == constants.DEFAULT_MAX_REDIRECTS
    assert web.MAX_RESPONSE_BYTES == constants.MAX_RESPONSE_BYTES

    # core/context_budget.py
    assert context_budget.DEFAULT_CONTEXT_WINDOW == constants.DEFAULT_CONTEXT_WINDOW
    assert context_budget.COMPACT_RATIO == constants.DEFAULT_COMPACT_RATIO
    assert context_budget.COMPACT_TARGET_RATIO == constants.DEFAULT_COMPACT_TARGET_RATIO
    assert context_budget.TOOL_OUTPUT_RATIO == constants.DEFAULT_TOOL_OUTPUT_RATIO
    assert context_budget.CHARS_PER_TOKEN == constants.CHARS_PER_TOKEN_ESTIMATE
    assert context_budget.KEEP_RECENT_OUTPUTS == constants.DEFAULT_KEEP_RECENT_OUTPUTS
    assert context_budget.DEFAULT_RESERVE_OUTPUT == constants.DEFAULT_RESERVE_OUTPUT_TOKENS
    assert context_budget.DEFAULT_SAFETY_MARGIN == constants.DEFAULT_SAFETY_MARGIN_TOKENS

    # core/spillover.py
    assert spillover.DEFAULT_HEAD_CHARS == constants.DEFAULT_SPILLOVER_HEAD_CHARS
    assert spillover.DEFAULT_TAIL_CHARS == constants.DEFAULT_SPILLOVER_TAIL_CHARS

    # core/compaction.py
    assert compaction.DEFAULT_COMPACTION_KEEP_MESSAGES == 6

    # tools/repo_map.py
    assert repo_map.DEFAULT_REPO_MAP_BUDGET == constants.DEFAULT_REPO_MAP_BUDGET_TOKENS

    # core/agent.py
    assert DEFAULT_MAX_TOOL_ROUNDS == constants.DEFAULT_MAX_TOOL_ROUNDS


def test_mcp_servers_output_chars_aligned():
    """驗證獨立 MCP 伺服器模組亦同步採用 12,000 字元上限。"""
    from mcp_servers import browser_mcp, database_mcp, rhel_mcp, win_rdp_mcp

    assert rhel_mcp.MAX_OUTPUT_CHARS == constants.DEFAULT_TOOL_OUTPUT_CHARS
    assert win_rdp_mcp.MAX_OUTPUT_CHARS == constants.DEFAULT_TOOL_OUTPUT_CHARS
    assert browser_mcp.MAX_TEXT == constants.DEFAULT_TOOL_OUTPUT_CHARS
    assert database_mcp.MAX_OUTPUT_CHARS == constants.DEFAULT_TOOL_OUTPUT_CHARS

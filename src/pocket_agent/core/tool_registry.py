"""核心工具派發抽象層（Adapter / Command Pattern）。

將 Agent 主迴圈與各具體工具模組（fs、shell、doc、document、web、rag、skills、subagents、MCP）
的判斷與呼叫差異解耦，維持核心主迴圈的獨立性與可擴充性。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pocket_agent.core.cancellation import run_sync
from pocket_agent.core.subagents import is_subagent_tool
from pocket_agent.extensions.skills import call_skill_tool, is_skill_tool
from pocket_agent.tools.doc import call_doc_tool, is_doc_tool
from pocket_agent.tools.document import call_document_tool, is_document_tool
from pocket_agent.tools.fs import call_fs_tool, is_fs_tool
from pocket_agent.tools.shell import call_shell_tool, is_shell_tool
from pocket_agent.tools.web import call_web_tool, is_web_tool


def _resolve_callable(attr_name: str, fallback: Any) -> Any:
    """優先從 pocket_agent.core.agent 解析，以相容對該模組之 monkeypatch/patch 測試。"""
    import sys

    agent_mod = sys.modules.get("pocket_agent.core.agent")
    if agent_mod is not None:
        return getattr(agent_mod, attr_name, fallback)
    return fallback


@dataclass
class ToolExecutionResult:
    """統一的工具執行結果封裝。"""

    output: str
    generated_path: Path | None = None
    existed_before: bool | None = None
    short_circuit: bool = False
    ok_override: bool | None = None


class ToolHandler(ABC):
    """工具派發轉接器介面。"""

    @abstractmethod
    def matches(self, name: str) -> bool:
        """判斷此 Handler 是否負責處理指定名稱的工具。"""

    def is_parallel_safe(self, name: str, args: dict, agent: Any) -> bool:
        """判斷該工具呼叫是否為無副作用且免互動確認的唯讀操作。"""
        return False

    @abstractmethod
    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        """執行工具並回傳統一結果。"""


class SkillToolHandler(ToolHandler):
    def matches(self, name: str) -> bool:
        return is_skill_tool(name)

    def is_parallel_safe(self, name: str, args: dict, agent: Any) -> bool:
        return True

    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        fn = _resolve_callable("call_skill_tool", call_skill_tool)
        return ToolExecutionResult(output=fn(name, args, agent.workspace_dirs))


class CoreSubagentToolHandler(ToolHandler):
    def matches(self, name: str) -> bool:
        return name in ("run_subagents_parallel", "ask_question") or is_subagent_tool(name)

    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        res = await agent._execute_core_tool(name, args, call_id)
        return ToolExecutionResult(output=res)


class ShellToolHandler(ToolHandler):
    def matches(self, name: str) -> bool:
        return is_shell_tool(name)

    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        before_snap = agent.activity.take_workspace_snapshot(agent.workspace_dirs)
        fn = _resolve_callable("call_shell_tool", call_shell_tool)
        result = await run_sync(fn, name, args, agent.on_confirm)
        if not isinstance(result, str) or not result.startswith(("[使用者拒絕", "[操作已由使用者取消", "[ERROR]")):
            agent.activity.apply_workspace_diff(before_snap, agent.workspace_dirs)
        return ToolExecutionResult(output=result)


class FsToolHandler(ToolHandler):
    def matches(self, name: str) -> bool:
        return is_fs_tool(name)

    def is_parallel_safe(self, name: str, args: dict, agent: Any) -> bool:
        return True

    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        existed_before: bool | None = None
        if name in ("write_to_file", "append_to_file", "replace_file_content"):
            target_p = args.get("path") or args.get("target_file") or args.get("file") or ""
            existed_before = agent.activity.inspect_file_before_tool(target_p)
        fs_confirm = (lambda _desc: True) if agent.accept_edits else agent.on_confirm
        fn = _resolve_callable("call_fs_tool", call_fs_tool)
        result = await run_sync(fn, name, args, fs_confirm, agent.workspace_dirs)
        return ToolExecutionResult(output=result, existed_before=existed_before)


class DocumentToolHandler(ToolHandler):
    def matches(self, name: str) -> bool:
        return is_document_tool(name)

    def is_parallel_safe(self, name: str, args: dict, agent: Any) -> bool:
        return True

    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        fn = _resolve_callable("call_document_tool", call_document_tool)
        result = await run_sync(fn, name, args, agent.workspace_dirs)
        return ToolExecutionResult(output=result)


class WebToolHandler(ToolHandler):
    def matches(self, name: str) -> bool:
        return is_web_tool(name)

    def is_parallel_safe(self, name: str, args: dict, agent: Any) -> bool:
        return True

    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        fn = _resolve_callable("call_web_tool", call_web_tool)
        result = await run_sync(fn, name, args)
        return ToolExecutionResult(output=result)


class DocGenToolHandler(ToolHandler):
    def matches(self, name: str) -> bool:
        return is_doc_tool(name)

    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        doc_confirm = (lambda _desc: True) if agent.accept_edits else agent.on_confirm
        fn = _resolve_callable("call_doc_tool", call_doc_tool)
        result, fpath = await run_sync(
            fn,
            name,
            args,
            doc_confirm,
            agent.workspace_dirs,
            lambda: agent.cancel_requested,
        )
        if fpath:
            agent.last_files.append(fpath)
        return ToolExecutionResult(output=result, generated_path=fpath)


class IndexedSearchToolHandler(ToolHandler):
    def matches(self, name: str) -> bool:
        return name == "indexed_search"

    def is_parallel_safe(self, name: str, args: dict, agent: Any) -> bool:
        return True

    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        from pocket_agent.tools.rag import call_indexed_search

        result = await run_sync(call_indexed_search, args, agent.workspace_dirs)
        return ToolExecutionResult(output=result)


class McpToolHandler(ToolHandler):
    """預設後備：處理所有外部或內建 MCP 工具。"""

    def matches(self, name: str) -> bool:
        return True

    def is_parallel_safe(self, name: str, args: dict, agent: Any) -> bool:
        return not agent._needs_mcp_confirm(name, args)

    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        if agent._needs_mcp_confirm(name, args):
            if agent.on_confirm is None:
                raise RuntimeError(f"缺乏使用者確認機制，拒絕執行 MCP 工具 {name}")
            ok = await run_sync(agent.on_confirm, name, args)
            if isinstance(ok, str) and ok.strip():
                result = (
                    f"[使用者拒絕執行此工具，並提供後續指示：\n"
                    f"「{ok.strip()}」\n"
                    "請立即並嚴格遵循使用者的上述指示繼續處理任務，切勿再次嘗試執行違背該指示的工具。]"
                )
                return ToolExecutionResult(output=result, short_circuit=True, ok_override=True)
            if ok is not True:
                raise Exception(f"使用者拒絕執行 MCP 工具 {name}")

        if agent.cancel_requested:
            return ToolExecutionResult(output="", short_circuit=True)

        if hasattr(agent, "mcp") and hasattr(agent.mcp, "is_sensitive_tool"):
            if agent.mcp.is_sensitive_tool(name):
                agent.is_sensitive_session = True

        result = await agent.mcp.call(name, args)
        return ToolExecutionResult(output=result)


class ToolRegistry:
    """集中管理並派發所有工具轉接器。"""

    def __init__(self, handlers: list[ToolHandler], fallback: ToolHandler) -> None:
        self._handlers = handlers
        self._fallback = fallback

    def resolve(self, name: str) -> ToolHandler:
        for handler in self._handlers:
            if handler.matches(name):
                return handler
        return self._fallback


default_tool_registry = ToolRegistry(
    handlers=[
        SkillToolHandler(),
        CoreSubagentToolHandler(),
        ShellToolHandler(),
        FsToolHandler(),
        DocumentToolHandler(),
        WebToolHandler(),
        DocGenToolHandler(),
        IndexedSearchToolHandler(),
    ],
    fallback=McpToolHandler(),
)

"""核心工具派發抽象層（Adapter / Command Pattern）。

將 Agent 主迴圈與各具體工具模組（fs、shell、doc、document、web、rag、skills、subagents、MCP）
的判斷與呼叫差異解耦，維持核心主迴圈的獨立性與可擴充性。
"""

import asyncio
import inspect
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pocket_agent.core.cancellation import run_sync
from pocket_agent.core.subagents import (
    build_subagent_labels,
    get_subagent,
    is_background_subagent_tool,
    is_subagent_tool,
)
from pocket_agent.extensions.mcp import parse_mcp_tool_name
from pocket_agent.extensions.skills import call_skill_tool, is_skill_tool
from pocket_agent.system.constants import (
    DEFAULT_MAX_PARALLEL_SUBAGENTS,
    TOOL_NOT_FOUND_PREFIX,
    TOOL_OVERWRITE_REQUIRED_PREFIX,
    TOOL_TARGET_NOT_FOUND_PREFIX,
    WEB_RESEARCH_CHECKPOINT_INTERVAL,
)
from pocket_agent.tools.doc import call_doc_tool, is_doc_tool
from pocket_agent.tools.document import call_document_tool, is_document_tool
from pocket_agent.tools.fs import (
    call_fs_tool,
    call_scratch_tool,
    is_fs_tool,
    is_safe_read_fs_tool,
    is_scratch_tool,
    wrap_external_content,
)
from pocket_agent.tools.rag import call_indexed_search, is_rag_tool
from pocket_agent.tools.shell import call_shell_tool, is_shell_tool
from pocket_agent.tools.web import call_web_tool, is_soft_web_error, is_web_tool


def _resolve_callable(attr_name: str, fallback: Any) -> Any:
    """優先從 pocket_agent.core.agent 解析，以相容對該模組之 monkeypatch/patch 測試。"""
    import sys

    agent_mod = sys.modules.get("pocket_agent.core.agent")
    if agent_mod is not None:
        return getattr(agent_mod, attr_name, fallback)
    return fallback


_SOFT_TOOL_STATUS_PREFIXES: tuple[tuple[str, str], ...] = (
    (TOOL_NOT_FOUND_PREFIX, "not_found"),
    (TOOL_OVERWRITE_REQUIRED_PREFIX, "overwrite_required"),
    (TOOL_TARGET_NOT_FOUND_PREFIX, "target_not_found"),
)
_ERROR_TOOL_PREFIXES: tuple[str, ...] = ("Error:", "[ERROR]", "[使用者拒絕")


def classify_tool_result(result: object) -> tuple[bool, str]:
    """依統一前綴判定工具執行結果的 `(ok_status, tool_status)`。"""
    if not isinstance(result, str):
        return True, "ok"
    if result.startswith(_ERROR_TOOL_PREFIXES):
        return False, "error"
    for prefix, status in _SOFT_TOOL_STATUS_PREFIXES:
        if result.startswith(prefix):
            return True, status
    return True, "ok"


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

    allows_dynamic_parallel_safe: bool = False

    @abstractmethod
    def matches(self, name: str) -> bool:
        """判斷此 Handler 是否負責處理指定名稱的工具。"""

    def is_parallel_safe(self, name: str, args: dict, agent: Any) -> bool:
        """判斷該工具呼叫是否為無副作用且免互動確認的唯讀操作。"""
        return False

    def is_sensitive(self, name: str, agent: Any) -> bool:
        """判斷該工具是否屬於敏感診斷工具（失敗時須遮罩原始例外訊息）。"""
        return False

    def on_cancel(self, name: str, agent: Any) -> str:
        """工具執行遭使用者取消時的收尾與提示訊息。"""
        return "[操作已由使用者取消中斷，工作可能仍在非同步執行緒中收尾，未產出後續回應]"

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
        output = await run_sync(fn, name, args, agent.workspace_dirs)
        return ToolExecutionResult(output=output)


class ParallelSubagentsToolHandler(ToolHandler):
    """平行派發多個子代理並彙整結果。"""

    def matches(self, name: str) -> bool:
        return name == "run_subagents_parallel"

    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        tasks = args.get("tasks", [])
        if not tasks:
            return ToolExecutionResult(output="[ERROR] 未提供任何子代理任務。")
        labels = build_subagent_labels(tasks)
        sem = asyncio.Semaphore(DEFAULT_MAX_PARALLEL_SUBAGENTS)
        agent._emit("parallel_subagents_start", {"count": len(tasks), "labels": labels})
        coros = [
            agent._run_subagent(t, display_label=label, semaphore=sem, non_interactive=True)
            for t, label in zip(tasks, labels)
        ]
        try:
            results = await asyncio.gather(*coros, return_exceptions=True)
        finally:
            agent._emit("parallel_subagents_done", {})

        combined = []
        require_save_prompt = False
        for idx, res in enumerate(results):
            t_name = tasks[idx].get("name", f"Subagent-{idx}")
            sa_def = get_subagent(t_name)
            if sa_def and sa_def.get("post_actions", {}).get("require_save_prompt"):
                require_save_prompt = True

            if isinstance(res, asyncio.CancelledError):
                combined.append(f"【子代理 {t_name} 已取消】")
            elif isinstance(res, BaseException):
                combined.append(f"【子代理 {t_name} 錯誤】\n{res}")
            else:
                combined.append(str(res))

        result = wrap_external_content("\n\n".join(combined))
        if require_save_prompt:
            result += agent.build_save_report_prompt(is_parallel=True)
        return ToolExecutionResult(output=result)


class AskQuestionToolHandler(ToolHandler):
    """暫停代理迴圈並向使用者提出結構化選項問題。"""

    def matches(self, name: str) -> bool:
        return name == "ask_question"

    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        if getattr(agent, "_non_interactive", False):
            return ToolExecutionResult(
                output=(
                    "[ERROR] 背景或平行子代理不可呼叫 ask_question 互動提問，請直接依現有資訊完成分析或回報資料缺口。"
                )
            )
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        def set_result_threadsafe(res: Any) -> None:
            loop.call_soon_threadsafe(fut.set_result, res)

        agent._emit(
            "ask_question",
            {
                "call_id": call_id,
                "questions": args.get("questions", []),
                "callback": set_result_threadsafe,
            },
        )
        result_dict = await fut
        return ToolExecutionResult(output=json.dumps(result_dict, ensure_ascii=False))


class SubagentToolHandler(ToolHandler):
    """委派單一子代理執行任務。"""

    allows_dynamic_parallel_safe: bool = True

    def matches(self, name: str) -> bool:
        return is_subagent_tool(name)

    def is_parallel_safe(self, name: str, args: dict, agent: Any) -> bool:
        sub_name = str(args.get("name") or "").strip()
        return bool(sub_name and hasattr(agent, "is_readonly_subagent") and agent.is_readonly_subagent(sub_name))

    @staticmethod
    def _finalize_subagent_output(raw_result: str, sub_name: str, agent: Any) -> ToolExecutionResult:
        result = wrap_external_content(raw_result)
        sa_def = get_subagent(sub_name)
        if sa_def and sa_def.get("post_actions", {}).get("require_save_prompt"):
            result += agent.build_save_report_prompt(is_parallel=False)
        return ToolExecutionResult(output=result)

    async def execute_parallel(
        self,
        args: dict,
        agent: Any,
        *,
        display_label: str,
        semaphore: asyncio.Semaphore,
    ) -> ToolExecutionResult:
        raw_result = await agent._run_subagent(
            args,
            display_label=display_label,
            semaphore=semaphore,
            non_interactive=True,
        )
        return self._finalize_subagent_output(raw_result, str(args.get("name") or ""), agent)

    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        raw_result = await agent._run_subagent(args)
        return self._finalize_subagent_output(raw_result, str(args.get("name") or ""), agent)


class BackgroundSubagentToolHandler(ToolHandler):
    """非同步派發、查詢／取回與取消唯讀背景子代理。"""

    def matches(self, name: str) -> bool:
        return is_background_subagent_tool(name)

    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        if name == "start_background_subagent":
            sub_name = str(args.get("name") or "").strip()
            task = str(args.get("task") or "").strip()
            job = agent.start_background_subagent(sub_name, task)
            return ToolExecutionResult(
                output=(
                    f"已於背景啟動唯讀子代理 `{job.subagent_name}`（job_id: `{job.job_id}`）。\n"
                    f"可先處理其他工作或回覆使用者，稍後呼叫 `check_background_subagent` 查詢進度或取回結果。"
                )
            )

        if name == "cancel_background_subagent":
            job_id = str(args.get("job_id") or "").strip()
            if not job_id:
                return ToolExecutionResult(output="[ERROR] 請提供要取消的 job_id。")
            ok = await agent.cancel_background_job(job_id)
            if ok:
                return ToolExecutionResult(output=f"已取消背景子代理工作 `{job_id}`。")
            return ToolExecutionResult(output=f"[ERROR] 找不到執行中的背景工作 `{job_id}`（可能已完成或不存在）。")

        # check_background_subagent
        job_id = str(args.get("job_id") or "").strip()
        if not job_id:
            jobs = agent.list_background_jobs()
            if not jobs:
                return ToolExecutionResult(output="目前沒有任何背景子代理工作。")
            lines = ["目前背景子代理工作清單："]
            for j in jobs:
                progress = f" | 進度: {j.last_progress}" if j.last_progress else ""
                lines.append(
                    f"- `{j.job_id}` [{j.subagent_name}] 狀態: {j.status} ({j.elapsed_seconds():.1f}s){progress}"
                )
            return ToolExecutionResult(output="\n".join(lines))

        raw_wait = args.get("wait_seconds", 0)
        try:
            wait_sec = int(raw_wait) if raw_wait is not None else 0
        except TypeError, ValueError:
            wait_sec = 0

        job = await agent.wait_background_job(job_id, timeout=float(wait_sec))
        if job.status == "running":
            progress = f"（目前進度：{job.last_progress}）" if job.last_progress else ""
            return ToolExecutionResult(
                output=(
                    f"背景工作 `{job.job_id}`（{job.subagent_name}）仍在執行中，"
                    f"已耗時 {job.elapsed_seconds():.1f}s{progress}。"
                )
            )

        job = agent.collect_background_job(job_id, inject_history=False)
        if job.status == "completed":
            output = wrap_external_content(f"【子代理 {job.subagent_name} 背景結果 ({job.job_id})】\n{job.result}")
            if job.require_save_prompt:
                output += agent.build_save_report_prompt(is_parallel=False)
            return ToolExecutionResult(output=output)
        if job.status == "failed":
            return ToolExecutionResult(output=f"[ERROR] 背景工作 `{job.job_id}` 執行失敗：\n{job.error}")
        extra = f"\n{job.result}" if job.result else ""
        return ToolExecutionResult(output=f"背景工作 `{job.job_id}`（{job.subagent_name}）已取消。{extra}")


class ShellToolHandler(ToolHandler):
    def matches(self, name: str) -> bool:
        return is_shell_tool(name)

    def on_cancel(self, name: str, agent: Any) -> str:
        from pocket_agent.tools.shell import interrupt_active_shell_processes_with_status

        killed, statuses = interrupt_active_shell_processes_with_status()
        if killed > 0:
            all_confirmed = bool(statuses) and all(s.exited and s.tree_cleanup_confirmed for s in statuses)
            if all_confirmed:
                return f"[操作已由使用者取消中斷，進行中之 {killed} 個 Shell 子行程樹已確認終止]"
            return f"[操作已由使用者取消中斷，已請求終止 {killed} 個 Shell 子行程樹（部分程序樹清理未確認）]"
        return "[操作已由使用者取消中斷，Shell 指令未執行或已終止]"

    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        before_snap = await run_sync(agent.activity.take_workspace_snapshot, agent.workspace_dirs)
        fn = _resolve_callable("call_shell_tool", call_shell_tool)
        result = await run_sync(fn, name, args, agent.on_confirm)
        if not isinstance(result, str) or not result.startswith(("[使用者拒絕", "[操作已由使用者取消", "[ERROR]")):
            await run_sync(agent.activity.apply_workspace_diff, before_snap, agent.workspace_dirs)
        return ToolExecutionResult(output=result)


class ScratchToolHandler(ToolHandler):
    """沙盒專用筆記工具轉接器（免確認、非平行安全、敏感會話阻斷）。"""

    def matches(self, name: str) -> bool:
        return is_scratch_tool(name)

    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        if getattr(agent, "is_sensitive_session", False):
            return ToolExecutionResult(
                output="[ERROR] 敏感會話期間基於資安政策，禁止將內容寫入沙盒筆記。",
            )
        fn = _resolve_callable("call_scratch_tool", call_scratch_tool)
        result = await run_sync(fn, name, args)
        if isinstance(result, str) and result.startswith("[SUCCESS]") and "：" in result:
            note_path = result.split("：", 1)[-1].strip()
            paths = getattr(agent, "scratch_note_paths", None)
            if isinstance(paths, list) and note_path and note_path not in paths:
                paths.append(note_path)
        return ToolExecutionResult(output=result)


def _callable_accepts_kwarg(fn: Any, kwarg_name: str) -> bool:
    """檢查可呼叫物件是否接受指定關鍵字參數，以維持測試 monkeypatch 相容性。"""
    try:
        sig = inspect.signature(fn)
    except TypeError, ValueError:
        return False
    return kwarg_name in sig.parameters or any(
        param.kind == inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values()
    )


class FsToolHandler(ToolHandler):
    def matches(self, name: str) -> bool:
        return is_fs_tool(name)

    def is_parallel_safe(self, name: str, args: dict, agent: Any) -> bool:
        return is_safe_read_fs_tool(name)

    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        existed_before: bool | None = None
        is_write_op = name in ("write_to_file", "append_to_file", "replace_file_content")
        if is_write_op:
            target_p = args.get("path") or args.get("target_file") or args.get("file") or ""
            existed_before = agent.activity.inspect_file_before_tool(target_p)
        accept_edits = bool(getattr(agent, "accept_edits", False))
        is_sensitive = bool(getattr(agent, "is_sensitive_session", False))
        fs_confirm = (lambda _desc: True) if accept_edits else agent.on_confirm
        fn = _resolve_callable("call_fs_tool", call_fs_tool)
        extra_kwargs: dict[str, Any] = {}
        if name == "write_to_file" and accept_edits and _callable_accepts_kwarg(fn, "require_overwrite_flag"):
            extra_kwargs["require_overwrite_flag"] = True
        if is_write_op and not is_sensitive and _callable_accepts_kwarg(fn, "auto_allow_scratch"):
            extra_kwargs["auto_allow_scratch"] = True
        result = await run_sync(fn, name, args, fs_confirm, agent.workspace_dirs, **extra_kwargs)
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
        return is_rag_tool(name)

    def is_parallel_safe(self, name: str, args: dict, agent: Any) -> bool:
        return True

    async def execute(self, name: str, args: dict, call_id: str, agent: Any) -> ToolExecutionResult:
        fn = _resolve_callable("call_indexed_search", call_indexed_search)
        result = await run_sync(fn, args, agent.workspace_dirs)
        return ToolExecutionResult(output=result)


class McpToolHandler(ToolHandler):
    """預設後備：處理所有外部或內建 MCP 工具。"""

    def matches(self, name: str) -> bool:
        return True

    def is_parallel_safe(self, name: str, args: dict, agent: Any) -> bool:
        return not agent._needs_mcp_confirm(name, args)

    def is_sensitive(self, name: str, agent: Any) -> bool:
        if hasattr(agent, "mcp") and hasattr(agent.mcp, "is_sensitive_tool"):
            return bool(agent.mcp.is_sensitive_tool(name))
        _, raw_name = parse_mcp_tool_name(name)
        return raw_name.startswith(("rhel_", "win_rdp_", "mysql_", "mssql_"))

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
        ParallelSubagentsToolHandler(),
        AskQuestionToolHandler(),
        SubagentToolHandler(),
        BackgroundSubagentToolHandler(),
        ShellToolHandler(),
        ScratchToolHandler(),
        FsToolHandler(),
        DocumentToolHandler(),
        WebToolHandler(),
        DocGenToolHandler(),
        IndexedSearchToolHandler(),
    ],
    fallback=McpToolHandler(),
)


class WebResearchGuard:
    """封裝網路調研次數上限、收斂提醒與階段性筆記檢查點邏輯，從主迴圈解耦。"""

    def __init__(self, soft_limit: int, hard_limit: int) -> None:
        self.soft_limit = soft_limit
        self.hard_limit = hard_limit
        self.call_count = 0
        self.projected_count = 0
        self.convergence_warned = False

    def reset_projection(self) -> None:
        self.projected_count = self.call_count

    def allow_parallel_batch(self, name: str) -> bool:
        if not is_web_tool(name):
            return True
        self.projected_count += 1
        return self.projected_count <= self.hard_limit

    def check_limit(self, name: str) -> str | None:
        if not is_web_tool(name):
            return None
        self.call_count += 1
        if self.call_count > self.hard_limit:
            return (
                f"[調研上限攔截] 本輪網路檢索次數已達上限（{self.hard_limit} 次）。"
                "系統已暫停執行新的網路調研，請立即根據目前累積之資訊完成回答或寫入檔案。"
            )
        return None

    def decorate_result(
        self,
        name: str,
        result: str,
        *,
        ok_status: bool,
        is_sensitive_session: bool,
        scratch_note_filename: str | None = None,
    ) -> str:
        if not is_web_tool(name):
            return result
        if ok_status and self.call_count >= self.soft_limit and not self.convergence_warned:
            self.convergence_warned = True
            return result + (
                f"\n\n[系統提醒：調研收斂] 你已執行多次網路檢索（累計 {self.call_count} 次）。"
                "請檢視現有資訊是否充足，優先開始產出解答或建立檔案，避免無休止調研。"
            )
        if (
            ok_status
            and self.call_count < self.soft_limit
            and self.call_count > 0
            and self.call_count % WEB_RESEARCH_CHECKPOINT_INTERVAL == 0
            and not is_sensitive_session
        ):
            if scratch_note_filename:
                return result + (
                    f"\n\n[系統提醒：階段性筆記] 已完成 {self.call_count} 次網路檢索。"
                    "若研究仍需多步探索，建議適時使用 `write_scratch_note`"
                    f"（檔名 `{scratch_note_filename}`）將已確認的重點與有效網址記錄至沙盒筆記，"
                    "以防範長對話壓縮或網路瞬斷造成資訊遺失。"
                )
            from pocket_agent.system.paths import scratch_dir

            notes_path = scratch_dir() / "notes.md"
            return result + (
                f"\n\n[系統提醒：階段性筆記] 已完成 {self.call_count} 次網路檢索。"
                "若研究仍需多步探索，建議適時使用 `write_to_file` "
                f"將已確認的重點與有效網址記錄至 `{notes_path}`，"
                "以防範長對話壓縮或網路瞬斷造成資訊遺失。"
            )
        return result

    @staticmethod
    def is_soft_error(name: str, result: str) -> bool:
        return is_web_tool(name) and is_soft_web_error(result)

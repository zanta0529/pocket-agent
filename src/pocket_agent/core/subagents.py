"""Subagents：帶獨立人設的子代理，主 agent 透過 run_subagent 工具委派。

定義檔（兩處都掃，使用者目錄優先）：
    <repo>/subagents/<name>.md
    ~/.pocket-agent/subagents/<name>.md

檔案格式：
    ---
    name: researcher
    description: 需要深入查找、彙整資料時委派
    model: <選填，覆寫模型>
    reasoning_effort: <選填 none/low/medium/high/xhigh>
    ---
    子代理的 system prompt。
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pocket_agent.core.cancellation import CancellationScope, current_scope
from pocket_agent.system.constants import (
    DEFAULT_BG_CHECK_MAX_WAIT_SEC,
    DEFAULT_MAX_BACKGROUND_JOB_HISTORY,
    DEFAULT_MAX_BACKGROUND_SUBAGENTS,
)
from pocket_agent.system.paths import bundle_dir, user_dir
from pocket_agent.system.registry import MarkdownRegistry
from pocket_agent.tools.fs import SAFE_READ_FS_TOOLS

log = logging.getLogger("pocket-agent")

REPO_SUBAGENTS_DIR = bundle_dir() / "subagents"  # 內建，隨程式發佈（唯讀）
USER_SUBAGENTS_DIR = user_dir() / "subagents"  # 使用者自訂／plugin 裝入（可寫）


class SubagentRegistry(MarkdownRegistry):
    def get_glob_pattern(self) -> str:
        return "*.md"

    def parse_item(self, path: Path, meta: dict, body: str) -> dict:
        return {
            "name": meta.get("name") or path.stem,
            "description": meta.get("description", ""),
            "model": meta.get("model", "") or None,
            "reasoning_effort": meta.get("reasoning_effort", "") or None,
            "allowed_tools": meta.get("allowed_tools"),  # list[str] | None
            "max_tool_rounds": meta.get("max_tool_rounds"),  # int | None
            "post_actions": meta.get("post_actions", {}),  # dict
            "prompt": body.strip(),
            "path": path,
        }


# 全域實例
_registry = SubagentRegistry(REPO_SUBAGENTS_DIR, USER_SUBAGENTS_DIR)


def list_subagents() -> list[dict]:
    """所有 subagent 的 {name, description, model, reasoning_effort, prompt, path}。"""
    return _registry.list_all()


def get_subagent(name: str) -> dict | None:
    return _registry.get_item(name)


# ── run_subagent 工具 ─────────────────────────────


def subagent_tools() -> list[dict]:
    """有定義 subagent 才註冊工具。"""
    agents = list_subagents()
    if not agents:
        return []

    listing = "、".join(f"{a['name']}（{a['description']}）" for a in agents)
    return [
        {
            "type": "function",
            "name": "run_subagent",
            "description": (
                "把一個明確的子任務委派給專門的子代理，回傳它的結果。"
                "子代理有獨立的對話脈絡與人設，適合需要專注處理的工作。"
                f"可用的子代理：{listing}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "子代理名稱",
                    },
                    "task": {
                        "type": "string",
                        "description": "要交辦的完整任務描述（子代理看不到主對話，請寫清楚）",
                    },
                },
                "required": ["name", "task"],
            },
        }
    ]


def is_subagent_tool(name: str) -> bool:
    return name == "run_subagent"


BACKGROUND_SUBAGENT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "start_background_subagent",
        "check_background_subagent",
        "cancel_background_subagent",
    }
)

BUILTIN_READONLY_SUBAGENT_TOOLS: frozenset[str] = SAFE_READ_FS_TOOLS | frozenset(
    {
        "write_scratch_note",
        "read_document",
        "search_web",
        "read_url_content",
        "indexed_search",
        "use_skill",
        "search_skills",
    }
)

STATEFUL_MCP_TOOL_PREFIXES: tuple[str, ...] = ("browser_", "win_rdp_")
STATEFUL_MCP_SERVER_NAMES: frozenset[str] = frozenset({"browser", "win_rdp", "win-rdp"})


def is_background_subagent_tool(name: str) -> bool:
    return name in BACKGROUND_SUBAGENT_TOOL_NAMES


def build_subagent_labels(tasks: list[dict[str, Any]]) -> list[str]:
    """為平行子代理任務產生不重複的顯示標籤（同名時加上 #1、#2 後綴）。"""
    totals: dict[str, int] = {}
    for item in tasks:
        raw_name = str(item.get("name", "subagent"))
        totals[raw_name] = totals.get(raw_name, 0) + 1

    seen: dict[str, int] = {}
    labels: list[str] = []
    for item in tasks:
        raw_name = str(item.get("name", "subagent"))
        if totals[raw_name] <= 1:
            labels.append(raw_name)
        else:
            seen[raw_name] = seen.get(raw_name, 0) + 1
            labels.append(f"{raw_name}#{seen[raw_name]}")
    return labels


def background_subagent_tools() -> list[dict[str, Any]]:
    """有定義 subagent 時，回傳背景非同步派發／查詢／取消工具 Schema。"""
    agents = list_subagents()
    if not agents:
        return []

    names = [a["name"] for a in agents]
    listing = "、".join(f"{a['name']}（{a['description']}）" for a in agents)
    return [
        {
            "type": "function",
            "name": "start_background_subagent",
            "description": (
                "在背景非同步啟動唯讀子代理（不阻塞目前主對話），立即回傳 job_id。"
                "適合需要長時間探索或蒐集資料的任務，稍後可用 check_background_subagent 查詢或取回結果。"
                f"可用的子代理：{listing}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": names,
                        "description": "子代理名稱",
                    },
                    "task": {
                        "type": "string",
                        "description": "交辦的完整任務描述（背景子代理看不到主對話且僅限唯讀工具）",
                    },
                },
                "required": ["name", "task"],
            },
        },
        {
            "type": "function",
            "name": "check_background_subagent",
            "description": (
                "查詢背景子代理工作狀態或取回已完成結果。"
                "省略 job_id 時列出全部背景工作；指定 job_id 且工作已完成時直接回傳報告。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "背景工作 ID（選填，省略時回傳全部工作摘要）",
                    },
                    "wait_seconds": {
                        "type": "integer",
                        "description": (
                            f"選填：若指定工作仍在執行中，最多等待幾秒"
                            f"（預設 0 立即返回，上限 {DEFAULT_BG_CHECK_MAX_WAIT_SEC} 秒）"
                        ),
                    },
                },
                "required": [],
            },
        },
        {
            "type": "function",
            "name": "cancel_background_subagent",
            "description": "取消指定之執行中背景子代理工作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "要取消的背景工作 ID",
                    },
                },
                "required": ["job_id"],
            },
        },
    ]


@dataclass
class BackgroundJob:
    """背景唯讀子代理工作狀態封裝。"""

    job_id: str
    subagent_name: str
    task: str
    status: str = "running"  # running | completed | failed | cancelled
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    last_progress: str = ""
    result: str = ""
    error: str = ""
    is_sensitive: bool = False
    collected: bool = False
    require_save_prompt: bool = False
    sub_agent: Any = None
    asyncio_task: asyncio.Task[Any] | None = None
    scope: CancellationScope | None = None
    _activity_merged: bool = False

    def elapsed_seconds(self) -> float:
        end_ts = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end_ts - self.created_at)


class BackgroundSubagentManager:
    """管理主代理於單一 asyncio 事件迴圈上派發的唯讀背景子代理工作。"""

    def __init__(
        self,
        parent: Any,
        *,
        max_concurrent: int = DEFAULT_MAX_BACKGROUND_SUBAGENTS,
        max_history: int = DEFAULT_MAX_BACKGROUND_JOB_HISTORY,
    ) -> None:
        self._parent = parent
        self._max_concurrent = max(1, int(max_concurrent))
        self._max_history = max(1, int(max_history))
        self._jobs: dict[str, BackgroundJob] = {}
        self.on_state_change: Callable[[], None] | None = None
        self.on_job_finished: Callable[[BackgroundJob], None] | None = None

    def _notify_state_change(self) -> None:
        cb = self.on_state_change
        if callable(cb):
            try:
                cb()
            except Exception:
                log.debug("background_jobs on_state_change 回呼失敗", exc_info=True)

    def _prune_history(self) -> None:
        finished = [j for j in self._jobs.values() if j.status != "running"]
        if len(finished) <= self._max_history:
            return
        finished.sort(key=lambda j: j.created_at)
        for old_job in finished[: len(finished) - self._max_history]:
            self._jobs.pop(old_job.job_id, None)

    def start(self, name: str, task: str) -> BackgroundJob:
        clean_name = str(name or "").strip()
        clean_task = str(task or "").strip()
        if not clean_name:
            raise ValueError("Subagent name is required")
        if not clean_task:
            raise ValueError("Subagent task is required")

        sub_config = get_subagent(clean_name)
        if not sub_config:
            raise ValueError(f"Unknown subagent: {clean_name}")

        running_count = sum(1 for j in self._jobs.values() if j.status == "running")
        if running_count >= self._max_concurrent:
            raise RuntimeError(f"背景子代理已達同時執行上限（{self._max_concurrent} 個），請先等候完成或取消既有工作。")

        readonly_tools = self._parent.resolve_readonly_subagent_tools(sub_config.get("allowed_tools"))
        if not readonly_tools:
            raise ValueError(f"子代理「{clean_name}」沒有可用的唯讀免確認工具，無法於背景執行。")

        loop = asyncio.get_running_loop()
        if not loop.is_running():
            raise RuntimeError("asyncio 事件迴圈尚未啟動，無法建立背景工作。")

        job_id = f"bg-{uuid.uuid4().hex[:6]}"
        sys_prompt = sub_config.get("system_prompt") or sub_config.get("prompt", "You are a helpful assistant.")
        sub = self._parent.create_ephemeral_subagent(
            system_prompt=sys_prompt,
            model=sub_config.get("model"),
            allowed_tools=readonly_tools,
            reasoning_effort=sub_config.get("reasoning_effort") or self._parent.reasoning_effort,
            max_tool_rounds=sub_config.get("max_tool_rounds"),
        )
        # 背景子代理強制唯讀無互動，絕不搶佔終端機 stdin
        sub.on_confirm = None
        sub.accept_edits = False
        sub._non_interactive = True
        sub.scratch_note_filename = f"{job_id}_{clean_name}.md"

        require_save = bool(sub_config.get("post_actions", {}).get("require_save_prompt"))
        job_scope = CancellationScope()
        job = BackgroundJob(
            job_id=job_id,
            subagent_name=clean_name,
            task=clean_task,
            require_save_prompt=require_save,
            sub_agent=sub,
            scope=job_scope,
        )

        last_notify_ts = 0.0

        def _on_bg_stream(kind: str, data: dict | str) -> None:
            nonlocal last_notify_ts
            snippet = ""
            if kind == "tool_start":
                tool_name = data.get("name", "") if isinstance(data, dict) else str(data)
                snippet = f"tool: {tool_name}"
            elif kind in ("thinking", "text"):
                snippet = " ".join(str(data).split())
            if snippet:
                job.last_progress = snippet[:80]
                now_ts = time.monotonic()
                if kind == "tool_start" or (now_ts - last_notify_ts) >= 0.3:
                    last_notify_ts = now_ts
                    self._notify_state_change()

        sub.on_stream = _on_bg_stream

        async def _runner() -> None:
            token = current_scope.set(job_scope)
            try:
                _thinking, reply, _usage = await sub.chat(clean_task)
                if "已達工具呼叫上限" in (reply or "") and hasattr(self._parent, "_build_subagent_handoff_notice"):
                    handoff = self._parent._build_subagent_handoff_notice(
                        sub,
                        run_id=job_id,
                        reason="已達子代理工具呼叫輪次上限",
                    )
                    if handoff:
                        reply = f"{reply}\n\n{handoff}"
                job.result = reply or ""
                job.status = "completed"
                job.last_progress = "completed"
            except asyncio.CancelledError:
                job.status = "cancelled"
                job.last_progress = "cancelled"
                if hasattr(self._parent, "_build_subagent_handoff_notice"):
                    handoff = self._parent._build_subagent_handoff_notice(
                        sub,
                        run_id=job_id,
                        reason="背景工作已取消",
                    )
                    if handoff:
                        job.result = handoff
            except Exception as exc:
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.last_progress = "failed"
                if hasattr(self._parent, "_build_subagent_handoff_notice"):
                    handoff = self._parent._build_subagent_handoff_notice(
                        sub,
                        run_id=job_id,
                        reason=job.error,
                    )
                    if handoff:
                        job.error = f"{job.error}\n\n{handoff}"
            finally:
                job.is_sensitive = bool(getattr(sub, "is_sensitive_session", False))
                if job.finished_at is None:
                    job.finished_at = time.time()
                await job_scope.drain()
                current_scope.reset(token)
                self._prune_history()
                self._notify_state_change()
                if job.status in ("completed", "failed") and callable(self.on_job_finished):
                    try:
                        self.on_job_finished(job)
                    except Exception:
                        log.debug("background_jobs on_job_finished 回呼失敗", exc_info=True)

        self._jobs[job_id] = job
        job.asyncio_task = loop.create_task(_runner())
        self._notify_state_change()
        log.info("BACKGROUND SUBAGENT started job_id=%s name=%s", job_id, clean_name)
        return job

    def list_jobs(self) -> list[BackgroundJob]:
        return list(self._jobs.values())

    def get(self, job_id: str) -> BackgroundJob | None:
        return self._jobs.get(str(job_id or "").strip())

    def counts(self) -> tuple[int, int]:
        running = sum(1 for j in self._jobs.values() if j.status == "running")
        uncollected = sum(
            1 for j in self._jobs.values() if j.status in ("completed", "failed", "cancelled") and not j.collected
        )
        return running, uncollected

    def summarize_active_jobs(self) -> list[dict[str, Any]]:
        """回傳執行中與尚未取回之背景工作精簡快照（供 CLI 狀態列即時顯示）。"""
        running = [j for j in self._jobs.values() if j.status == "running"]
        ready = [j for j in self._jobs.values() if j.status in ("completed", "failed", "cancelled") and not j.collected]
        return [
            {
                "job_id": j.job_id,
                "name": j.subagent_name,
                "status": j.status,
                "progress": j.last_progress,
                "created_at": j.created_at,
                "finished_at": j.finished_at,
            }
            for j in (running + ready)
        ]

    async def wait(
        self,
        job_id: str,
        timeout: float = 0.0,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> BackgroundJob:
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"找不到背景工作：{job_id}")
        wait_limit = max(0.0, min(float(timeout or 0.0), float(DEFAULT_BG_CHECK_MAX_WAIT_SEC)))
        if wait_limit <= 0.0 or job.asyncio_task is None or job.asyncio_task.done():
            return job

        deadline = time.monotonic() + wait_limit
        while not job.asyncio_task.done():
            if cancel_check and cancel_check():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(asyncio.shield(job.asyncio_task), timeout=min(0.2, remaining))
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                break
        return job

    def collect(self, job_id: str, *, inject_history: bool = False) -> BackgroundJob:
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"找不到背景工作：{job_id}")
        if not job._activity_merged and job.sub_agent is not None and hasattr(self._parent, "activity"):
            self._parent.activity.merge_from(job.sub_agent.activity)
            job._activity_merged = True
        if job.is_sensitive:
            self._parent.is_sensitive_session = True
        job.collected = True

        if inject_history and hasattr(self._parent, "history"):
            if job.status == "completed":
                body = f"【子代理 {job.subagent_name} 背景工作 {job.job_id} 結果】\n{job.result}"
            elif job.status == "failed":
                body = f"【子代理 {job.subagent_name} 背景工作 {job.job_id} 失敗】\n{job.error}"
            else:
                body = f"【子代理 {job.subagent_name} 背景工作 {job.job_id} 已取消】\n{job.result}".strip()
            wrapped = (
                f"[系統備註：以下報告由背景唯讀子代理 ({job.subagent_name}, {job.job_id}) "
                f"獨立執行後回傳，為合法可信來源。]\n\n{body}"
            )
            self._parent.history.append({"role": "user", "content": f"/bg {job.subagent_name} {job.task}"})
            self._parent.history.append({"role": "assistant", "content": wrapped})

        self._notify_state_change()
        return job

    async def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status != "running":
            return False
        if job.scope is not None:
            job.scope.cancel()
        if job.sub_agent is not None:
            job.sub_agent.request_stop()
        if job.asyncio_task is not None and not job.asyncio_task.done():
            job.asyncio_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(job.asyncio_task), timeout=1.0)
            except asyncio.TimeoutError, asyncio.CancelledError, Exception:
                pass
        if job.status == "running":
            job.status = "cancelled"
            job.finished_at = time.time()
        self._notify_state_change()
        return True

    async def cancel_all(self) -> int:
        running_ids = [j.job_id for j in self._jobs.values() if j.status == "running"]
        cancelled = 0
        for jid in running_ids:
            if await self.cancel(jid):
                cancelled += 1
        return cancelled

    async def shutdown(self, timeout: float = 3.0) -> None:
        running = [j for j in self._jobs.values() if j.status == "running"]
        if not running:
            return
        for job in running:
            if job.scope is not None:
                job.scope.cancel()
            if job.sub_agent is not None:
                job.sub_agent.request_stop()
            if job.asyncio_task is not None and not job.asyncio_task.done():
                job.asyncio_task.cancel()
        tasks = [j.asyncio_task for j in running if j.asyncio_task is not None]
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
            except (asyncio.TimeoutError, Exception) as err:
                log.warning("背景子代理關閉逾時或失敗 (%s 秒): %s", timeout, err)
        for job in running:
            if job.status == "running":
                job.status = "cancelled"
                job.finished_at = time.time()
        self._notify_state_change()

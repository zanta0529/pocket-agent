"""斜線指令空白後參數自動補全（純記憶體與靜態常數查詢，零網路／磁碟掃描延遲）。"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit.completion import Completion

from pocket_agent.frontend.cli.render import plain
from pocket_agent.frontend.cli.state import CliState
from pocket_agent.system.i18n import t

if TYPE_CHECKING:
    from pocket_agent.core.agent import Agent

_JOBS_SUBCOMMANDS: tuple[tuple[str, str], ...] = (
    ("show", "arg_comp.jobs_sub_show"),
    ("collect", "arg_comp.jobs_sub_collect"),
    ("all", "arg_comp.jobs_sub_all"),
    ("clear", "arg_comp.jobs_sub_clear"),
    ("cancel", "arg_comp.jobs_sub_cancel"),
    ("list", "arg_comp.jobs_sub_list"),
)

_SESSION_SUBCOMMANDS: tuple[tuple[str, str], ...] = (
    ("list", "arg_comp.session_sub_list"),
    ("load", "arg_comp.session_sub_load"),
    ("save", "arg_comp.session_sub_save"),
    ("delete", "arg_comp.session_sub_delete"),
    ("prune", "arg_comp.session_sub_prune"),
)

_ATTACHMENTS_SUBCOMMANDS: tuple[tuple[str, str], ...] = (
    ("clear", "arg_comp.attachments_sub_clear"),
    ("remove", "arg_comp.attachments_sub_remove"),
)

_CMD_ALIASES: dict[str, str] = {
    "effort": "reasoning",
    "tools": "mcp",
}


def _iter_static_choices(
    prefix: str,
    items: tuple[tuple[str, str], ...] | list[tuple[str, str]],
) -> Iterator[Completion]:
    """依前綴過濾並產生固定選項之補全項目。"""
    low = prefix.lower()
    for name, desc in items:
        if name.lower().startswith(low):
            yield Completion(
                name,
                start_position=-len(prefix),
                display=plain(name),
                display_meta=plain(desc),
            )


def _iter_jobs_completions(rest: str, agent: "Agent | None") -> Iterator[Completion]:
    """/jobs 兩階段參數補全：第一階段為子指令，第二階段為背景工作 ID。"""
    if " " not in rest:
        yield from _iter_static_choices(rest, [(name, t(key)) for name, key in _JOBS_SUBCOMMANDS])
        return

    sub_cmd, arg_rest = rest.split(" ", 1)
    if " " in arg_rest:
        return

    sub_lower = sub_cmd.lower()
    if sub_lower not in ("show", "collect", "cancel") or agent is None:
        return
    if not callable(getattr(type(agent), "list_background_jobs", None)):
        return

    jobs = agent.list_background_jobs()
    if sub_lower == "show":
        candidates = sorted(jobs, key=lambda j: j.status != "running" and j.collected)
    elif sub_lower == "collect":
        candidates = [j for j in jobs if j.status != "running" and not j.collected]
    else:
        candidates = [j for j in jobs if j.status == "running"]

    from pocket_agent.frontend.cli.commands.subagents import _job_status_meta, _summarize_task

    arg_low = arg_rest.lower()
    for job in candidates:
        if arg_low and arg_low not in job.job_id.lower() and arg_low not in job.subagent_name.lower():
            continue
        icon, status_label = _job_status_meta(job.status)
        collected_tag = t("subagent_cmd.jobs_collected_tag") if job.collected else ""
        snippet = _summarize_task(getattr(job, "task", ""))
        task_note = f' — "{snippet}"' if snippet else ""
        yield Completion(
            job.job_id,
            start_position=-len(arg_rest),
            display=plain(f"{icon} {job.job_id} [{job.subagent_name}]"),
            display_meta=plain(f"{status_label}{collected_tag}{task_note}"),
        )

    if len(candidates) > 1 and sub_lower in ("collect", "cancel") and "all".startswith(arg_low):
        all_key = "arg_comp.jobs_all_ready" if sub_lower == "collect" else "arg_comp.jobs_all_running"
        yield Completion(
            "all",
            start_position=-len(arg_rest),
            display=plain("all"),
            display_meta=plain(t(all_key, count=len(candidates))),
        )


def _iter_subagent_completions(prefix: str, agent: "Agent | None", *, readonly_only: bool) -> Iterator[Completion]:
    """/bg 與 /subagent 的子代理名稱補全。"""
    if " " in prefix:
        return
    from pocket_agent.core.subagents import list_subagents

    low = prefix.lower()
    is_ro = getattr(type(agent), "is_readonly_subagent", None) if agent is not None else None
    for sa in list_subagents():
        name = sa["name"]
        if readonly_only and callable(is_ro) and not agent.is_readonly_subagent(name):
            continue
        if name.lower().startswith(low):
            yield Completion(
                name,
                start_position=-len(prefix),
                display=plain(name),
                display_meta=plain(sa.get("description", "")),
            )


def _iter_pending_file_completions(prefix: str, state: CliState) -> Iterator[Completion]:
    """/rm-file 與 /attachments remove 的待送附件編號補全。"""
    if " " in prefix:
        return
    files = getattr(state, "pending_files", [])
    for idx, path_str in enumerate(files, 1):
        num = str(idx)
        name = Path(path_str).name
        if not prefix or num.startswith(prefix) or prefix.lower() in name.lower():
            yield Completion(
                num,
                start_position=-len(prefix),
                display=plain(f"{num}. {name}"),
                display_meta=plain(path_str),
            )


def _iter_mcp_completions(rest: str, agent: "Agent | None") -> Iterator[Completion]:
    """/mcp 伺服器名稱補全與 /mcp sqlite <path> 路徑補全。"""
    if agent is None:
        return None
    mcp_manager = getattr(agent, "mcp", None)
    if mcp_manager is None:
        return
    grouped = mcp_manager.get_grouped_tools() if hasattr(mcp_manager, "get_grouped_tools") else {}
    if not grouped:
        return
    low = rest.lower()
    for server_name, tools in grouped.items():
        if server_name.lower().startswith(low):
            yield Completion(
                server_name,
                start_position=-len(rest),
                display=plain(server_name),
                display_meta=plain(t("mcp_cmd.server_tools_count", count=len(tools))),
            )


def _iter_single_arg_completions(
    cmd: str,
    rest: str,
    state: CliState,
    agent: "Agent | None",
) -> Iterator[Completion]:
    """處理單一參數（無第二段空白）之斜線指令補全。"""
    if " " in rest:
        return

    if cmd == "reasoning":
        from pocket_agent.system.constants import VALID_REASONING_EFFORTS

        items = [(lvl, t(f"system_cmd.reasoning_{lvl}")) for lvl in VALID_REASONING_EFFORTS]
        yield from _iter_static_choices(rest, items)
    elif cmd == "session":
        yield from _iter_static_choices(rest, [(n, t(k)) for n, k in _SESSION_SUBCOMMANDS])
    elif cmd == "rm-file":
        yield from _iter_pending_file_completions(rest, state)
    elif cmd == "rm-dir" and agent is not None:
        dirs = [(str(d), str(d)) for d in getattr(agent, "workspace_dirs", [])]
        yield from _iter_static_choices(rest, dirs)
    elif cmd == "model":
        from pocket_agent.system.providers import PROVIDER_STRATEGIES, SUPPORTED_PROVIDERS

        items = [(p, PROVIDER_STRATEGIES[p].display_name) for p in SUPPORTED_PROVIDERS]
        yield from _iter_static_choices(rest, items)
    elif cmd == "skills":
        from pocket_agent.extensions.skills import list_skills

        ws = getattr(agent, "workspace_dirs", None) if agent is not None else None
        items = [(s["name"], s.get("description", "")) for s in list_skills(ws)]
        yield from _iter_static_choices(rest, items)
    elif cmd == "prune":
        yield from _iter_static_choices(rest, [("--keep", t("arg_comp.prune_opt_keep"))])


def complete_slash_command_args(
    text: str,
    state: CliState,
    agent: "Agent | None",
    path_completer: Callable[..., Iterator[Completion]],
) -> Iterator[Completion]:
    """依斜線指令與游標前參數內容提供空白觸發之動態補全。"""
    if not text.startswith("/") or " " not in text:
        return

    head, rest = text[1:].split(" ", 1)
    cmd = _CMD_ALIASES.get(head.lower(), head.lower())

    if cmd == "jobs":
        yield from _iter_jobs_completions(rest, agent)
    elif cmd in ("bg", "subagent"):
        yield from _iter_subagent_completions(rest, agent, readonly_only=(cmd == "bg"))
    elif cmd == "add-dir" and " " not in rest:
        yield from path_completer(rest, dirs_only=True)
    elif cmd == "attachments":
        if " " not in rest:
            yield from _iter_static_choices(rest, [(n, t(k)) for n, k in _ATTACHMENTS_SUBCOMMANDS])
        else:
            sub_cmd, arg_rest = rest.split(" ", 1)
            if sub_cmd.lower() == "remove":
                yield from _iter_pending_file_completions(arg_rest, state)
    elif cmd == "mcp":
        if " " not in rest:
            yield from _iter_mcp_completions(rest, agent)
        else:
            sub_cmd, arg_rest = rest.split(" ", 1)
            if sub_cmd.lower() == "sqlite" and " " not in arg_rest:
                yield from path_completer(arg_rest)
    else:
        yield from _iter_single_arg_completions(cmd, rest, state, agent)

"""斜線指令空白後參數自動補全（Strategy + Registry 模式，純記憶體與常數查詢）。"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


@dataclass(frozen=True)
class ArgCompletionContext:
    """斜線指令參數補全的唯讀執行期上下文。"""

    state: CliState
    agent: "Agent | None"
    path_completer: Callable[..., Iterator[Completion]]


class ArgCompleterStrategy(ABC):
    """斜線指令參數補全策略抽象介面。"""

    @abstractmethod
    def complete(self, rest: str, ctx: ArgCompletionContext) -> Iterator[Completion]:
        """依據游標前之參數字串產生補全候選項目。"""


class ChoiceArgCompleter(ArgCompleterStrategy):
    """單段靜態或動態選項前綴過濾補全策略。"""

    def __init__(self, items_provider: Callable[[ArgCompletionContext], list[tuple[str, str]]]) -> None:
        self._items_provider = items_provider

    def complete(self, rest: str, ctx: ArgCompletionContext) -> Iterator[Completion]:
        if " " in rest:
            return
        low = rest.lower()
        for name, desc in self._items_provider(ctx):
            if name.lower().startswith(low):
                yield Completion(
                    name,
                    start_position=-len(rest),
                    display=plain(name),
                    display_meta=plain(desc),
                )


class PendingFileArgCompleter(ArgCompleterStrategy):
    """待送附件編號補全策略（供 /rm-file 與 /attachments remove 共用）。"""

    def complete(self, rest: str, ctx: ArgCompletionContext) -> Iterator[Completion]:
        if " " in rest:
            return
        files = getattr(ctx.state, "pending_files", [])
        low = rest.lower()
        for idx, path_str in enumerate(files, 1):
            num = str(idx)
            name = Path(path_str).name
            if not rest or num.startswith(rest) or low in name.lower():
                yield Completion(
                    num,
                    start_position=-len(rest),
                    display=plain(f"{num}. {name}"),
                    display_meta=plain(path_str),
                )


class PathArgCompleter(ArgCompleterStrategy):
    """檔案／目錄路徑補全委派策略（供 /add-dir 與 /mcp sqlite 共用）。"""

    def __init__(self, *, dirs_only: bool = False) -> None:
        self._dirs_only = dirs_only

    def complete(self, rest: str, ctx: ArgCompletionContext) -> Iterator[Completion]:
        if " " in rest:
            return
        if self._dirs_only:
            yield from ctx.path_completer(rest, dirs_only=True)
        else:
            yield from ctx.path_completer(rest)


class TwoStageArgCompleter(ArgCompleterStrategy):
    """兩段式參數補全組合策略：第一段補全子指令，第二段依子指令查表委派。"""

    def __init__(
        self,
        first_stage: ArgCompleterStrategy,
        second_stages: dict[str, ArgCompleterStrategy],
    ) -> None:
        self._first_stage = first_stage
        self._second_stages = {k.lower(): v for k, v in second_stages.items()}

    def complete(self, rest: str, ctx: ArgCompletionContext) -> Iterator[Completion]:
        if " " not in rest:
            yield from self._first_stage.complete(rest, ctx)
            return
        sub_cmd, arg_rest = rest.split(" ", 1)
        strategy = self._second_stages.get(sub_cmd.lower())
        if strategy is not None:
            yield from strategy.complete(arg_rest, ctx)


class JobsTargetArgCompleter(ArgCompleterStrategy):
    """背景工作目標 ID 動態補全策略（依模式篩選 show / collect / cancel）。"""

    def __init__(
        self,
        predicate: Callable[[Any], bool],
        *,
        sort_active_first: bool = False,
        all_i18n_key: str | None = None,
    ) -> None:
        self._predicate = predicate
        self._sort_active_first = sort_active_first
        self._all_i18n_key = all_i18n_key

    def complete(self, rest: str, ctx: ArgCompletionContext) -> Iterator[Completion]:
        if " " in rest or ctx.agent is None:
            return
        if not callable(getattr(type(ctx.agent), "list_background_jobs", None)):
            return

        candidates = [j for j in ctx.agent.list_background_jobs() if self._predicate(j)]
        if self._sort_active_first:
            candidates = sorted(candidates, key=lambda j: j.status != "running" and j.collected)

        from pocket_agent.frontend.cli.commands.subagents import _job_status_meta, _summarize_task

        arg_low = rest.lower()
        for job in candidates:
            if arg_low and arg_low not in job.job_id.lower() and arg_low not in job.subagent_name.lower():
                continue
            icon, status_label = _job_status_meta(job.status)
            collected_tag = t("subagent_cmd.jobs_collected_tag") if job.collected else ""
            snippet = _summarize_task(getattr(job, "task", ""))
            task_note = f' — "{snippet}"' if snippet else ""
            yield Completion(
                job.job_id,
                start_position=-len(rest),
                display=plain(f"{icon} {job.job_id} [{job.subagent_name}]"),
                display_meta=plain(f"{status_label}{collected_tag}{task_note}"),
            )

        if self._all_i18n_key and len(candidates) > 1 and "all".startswith(arg_low):
            yield Completion(
                "all",
                start_position=-len(rest),
                display=plain("all"),
                display_meta=plain(t(self._all_i18n_key, count=len(candidates))),
            )


class ArgCompleterRegistry:
    """斜線指令參數補全策略註冊表。"""

    def __init__(self) -> None:
        self._strategies: dict[str, ArgCompleterStrategy] = {}

    def register(
        self,
        name: str,
        strategy: ArgCompleterStrategy,
        *,
        aliases: tuple[str, ...] = (),
    ) -> None:
        self._strategies[name.lower()] = strategy
        for alias in aliases:
            self._strategies[alias.lower()] = strategy

    def get(self, name: str) -> ArgCompleterStrategy | None:
        return self._strategies.get(name.lower())


def _i18n_choices(pairs: tuple[tuple[str, str], ...]) -> Callable[[ArgCompletionContext], list[tuple[str, str]]]:
    return lambda _ctx: [(name, t(key)) for name, key in pairs]


def _subagent_items(ctx: ArgCompletionContext, *, readonly_only: bool) -> list[tuple[str, str]]:
    from pocket_agent.core.subagents import list_subagents

    is_ro = getattr(type(ctx.agent), "is_readonly_subagent", None) if ctx.agent is not None else None
    items: list[tuple[str, str]] = []
    for sa in list_subagents():
        name = sa["name"]
        if readonly_only and callable(is_ro) and ctx.agent is not None and not ctx.agent.is_readonly_subagent(name):
            continue
        items.append((name, sa.get("description", "")))
    return items


def _reasoning_items(_ctx: ArgCompletionContext) -> list[tuple[str, str]]:
    from pocket_agent.system.constants import VALID_REASONING_EFFORTS

    return [(lvl, t(f"system_cmd.reasoning_{lvl}")) for lvl in VALID_REASONING_EFFORTS]


def _mcp_server_items(ctx: ArgCompletionContext) -> list[tuple[str, str]]:
    if ctx.agent is None:
        return []
    mcp_manager = getattr(ctx.agent, "mcp", None)
    if mcp_manager is None or not hasattr(mcp_manager, "get_grouped_tools"):
        return []
    grouped = mcp_manager.get_grouped_tools() or {}
    return [(server, t("mcp_cmd.server_tools_count", count=len(tools))) for server, tools in grouped.items()]


def _workspace_dir_items(ctx: ArgCompletionContext) -> list[tuple[str, str]]:
    if ctx.agent is None:
        return []
    return [(str(d), str(d)) for d in getattr(ctx.agent, "workspace_dirs", [])]


def _skill_items(ctx: ArgCompletionContext) -> list[tuple[str, str]]:
    from pocket_agent.extensions.skills import list_skills

    ws = getattr(ctx.agent, "workspace_dirs", None) if ctx.agent is not None else None
    return [(s["name"], s.get("description", "")) for s in list_skills(ws)]


def _build_default_registry() -> ArgCompleterRegistry:
    registry = ArgCompleterRegistry()

    jobs_strategy = TwoStageArgCompleter(
        ChoiceArgCompleter(_i18n_choices(_JOBS_SUBCOMMANDS)),
        {
            "show": JobsTargetArgCompleter(lambda _j: True, sort_active_first=True),
            "collect": JobsTargetArgCompleter(
                lambda j: j.status != "running" and not j.collected,
                all_i18n_key="arg_comp.jobs_all_ready",
            ),
            "cancel": JobsTargetArgCompleter(
                lambda j: j.status == "running",
                all_i18n_key="arg_comp.jobs_all_running",
            ),
        },
    )
    registry.register("jobs", jobs_strategy)
    registry.register("bg", ChoiceArgCompleter(lambda ctx: _subagent_items(ctx, readonly_only=True)))
    registry.register("subagent", ChoiceArgCompleter(lambda ctx: _subagent_items(ctx, readonly_only=False)))
    registry.register("reasoning", ChoiceArgCompleter(_reasoning_items), aliases=("effort",))
    registry.register("session", ChoiceArgCompleter(_i18n_choices(_SESSION_SUBCOMMANDS)))
    registry.register(
        "attachments",
        TwoStageArgCompleter(
            ChoiceArgCompleter(_i18n_choices(_ATTACHMENTS_SUBCOMMANDS)),
            {"remove": PendingFileArgCompleter()},
        ),
    )
    registry.register("rm-file", PendingFileArgCompleter())
    registry.register("rm-dir", ChoiceArgCompleter(_workspace_dir_items))
    registry.register("add-dir", PathArgCompleter(dirs_only=True))
    registry.register(
        "mcp",
        TwoStageArgCompleter(
            ChoiceArgCompleter(_mcp_server_items),
            {"sqlite": PathArgCompleter(dirs_only=False)},
        ),
        aliases=("tools",),
    )
    registry.register("skills", ChoiceArgCompleter(_skill_items))
    registry.register("prune", ChoiceArgCompleter(lambda _ctx: [("--keep", t("arg_comp.prune_opt_keep"))]))
    return registry


_DEFAULT_REGISTRY = _build_default_registry()


def complete_slash_command_args(
    text: str,
    state: CliState,
    agent: "Agent | None",
    path_completer: Callable[..., Iterator[Completion]],
) -> Iterator[Completion]:
    """依斜線指令名稱查表委派至對應補全策略。"""
    if not text.startswith("/") or " " not in text:
        return

    head, rest = text[1:].split(" ", 1)
    strategy = _DEFAULT_REGISTRY.get(head)
    if strategy is None:
        return
    ctx = ArgCompletionContext(state=state, agent=agent, path_completer=path_completer)
    yield from strategy.complete(rest, ctx)

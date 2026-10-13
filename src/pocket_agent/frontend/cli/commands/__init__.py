from typing import TYPE_CHECKING

from pocket_agent.frontend.cli.commands.core import CliCommandRegistry, CommandContext, CommandResult
from pocket_agent.frontend.cli.commands.misc import (
    AttachmentsCommand,
    CleanCommand,
    ClearCommand,
    CodeSearchCommand,
    FileCommand,
    GoalCommand,
    HelpCommand,
    ImageCommand,
    LogoutCommand,
    QuitCommand,
    RmFileCommand,
    SkillsCommand,
    ToolsCommand,
    UsageCommand,
)
from pocket_agent.frontend.cli.commands.session import CompactCommand, SessionCommand
from pocket_agent.frontend.cli.commands.subagents import AgentsCommand, ParallelCommand, SubagentCommand
from pocket_agent.frontend.cli.commands.system import SysRegistryCommand, switch_model
from pocket_agent.frontend.cli.input import CliInput
from pocket_agent.frontend.cli.render import CliRenderer
from pocket_agent.frontend.cli.state import CliState

if TYPE_CHECKING:
    from pocket_agent.core.agent import Agent

__all__ = ["CommandHandler", "CommandResult", "switch_model"]


def _build_registry() -> CliCommandRegistry:
    registry = CliCommandRegistry()
    registry.register("quit", QuitCommand())
    registry.register("exit", QuitCommand())

    usage_cmd = UsageCommand()
    registry.register("usage", usage_cmd)
    registry.register("quota", usage_cmd)
    registry.register("logout", LogoutCommand())
    registry.register("clear", ClearCommand())
    clean_cmd = CleanCommand()
    registry.register("clean", clean_cmd)
    registry.register("gc", clean_cmd)
    registry.register("file", FileCommand())
    registry.register("rm-file", RmFileCommand())
    registry.register("attachments", AttachmentsCommand())
    registry.register("session", SessionCommand())
    registry.register("compact", CompactCommand())
    registry.register("agents", AgentsCommand())
    registry.register("subagent", SubagentCommand())
    registry.register("parallel", ParallelCommand())
    registry.register("tools", ToolsCommand())
    registry.register("skills", SkillsCommand())
    registry.register("help", HelpCommand())
    registry.register("goal", GoalCommand())

    cs_cmd = CodeSearchCommand()
    registry.register("codesearch", cs_cmd)
    registry.register("cs", cs_cmd)

    for sys_cmd in ("add-dir", "rm-dir", "workspace", "model", "learn", "config", "undo"):
        registry.register(sys_cmd, SysRegistryCommand(sys_cmd))

    registry.register("image", ImageCommand())

    return registry


_CLI_REGISTRY = _build_registry()


class CommandHandler:
    def __init__(
        self,
        agent: "Agent",
        state: CliState,
        reader: CliInput,
        renderer: CliRenderer,
        tools: list[str],
    ):
        self.agent = agent
        self.state = state
        self.reader = reader
        self.renderer = renderer
        self.tools = tools
        self.context = CommandContext(
            agent=agent,
            state=state,
            reader=reader,
            renderer=renderer,
            tools=tools,
        )

    async def dispatch(self, raw: str) -> CommandResult:
        text = raw.strip()
        if not text.startswith("/"):
            return CommandResult("chat", raw)

        head, *tail = text.split(maxsplit=1)
        name, args = head[1:], tail[0] if tail else ""

        command = _CLI_REGISTRY.get(name)
        if command:
            return await command.execute(self.context, args)

        # Fallback to dynamic expansions, macros, or subagents
        from pocket_agent.extensions.skills import list_skills
        from pocket_agent.system.commands import expand_command, get_command

        if get_command(name) is not None:
            expanded = expand_command("/" + name + (" " + args if args else ""))
            if expanded is None or not expanded.strip():
                raise ValueError("指令模板無法展開或內容為空；待送附件保留")
            return CommandResult("chat", expanded)

        skills_dict = {s["name"]: s for s in list_skills(self.agent.workspace_dirs)}
        if name in skills_dict:
            prompt = f"請載入並使用 `{name}` 技能來處理此任務"
            if args:
                prompt += f"：\n{args}"
            else:
                prompt += "。"
            return CommandResult("chat", prompt)

        from pocket_agent.core.subagents import list_subagents

        sa_dict = {sa["name"]: sa for sa in list_subagents()}
        if name in sa_dict:
            task = args if args else "請執行你的預設任務"
            from pocket_agent.frontend.cli.commands.subagents import execute_subagent_flow

            return await execute_subagent_flow(self.context, name, task, raw)

        raise ValueError(f"找不到指令、技能或子代理 /{name}；輸入 /help 查看說明")
